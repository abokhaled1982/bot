"""
src/adapters/binance_orderflow.py — Order Flow Adapter

Connects to THREE Binance WebSocket streams simultaneously:
  1. !miniTicker@arr  — All USDT prices (1s update)
  2. <sym>@aggTrade   — Every trade on top-50 pairs (whale detection)
  3. <sym>@depth5     — Order book top 5 bids/asks (imbalance detection)

Signals emitted:
  • WHALE_BUY    — Large market buy detected (>$50k in one trade)
  • WHALE_SELL   — Large market sell detected
  • BOOK_LONG    — Bid volume >> Ask volume (buying pressure)
  • BOOK_SHORT   — Ask volume >> Bid volume (selling pressure)
"""
from __future__ import annotations

import asyncio
import json
import os
import time
from collections import defaultdict, deque
from dataclasses import dataclass, field
from typing import Optional

import websockets
from loguru import logger

# ── Config ────────────────────────────────────────────────────────────────────

WHALE_THRESHOLD     = float(os.getenv("WHALE_THRESHOLD_USDT", "50000"))   # $50k per trade
IMBALANCE_RATIO     = float(os.getenv("IMBALANCE_RATIO",      "1.5"))     # 1.5x bid/ask
MIN_VOLUME_24H      = float(os.getenv("BN_MIN_VOLUME_24H",    "5000000")) # $5M min
TOP_PAIRS_COUNT     = int(os.getenv("TOP_PAIRS",              "20"))      # Subscribe to top N
SIGNAL_TTL          = float(os.getenv("SIGNAL_TTL",           "30.0"))    # Signal valid for 30s

WS_BASE = "wss://stream.binance.com:9443"


@dataclass
class OrderFlowSignal:
    symbol:     str
    signal:     str        # WHALE_BUY | WHALE_SELL | BOOK_LONG | BOOK_SHORT
    price:      float
    value_usd:  float      # trade size or imbalance value
    ratio:      float      # bid/ask ratio or whale multiplier
    timestamp:  float = field(default_factory=time.time)

    @property
    def age_sec(self) -> float:
        return time.time() - self.timestamp

    @property
    def is_fresh(self) -> bool:
        return self.age_sec < SIGNAL_TTL


