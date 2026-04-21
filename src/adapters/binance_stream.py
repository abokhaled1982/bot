"""
src/adapters/binance_stream.py — Binance WebSocket Adapter

Subscribes to Binance real-time streams:
  • !miniTicker@arr  — 24h rolling stats for ALL USDT spot pairs (updates every second)
  • <symbol>@kline_1m / @kline_5m — candlestick data for top candidates

Candidate detection logic (analogous to PumpFunAdapter):
  • Volume spike vs. 24h average
  • Positive price momentum (1m / 5m change)
  • Minimum liquidity threshold
  • Blacklist for stablecoins, wrapped tokens, etc.
"""
from __future__ import annotations

import asyncio
import json
import os
import time
from collections import defaultdict, deque
from typing import Optional

import websockets
from loguru import logger

# ── Configuration ─────────────────────────────────────────────────────────────

WS_URL_MINI_TICKER = "wss://stream.binance.com:9443/ws/!miniTicker@arr"
WS_URL_BASE        = "wss://stream.binance.com:9443/ws"

# Candidate thresholds
MIN_VOLUME_USDT_24H   = float(os.getenv("BN_MIN_VOLUME_24H",    "1000000"))   # $1M min daily vol
MIN_PRICE_USDT        = float(os.getenv("BN_MIN_PRICE",         "0.000001"))  # filter dust
VOLUME_SPIKE_FACTOR   = float(os.getenv("BN_VOLUME_SPIKE",      "2.0"))       # 2× recent average
MOMENTUM_MIN_PCT      = float(os.getenv("BN_MOMENTUM_MIN_PCT",  "0.5"))       # +0.5% 5m change (or 24h fallback at startup)
MAX_CANDIDATES        = int(os.getenv("BN_MAX_CANDIDATES",       "20"))

# Reconnect settings
RECONNECT_DELAY_BASE  = 5   # seconds
RECONNECT_DELAY_MAX   = 60  # seconds

# Tokens to always exclude
_BLACKLIST_SUFFIXES = {"UPUSDT", "DOWNUSDT", "BULLUSDT", "BEARUSDT"}
_BLACKLIST_PREFIXES = {"BUSD", "USDC", "TUSD", "DAI", "FDUSD", "USDP"}
_BLACKLIST_EXACT    = {
    "USDTUSDT", "EURUSDT", "GBPUSDT", "AUDUSDT",
    "BRLNUSDT", "TRXUSDT",  # high noise
}


def _is_blacklisted(symbol: str) -> bool:
    base = symbol.replace("USDT", "")
    if symbol in _BLACKLIST_EXACT:
        return True
    if any(symbol.endswith(s) for s in _BLACKLIST_SUFFIXES):
        return True
    if any(base.startswith(p) for p in _BLACKLIST_PREFIXES):
        return True
    return False


# ── BinanceStreamAdapter ──────────────────────────────────────────────────────

class BinanceStreamAdapter:
    """
    Connects to Binance WebSocket and maintains a live view of all USDT pairs.
    Provides get_candidates() to return the best current opportunities.
    """

    def __init__(self) -> None:
        self._connected    = False
        self._last_update  = 0.0
        self._reconnect_delay = RECONNECT_DELAY_BASE

        # symbol → latest mini-ticker dict
        self._tickers: dict[str, dict] = {}

        # symbol → deque of last 60 volume samples (one per second update)
        # Used to compute rolling average volume for spike detection
        self._volume_history: dict[str, deque] = defaultdict(lambda: deque(maxlen=60))

        # symbol → last seen 5m price for momentum calculation
        self._price_5m_ago: dict[str, float] = {}
        self._price_1m_ago: dict[str, float] = {}

        # Candidates emitted in last scan
        self._candidates: list[dict] = []

        # kline subscriber task (set later)
        self._kline_task: Optional[asyncio.Task] = None

    # ── Connection ────────────────────────────────────────────────────────────

    async def start(self) -> None:
        """Main entry point: connect and maintain the mini-ticker stream."""
        delay = RECONNECT_DELAY_BASE
        while True:
            try:
                await self._connect_mini_ticker()
                delay = RECONNECT_DELAY_BASE  # reset on success
            except Exception as e:
                self._connected = False
                logger.error(f"[BINANCE] Stream error: {e}")
                logger.warning(f"[BINANCE] Reconnecting in {delay}s...")
                await asyncio.sleep(delay)
                delay = min(delay * 2, RECONNECT_DELAY_MAX)

    async def _connect_mini_ticker(self) -> None:
        logger.info(f"[BINANCE] Connecting to {WS_URL_MINI_TICKER}")
        async with websockets.connect(
            WS_URL_MINI_TICKER,
            ping_interval=20,
            ping_timeout=10,
            close_timeout=5,
        ) as ws:
            self._connected = True
            self._reconnect_delay = RECONNECT_DELAY_BASE
            logger.info("[BINANCE] ✅ Connected — receiving all USDT mini-tickers")

            async for raw in ws:
                try:
                    await self._handle_mini_ticker(json.loads(raw))
                except Exception as e:
                    logger.debug(f"[BINANCE] Message parse error: {e}")

        self._connected = False

    async def _handle_mini_ticker(self, data: list[dict]) -> None:
        """Process the !miniTicker@arr update (array of all symbols)."""
        now = time.time()
        self._last_update = now

        for ticker in data:
            symbol = ticker.get("s", "")
            if not symbol.endswith("USDT"):
                continue
            if _is_blacklisted(symbol):
                continue

            price     = float(ticker.get("c", 0))   # last price
            vol_24h   = float(ticker.get("q", 0))   # quote volume (USDT) 24h
            high_24h  = float(ticker.get("h", 0))
            low_24h   = float(ticker.get("l", 0))
            open_24h  = float(ticker.get("o", 0))

            if price < MIN_PRICE_USDT or vol_24h < MIN_VOLUME_USDT_24H:
                continue

            # Rolling volume history (per-second update)
            self._volume_history[symbol].append(vol_24h)

            # Track price history for momentum
            old_1m = self._price_1m_ago.get(symbol)
            old_5m = self._price_5m_ago.get(symbol)

            change_1m = ((price - old_1m) / old_1m * 100) if old_1m else 0.0
            change_5m = ((price - old_5m) / old_5m * 100) if old_5m else 0.0
            change_24h = ((price - open_24h) / open_24h * 100) if open_24h else 0.0

            self._tickers[symbol] = {
                "symbol":      symbol,
                "address":     symbol,   # used as identifier in pipeline
                "source":      "BINANCE_STREAM",
                "price_usd":   price,
                "volume_24h":  vol_24h,
                "high_24h":    high_24h,
                "low_24h":     low_24h,
                "change_24h":  change_24h,
                "change_1m":   change_1m,
                "change_5m":   change_5m,
                "updated_at":  now,
            }

        # Update price snapshots (every ~60 updates ≈ 1 min, every ~300 ≈ 5 min)
        # We simply store the current price periodically via a counter trick
        tick_count = getattr(self, "_tick_count", 0) + 1
        self._tick_count = tick_count

        if tick_count % 60 == 0:
            for sym, td in self._tickers.items():
                self._price_1m_ago[sym] = td["price_usd"]

        if tick_count % 300 == 0:
            for sym, td in self._tickers.items():
                self._price_5m_ago[sym] = td["price_usd"]

        # Refresh candidates
        self._candidates = self._compute_candidates()

    # ── Candidate detection ───────────────────────────────────────────────────

    def _compute_candidates(self) -> list[dict]:
        """
        Score and rank all current tickers.
        Returns top N candidates sorted by momentum score.
        """
        scored = []

        for symbol, td in self._tickers.items():
            vol_24h   = td["volume_24h"]
            change_5m = td["change_5m"]
            change_1m = td["change_1m"]
            price     = td["price_usd"]

            # Volume spike: compare current 24h vol to rolling average
            history = list(self._volume_history[symbol])
            if len(history) >= 10:
                avg_vol = sum(history[:-1]) / (len(history) - 1)
                spike   = vol_24h / avg_vol if avg_vol > 0 else 1.0
            else:
                spike = 1.0

            td["volume_spike"] = round(spike, 2)

            # Use 5m momentum if available, fall back to 24h / 1m at startup
            has_5m_history = symbol in self._price_5m_ago
            momentum = change_5m if has_5m_history else (td["change_24h"] / 4.8)  # ~5m equiv

            # Score: momentum + volume spike
            score = (
                momentum * 2.0 +
                change_1m * 1.0 +
                min(spike, 5.0) * 5.0
            )

            # Only include if positive momentum and meaningful volume spike
            if momentum < MOMENTUM_MIN_PCT:
                continue
            if spike < VOLUME_SPIKE_FACTOR and len(history) >= 10:
                continue

            scored.append((score, {**td}))

        # Sort descending by score
        scored.sort(key=lambda x: x[0], reverse=True)
        return [item for _, item in scored[:MAX_CANDIDATES]]

    # ── Public API ────────────────────────────────────────────────────────────

    def get_candidates(self, limit: int = 10) -> list[dict]:
        """Return top momentum candidates (analogous to PumpFunAdapter.get_candidates)."""
        return self._candidates[:limit]

    def get_ticker(self, symbol: str) -> Optional[dict]:
        """Return latest data for a specific symbol (e.g. 'BTCUSDT')."""
        return self._tickers.get(symbol)

    def all_tickers(self) -> dict[str, dict]:
        """Return snapshot of all tracked tickers."""
        return dict(self._tickers)

    def status(self) -> dict:
        """Return connection status summary."""
        return {
            "connected":       self._connected,
            "last_update":     self._last_update,
            "tracked_symbols": len(self._tickers),
            "candidates":      len(self._candidates),
            "age_sec":         round(time.time() - self._last_update, 1) if self._last_update else None,
        }

    async def cleanup_loop(self) -> None:
        """Periodically log status (keeps interface consistent with other adapters)."""
        while True:
            await asyncio.sleep(60)
            st = self.status()
            logger.info(
                f"[BINANCE] Status | Connected: {st['connected']} | "
                f"Symbols: {st['tracked_symbols']} | "
                f"Candidates: {st['candidates']} | "
                f"Last update: {st['age_sec']}s ago"
            )