class BinanceOrderFlowAdapter:
    """
    Monitors Binance order flow in real-time.
    Use get_signals() to retrieve fresh signals.
    Event-driven: subscribe to signal_queue for immediate notifications.
    """

    def __init__(self) -> None:
        # Mini-ticker data (price + 24h stats)
        self._tickers: dict[str, dict]  = {}
        self._connected_mini             = False
        self._last_update                = 0.0

        # Order book state: symbol → {bids: [...], asks: [...]}
        self._books:   dict[str, dict]  = {}

        # Recent signals (max 200, time-ordered)
        self._signals: deque[OrderFlowSignal] = deque(maxlen=200)

        # ⚡ Event-driven queue: whale signals pushed here immediately
        self.signal_queue: asyncio.Queue[OrderFlowSignal] = asyncio.Queue(maxsize=500)

        # Rolling 30s trade volume per symbol (buy vs sell)
        self._buy_vol:  dict[str, deque] = defaultdict(lambda: deque(maxlen=300))
        self._sell_vol: dict[str, deque] = defaultdict(lambda: deque(maxlen=300))

        # Track subscribed pairs
        self._subscribed_pairs: list[str] = []

    # ── Public API ────────────────────────────────────────────────────────────

    def get_signals(self, min_type: Optional[str] = None) -> list[OrderFlowSignal]:
        """Return all fresh signals, optionally filtered by type."""
        fresh = [s for s in self._signals if s.is_fresh]
        if min_type:
            fresh = [s for s in fresh if s.signal == min_type]
        return sorted(fresh, key=lambda s: s.timestamp, reverse=True)

    def get_ticker(self, symbol: str) -> Optional[dict]:
        return self._tickers.get(symbol)

    def all_tickers(self) -> dict[str, dict]:
        return dict(self._tickers)

    def get_book(self, symbol: str) -> Optional[dict]:
        return self._books.get(symbol)

    def get_top_pairs(self, n: int = 20) -> list[str]:
        """Return top-N pairs by 24h volume."""
        pairs = [
            (sym, td["volume_24h"])
            for sym, td in self._tickers.items()
            if td["volume_24h"] >= MIN_VOLUME_24H
        ]
        pairs.sort(key=lambda x: x[1], reverse=True)
        return [p[0] for p in pairs[:n]]

    def get_candidates(self, limit: int = 10) -> list[dict]:
        """Return tickers that have recent WHALE_BUY + BOOK_LONG signals."""
        signals = self.get_signals()
        sig_symbols: dict[str, set] = defaultdict(set)
        for s in signals:
            sig_symbols[s.symbol].add(s.signal)

        candidates = []
        for sym, sig_types in sig_symbols.items():
            td = self._tickers.get(sym)
            if not td:
                continue
            # Score: whale buy = 2pts, book_long = 1pt, whale_sell = -2pts
            score = (
                (2 if "WHALE_BUY"  in sig_types else 0) +
                (1 if "BOOK_LONG"  in sig_types else 0) -
                (2 if "WHALE_SELL" in sig_types else 0)
            )
            if score >= 2:
                candidates.append({**td, "orderflow_score": score, "signals": list(sig_types)})

        candidates.sort(key=lambda x: x["orderflow_score"], reverse=True)
        return candidates[:limit]

    def status(self) -> dict:
        return {
            "connected":        self._connected_mini,
            "tracked_symbols":  len(self._tickers),
            "subscribed_pairs": len(self._subscribed_pairs),
            "fresh_signals":    len([s for s in self._signals if s.is_fresh]),
            "last_update":      self._last_update,
            "age_sec":          round(time.time() - self._last_update, 1) if self._last_update else None,
        }

    # ── Streams ───────────────────────────────────────────────────────────────

    async def start(self) -> None:
        """Start all streams concurrently."""
        await asyncio.gather(
            self._run_mini_ticker(),
            self._run_combined_stream(),
        )

    async def _run_mini_ticker(self) -> None:
        """Stream 1: All USDT mini-tickers (price + 24h data)."""
        url = f"{WS_BASE}/ws/!miniTicker@arr"
        delay = 5
        while True:
            try:
                logger.info(f"[ORDERFLOW] Connecting mini-ticker...")
                async with websockets.connect(url) as ws:
                    self._connected_mini = True
                    delay = 5
                    logger.info("[ORDERFLOW] ✅ Mini-ticker connected")
                    async for raw in ws:
                        await self._handle_mini_ticker(json.loads(raw))
            except Exception as e:
                self._connected_mini = False
                logger.warning(f"[ORDERFLOW] Mini-ticker reconnect in {delay}s: {e}")
                await asyncio.sleep(delay)
                delay = min(delay * 2, 60)

    async def _run_combined_stream(self) -> None:
        """Stream 2+3: aggTrade + depth5 for top pairs (combined stream)."""
        # Wait for mini-ticker to populate tickers first
        logger.info("[ORDERFLOW] Waiting 8s for tickers before subscribing to depth/trades...")
        await asyncio.sleep(8)

        delay = 5
        while True:
            try:
                # Refresh top pairs each reconnect
                top = self.get_top_pairs(TOP_PAIRS_COUNT)
                if not top:
                    logger.info("[ORDERFLOW] No tickers yet, waiting 5s...")
                    await asyncio.sleep(5)
                    continue

                self._subscribed_pairs = top
                streams = []
                for sym in top:
                    s = sym.lower()
                    streams.append(f"{s}@aggTrade")
                    streams.append(f"{s}@depth5")

                url = f"{WS_BASE}/stream?streams=" + "/".join(streams)
                logger.info(f"[ORDERFLOW] Subscribing to {len(top)} pairs ({len(streams)} streams)")

                async with websockets.connect(url) as ws:
                    delay = 5
                    logger.info(f"[ORDERFLOW] ✅ Order flow streams active ({len(top)} pairs)")
                    async for raw in ws:
                        data = json.loads(raw)
                        stream_name = data.get("stream", "")
                        payload     = data.get("data", {})
                        if "@aggTrade" in stream_name:
                            await self._handle_agg_trade(payload)
                        elif "@depth" in stream_name:
                            await self._handle_depth(payload, stream_name)
            except Exception as e:
                logger.warning(f"[ORDERFLOW] Combined stream reconnect in {delay}s: {e}")
                await asyncio.sleep(delay)
                delay = min(delay * 2, 60)

    # ── Handlers ──────────────────────────────────────────────────────────────

    async def _handle_mini_ticker(self, data: list[dict]) -> None:
        now = time.time()
        self._last_update = now
        for t in data:
            sym = t.get("s", "")
            if not sym.endswith("USDT"):
                continue
            price    = float(t.get("c", 0))
            vol_24h  = float(t.get("q", 0))
            open_24h = float(t.get("o", 0))
            high_24h = float(t.get("h", 0))
            low_24h  = float(t.get("l", 0))
            ch24 = ((price - open_24h) / open_24h * 100) if open_24h else 0

            if price > 0 and vol_24h >= MIN_VOLUME_24H:
                self._tickers[sym] = {
                    "symbol":     sym,
                    "address":    sym,
                    "source":     "BINANCE_ORDERFLOW",
                    "price_usd":  price,
                    "volume_24h": vol_24h,
                    "high_24h":   high_24h,
                    "low_24h":    low_24h,
                    "change_24h": ch24,
                    "updated_at": now,
                }

    async def _handle_agg_trade(self, t: dict) -> None:
        """
        aggTrade payload:
          s = symbol, p = price, q = quantity, m = is_buyer_maker
          (m=True means seller initiated = SELL, m=False = BUY)
        """
        sym   = t.get("s", "")
        price = float(t.get("p", 0))
        qty   = float(t.get("q", 0))
        is_sell = t.get("m", False)  # buyer is maker → sell pressure

        if price <= 0 or qty <= 0:
            return

        value_usd = price * qty
        now = time.time()

        # Rolling volume tracking
        entry = (now, value_usd)
        if is_sell:
            self._sell_vol[sym].append(entry)
        else:
            self._buy_vol[sym].append(entry)

        # Whale detection
        if value_usd >= WHALE_THRESHOLD:
            signal_type = "WHALE_SELL" if is_sell else "WHALE_BUY"
            sig = OrderFlowSignal(
                symbol=sym, signal=signal_type,
                price=price, value_usd=value_usd,
                ratio=round(value_usd / WHALE_THRESHOLD, 2),
            )
            self._signals.append(sig)
            # ⚡ Push to event queue immediately (non-blocking)
            try:
                self.signal_queue.put_nowait(sig)
            except asyncio.QueueFull:
                pass
            direction = "🐋 SELL" if is_sell else "🐋 BUY"
            logger.info(
                f"[ORDERFLOW] {direction} {sym} | "
                f"${value_usd:,.0f} @ ${price:.6f}"
            )

    async def _handle_depth(self, d: dict, stream: str) -> None:
        """
        depth5 payload:
          bids: [[price, qty], ...] (top 5 bids)
          asks: [[price, qty], ...] (top 5 asks)
        """
        sym = stream.split("@")[0].upper()
        bids = d.get("bids", [])
        asks = d.get("asks", [])

        if not bids or not asks:
            return

        price = self._tickers.get(sym, {}).get("price_usd", 0)
        if price <= 0:
            return

        bid_vol = sum(float(b[0]) * float(b[1]) for b in bids)  # in USDT
        ask_vol = sum(float(a[0]) * float(a[1]) for a in asks)

        self._books[sym] = {
            "bids": bids, "asks": asks,
            "bid_vol": bid_vol, "ask_vol": ask_vol,
            "updated_at": time.time(),
        }

        if ask_vol <= 0:
            return

        ratio = bid_vol / ask_vol

        if ratio >= IMBALANCE_RATIO:
            sig = OrderFlowSignal(
                symbol=sym, signal="BOOK_LONG",
                price=price, value_usd=bid_vol,
                ratio=round(ratio, 2),
            )
            self._signals.append(sig)
            # No per-update log — pipeline will log when candidate reaches G3
        elif ratio <= (1 / IMBALANCE_RATIO):
            sig = OrderFlowSignal(
                symbol=sym, signal="BOOK_SHORT",
                price=price, value_usd=ask_vol,
                ratio=round(ratio, 2),
            )
            self._signals.append(sig)

    async def cleanup_loop(self) -> None:
        while True:
            await asyncio.sleep(60)
            st = self.status()
            fresh = st["fresh_signals"]
            logger.info(
                f"[ORDERFLOW] Status | Connected: {st['connected']} | "
                f"Tickers: {st['tracked_symbols']} | "
                f"Pairs: {st['subscribed_pairs']} | "
                f"Fresh Signals: {fresh}"
            )
