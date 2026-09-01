"""
src/adapters/hyperliquid_copytrader.py — Hyperliquid Copy-Trader Adapter

Monitors top traders on Hyperliquid (fully on-chain, free API, no key needed)
and emits signals when they open/close/modify positions.

Hyperliquid is used ONLY as a signal source — all execution happens on Binance.

Signal types emitted:
  • COPY_OPEN_LONG   — Tracked trader opened a new long position
  • COPY_CLOSE_LONG  — Tracked trader closed a long position
  • COPY_OPEN_SHORT  — Tracked trader opened a new short (info only)
  • COPY_INCREASE    — Tracked trader increased an existing position
  • COPY_DECREASE    — Tracked trader decreased an existing position
"""
from __future__ import annotations

import asyncio
import json
import os
import threading
import time
from collections import defaultdict
from dataclasses import dataclass, field
from typing import Optional

import requests
import websockets
from loguru import logger

# ── Config ────────────────────────────────────────────────────────────────────

HL_API_URL = "https://api.hyperliquid.xyz/info"
HL_WS_URL = "wss://api.hyperliquid.xyz/ws"

_raw_wallets = os.getenv("HL_TRADER_WALLETS", "")
MANUAL_WALLETS: list[str] = [w.strip() for w in _raw_wallets.split(",") if w.strip()]

POLL_INTERVAL = float(os.getenv("HL_POLL_INTERVAL", "5"))
MIN_COPY_SIZE_USD = float(os.getenv("HL_MIN_COPY_SIZE_USD", "1000"))
SIGNAL_TTL = float(os.getenv("HL_SIGNAL_TTL", "60.0"))

# Auto-discovery settings
AUTO_DISCOVER = os.getenv("HL_AUTO_DISCOVER", "True").lower() == "true"
MAX_TRACKED_TRADERS = int(os.getenv("HL_MAX_TRADERS", "5"))
RESCAN_INTERVAL_SEC = float(os.getenv("HL_RESCAN_HOURS", "6")) * 3600
# Minimum requirements for a trader to be selected
MIN_TRADES = int(os.getenv("HL_MIN_TRADES", "100"))
MIN_WIN_RATE = float(os.getenv("HL_MIN_WIN_RATE", "0.55"))
MIN_ACCOUNT_VALUE = float(os.getenv("HL_MIN_ACCOUNT_USD", "10000"))
# Prefer scalpers: maximum average hold time in seconds
MAX_AVG_HOLD_SEC = float(os.getenv("HL_MAX_AVG_HOLD_SEC", "1800"))  # 30min
# Trader must have closed a trade within this window to count as "active"
ACTIVE_WITHIN_SEC = float(os.getenv("HL_ACTIVE_WITHIN_HOURS", "24")) * 3600
# How many leaderboard candidates to fetch detailed fill stats for
DISCOVERY_CANDIDATE_POOL = int(os.getenv("HL_DISCOVERY_POOL", "25"))

LEADERBOARD_URL = "https://stats-data.hyperliquid.xyz/Mainnet/leaderboard"
LEADERBOARD_WINDOWS = ("day", "week", "month", "allTime")

# Minimum spacing between HL API calls to avoid 429 rate limiting
MIN_REQUEST_INTERVAL = float(os.getenv("HL_MIN_REQUEST_INTERVAL", "2.0"))

# Where manually activated (dashboard) traders are persisted across restarts
ACTIVE_WALLETS_FILE = os.getenv("HL_ACTIVE_WALLETS_FILE", "hl_active_traders.json")

_UNSUPPORTED_COINS = {"PURR", "HFUN", "JEFF"}

_rate_lock = threading.Lock()
_last_request_time = 0.0


def _throttle() -> None:
    """Block the calling thread until MIN_REQUEST_INTERVAL has passed since the last HL call."""
    global _last_request_time
    with _rate_lock:
        wait = MIN_REQUEST_INTERVAL - (time.time() - _last_request_time)
        if wait > 0:
            time.sleep(wait)
        _last_request_time = time.time()


def _hl_request(method: str, url: str, **kwargs) -> Optional[requests.Response]:
    """Throttled HL API request with exponential backoff retry on 429."""
    backoff = 1.0
    for _ in range(4):
        _throttle()
        try:
            resp = requests.request(method, url, **kwargs)
        except requests.RequestException as e:
            logger.debug(f"[HL-COPY] Request error: {e}")
            return None
        if resp.status_code == 429:
            retry_after = float(resp.headers.get("Retry-After", backoff))
            logger.warning(f"[HL-COPY] 429 rate limited — backing off {retry_after:.1f}s")
            time.sleep(retry_after)
            backoff = min(backoff * 2, 10.0)
            continue
        return resp
    return None


def hl_coin_to_binance_symbol(coin: str) -> str:
    """Convert Hyperliquid coin name to Binance USDT spot symbol."""
    coin = coin.upper().strip()
    mapping = {"WBTC": "BTC", "WETH": "ETH"}
    coin = mapping.get(coin, coin)
    return f"{coin}USDT"


def hl_explorer_url(wallet: str) -> str:
    """Hyperliquid block explorer link for manual due-diligence on a trader."""
    return f"https://app.hyperliquid.xyz/explorer/address/{wallet}"


@dataclass
class CopySignal:
    """Signal emitted when a tracked trader makes a move."""
    trader:      str
    coin:        str
    symbol:      str       # Binance symbol (BTCUSDT, ...)
    signal:      str       # COPY_OPEN_LONG | COPY_CLOSE_LONG | ...
    size_usd:    float
    entry_price: float
    leverage:    float
    pnl_pct:     float
    timestamp:   float = field(default_factory=time.time)

    @property
    def age_sec(self) -> float:
        return time.time() - self.timestamp

    @property
    def is_fresh(self) -> bool:
        return self.age_sec < SIGNAL_TTL

    @property
    def trader_short(self) -> str:
        if len(self.trader) > 12:
            return f"{self.trader[:6]}...{self.trader[-4:]}"
        return self.trader


@dataclass
class TraderStats:
    """Cached statistics for a tracked trader."""
    wallet:       str
    total_pnl:    float = 0.0
    total_trades: int = 0
    last_update:  float = 0.0


@dataclass
class TraderMetrics:
    """Derived scalper metrics used to filter auto-discovered traders."""
    trades:           int
    win_rate:         float
    avg_hold_sec:      float
    last_active_age:  float



class HyperliquidCopyTrader:
    """
    Monitors Hyperliquid traders and emits copy-signals.

    Usage:
        adapter = HyperliquidCopyTrader()
        asyncio.create_task(adapter.start())
        signal = await adapter.signal_queue.get()
        # → signal.symbol = "BTCUSDT" → execute on Binance
    """

    def __init__(self) -> None:
        self._positions: dict[str, dict[str, dict]] = defaultdict(dict)
        self._trader_stats: dict[str, TraderStats] = {}
        self.signal_queue: asyncio.Queue[CopySignal] = asyncio.Queue(maxsize=500)
        self._signals: list[CopySignal] = []
        self._max_signals = 200
        self._connected = False
        self._ws_connected = False
        self._discovering = False
        self._last_poll = 0.0
        self._poll_count = 0
        self._wallets_lock = threading.Lock()
        # Wallets manually activated via the dashboard, persisted across restarts
        self._manual_active: set[str] = self._load_active_wallets()
        # Dynamic trader list (env manual + dashboard-activated + auto-discovered)
        self._wallets: list[str] = list(dict.fromkeys(MANUAL_WALLETS + list(self._manual_active)))
        self._last_scan = 0.0
        # Runtime-adjustable settings (dashboard "Einstellungen" tab), default from env
        self._poll_interval = POLL_INTERVAL
        self._min_copy_size_usd = MIN_COPY_SIZE_USD

    # ── Public API ────────────────────────────────────────────────────────────

    def get_signals(self, signal_type: Optional[str] = None) -> list[CopySignal]:
        fresh = [s for s in self._signals if s.is_fresh]
        if signal_type:
            fresh = [s for s in fresh if s.signal == signal_type]
        return sorted(fresh, key=lambda s: s.timestamp, reverse=True)

    def get_trader_positions(self, wallet: str) -> dict[str, dict]:
        return dict(self._positions.get(wallet, {}))

    def get_all_positions(self) -> dict[str, dict[str, dict]]:
        return {w: dict(p) for w, p in self._positions.items()}

    def get_trader_stats(self) -> dict[str, TraderStats]:
        return dict(self._trader_stats)

    def get_wallets(self) -> list[str]:
        return list(self._wallets)

    def get_active_wallets(self) -> set[str]:
        """Wallets manually activated via the dashboard."""
        return set(self._manual_active)

    def get_settings(self) -> dict:
        """Runtime + env-derived config shown/edited on the dashboard's Einstellungen tab."""
        return {
            "poll_interval": self._poll_interval,
            "min_copy_size_usd": self._min_copy_size_usd,
            "auto_discover": AUTO_DISCOVER,
            "max_tracked_traders": MAX_TRACKED_TRADERS,
            "rescan_hours": RESCAN_INTERVAL_SEC / 3600,
            "min_trades": MIN_TRADES,
            "min_win_rate": MIN_WIN_RATE,
            "min_account_value": MIN_ACCOUNT_VALUE,
            "max_avg_hold_min": MAX_AVG_HOLD_SEC / 60,
            "signal_ttl": SIGNAL_TTL,
        }

    def set_poll_interval(self, seconds: float) -> None:
        self._poll_interval = max(1.0, float(seconds))

    def set_min_copy_size(self, usd: float) -> None:
        self._min_copy_size_usd = max(0.0, float(usd))

    def activate_wallet(self, wallet: str) -> bool:
        """Manually activate a trader for immediate copy-trading. Returns True if newly added."""
        wallet = wallet.strip()
        if not wallet:
            return False
        with self._wallets_lock:
            self._manual_active.add(wallet)
            is_new = wallet not in self._wallets
            if is_new:
                self._wallets.append(wallet)
        self._save_active_wallets()
        if is_new:
            stats = self._api_trader_stats(wallet)
            if stats:
                self._trader_stats[wallet] = stats
        return is_new

    def deactivate_wallet(self, wallet: str) -> None:
        """Remove a manually activated trader from tracking."""
        with self._wallets_lock:
            self._manual_active.discard(wallet)
            if wallet in self._wallets and wallet not in MANUAL_WALLETS:
                self._wallets.remove(wallet)
        self._positions.pop(wallet, None)
        self._trader_stats.pop(wallet, None)
        self._save_active_wallets()

    @staticmethod
    def _load_active_wallets() -> set[str]:
        try:
            with open(ACTIVE_WALLETS_FILE, encoding="utf-8") as f:
                data = json.load(f)
                return set(data) if isinstance(data, list) else set()
        except (OSError, json.JSONDecodeError):
            return set()

    def _save_active_wallets(self) -> None:
        try:
            with open(ACTIVE_WALLETS_FILE, "w", encoding="utf-8") as f:
                json.dump(sorted(self._manual_active), f, indent=2)
        except OSError as e:
            logger.error(f"[HL-COPY] Failed to save active wallets: {e}")

    def list_leaderboard(
        self, window: str = "day", limit: int = 30,
        min_trades: int = 0, min_win_rate: float = 0.0,
        max_avg_hold_sec: Optional[float] = None,
        min_account_value: float = 0.0,
        progress_cb: Optional[callable] = None,
    ) -> list[dict]:
        """Rank HL leaderboard traders by PnL in a user-chosen window and attach fill metrics.

        Used by the dashboard's manual trader search — unlike `_discover_scalpers`,
        callers pick their own window/filters instead of the fixed HL_* env defaults.
        The HL API is rate-limited, so each candidate's fills lookup is throttled
        (~2s apart) — `progress_cb(done, total, wallet)` lets callers show progress
        instead of a long silent block.
        """
        rows = self._fetch_leaderboard()
        if not rows:
            return []

        ranked: list[tuple[str, float, dict]] = []
        for row in rows:
            wallet = row.get("ethAddress")
            if not wallet:
                continue
            acct_value = float(row.get("accountValue", "0") or "0")
            if acct_value < min_account_value:
                continue
            pnl, vlm = self._leaderboard_window_metric(row, window)
            if pnl <= 0:
                continue
            ranked.append((wallet, pnl, {"account_value": acct_value, "pnl": pnl, "vlm": vlm}))

        ranked.sort(key=lambda r: r[1], reverse=True)
        candidates = ranked[:limit]

        results = []
        for i, (wallet, _, info) in enumerate(candidates, 1):
            if progress_cb:
                progress_cb(i, len(candidates), wallet)
            metrics = self._compute_trader_metrics(wallet)
            if metrics is None:
                continue
            if metrics.trades < min_trades:
                continue
            if metrics.win_rate < min_win_rate:
                continue
            if max_avg_hold_sec is not None and metrics.avg_hold_sec > max_avg_hold_sec:
                continue
            results.append({
                "wallet": wallet,
                "account_value": info["account_value"],
                "window_pnl": info["pnl"],
                "window_volume": info["vlm"],
                "trades": metrics.trades,
                "win_rate": metrics.win_rate,
                "avg_hold_sec": metrics.avg_hold_sec,
                "last_active_age": metrics.last_active_age,
            })
        return results

    def status(self) -> dict:
        total_positions = sum(len(p) for p in self._positions.values())
        return {
            "connected":       self._connected,
            "ws_connected":    self._ws_connected,
            "discovering":     self._discovering,
            "tracked_traders": len(self._wallets),
            "total_positions": total_positions,
            "fresh_signals":   len([s for s in self._signals if s.is_fresh]),
            "poll_count":      self._poll_count,
            "last_poll_age":   round(time.time() - self._last_poll, 1) if self._last_poll else None,
            "auto_discover":   AUTO_DISCOVER,
            "last_scan_age":   round(time.time() - self._last_scan, 1) if self._last_scan else None,
            "next_scan_in":    round(RESCAN_INTERVAL_SEC - (time.time() - self._last_scan), 1) if self._last_scan else None,
        }

    # ── Startup ───────────────────────────────────────────────────────────────

    async def start(self) -> None:
        # Auto-discover traders if no manual wallets and auto-discover is on
        if AUTO_DISCOVER:
            logger.info("[HL-COPY] 🔍 Auto-discovery enabled — scanning for top scalpers...")
            self._discovering = True
            try:
                discovered = await asyncio.to_thread(self._discover_scalpers)
            finally:
                self._discovering = False
            for w in discovered:
                if w not in self._wallets:
                    self._wallets.append(w)
            self._last_scan = time.time()

        if not self._wallets:
            logger.warning(
                "[HL-COPY] No traders found! Set HL_TRADER_WALLETS in .env "
                "or ensure HL_AUTO_DISCOVER=True and API is reachable."
            )
            return

        logger.info(f"[HL-COPY] Tracking {len(self._wallets)} trader(s):")
        for i, w in enumerate(self._wallets, 1):
            short = f"{w[:6]}...{w[-4:]}" if len(w) > 12 else w
            stats = self._trader_stats.get(w)
            tag = ""
            if stats:
                tag = f" | ${stats.total_pnl:+,.0f} | {stats.total_trades} fills"
            manual = " (manual)" if w in MANUAL_WALLETS else " (auto)"
            logger.info(f"[HL-COPY]   #{i}: {short}{tag}{manual}")

        await self._fetch_all_trader_stats()
        await self._poll_all_positions(emit_signals=False)
        self._connected = True

        await asyncio.gather(
            self._poll_loop(),
            self._ws_loop(),
            self._rescan_loop(),
        )

    # ── REST Polling ──────────────────────────────────────────────────────────

    async def _poll_loop(self) -> None:
        while True:
            await asyncio.sleep(self._poll_interval)
            try:
                await self._poll_all_positions(emit_signals=True)
            except Exception as e:
                logger.error(f"[HL-COPY] Poll error: {e}")

    async def _poll_all_positions(self, emit_signals: bool = True) -> None:
        for wallet in list(self._wallets):
            try:
                await self._poll_trader(wallet, emit_signals)
            except Exception as e:
                short = f"{wallet[:6]}...{wallet[-4:]}"
                logger.error(f"[HL-COPY] Error polling {short}: {e}")
        self._last_poll = time.time()
        self._poll_count += 1

    async def _poll_trader(self, wallet: str, emit_signals: bool) -> None:
        data = await asyncio.to_thread(self._api_clearinghouse_state, wallet)
        if data is None:
            return

        new_positions: dict[str, dict] = {}
        for ap in data.get("assetPositions", []):
            pos = ap.get("position", {})
            coin = pos.get("coin", "")
            size_raw = float(pos.get("sizeDecimal", pos.get("size", "0")) or "0")
            entry_px = float(pos.get("entryPx", "0") or "0")
            lev = pos.get("leverage", {})
            leverage_val = float(
                lev.get("value", 1) if isinstance(lev, dict) else (lev or 1)
            )
            unrealized_pnl = float(pos.get("unrealizedPnl", "0") or "0")
            position_value = float(pos.get("positionValue", "0") or "0")

            if abs(size_raw) < 1e-12:
                continue

            pnl_pct = 0.0
            cost = abs(size_raw) * entry_px
            if cost > 0:
                pnl_pct = (unrealized_pnl / cost) * 100

            new_positions[coin] = {
                "coin": coin, "size": size_raw, "entry_px": entry_px,
                "leverage": leverage_val, "pnl_pct": pnl_pct,
                "value_usd": position_value, "unrealized_pnl": unrealized_pnl,
                "updated_at": time.time(),
            }

        if emit_signals:
            self._diff_positions(wallet, new_positions)
        self._positions[wallet] = new_positions


    def _diff_positions(self, wallet: str, new_pos: dict[str, dict]) -> None:
        old_pos = self._positions.get(wallet, {})
        short = f"{wallet[:6]}...{wallet[-4:]}" if len(wallet) > 12 else wallet

        for coin, pos in new_pos.items():
            if coin in _UNSUPPORTED_COINS:
                continue
            symbol = hl_coin_to_binance_symbol(coin)
            old = old_pos.get(coin)
            size = pos["size"]
            value = pos["value_usd"]

            if old is None and size > 0 and value >= self._min_copy_size_usd:
                self._emit(CopySignal(
                    trader=wallet, coin=coin, symbol=symbol,
                    signal="COPY_OPEN_LONG", size_usd=value,
                    entry_price=pos["entry_px"], leverage=pos["leverage"],
                    pnl_pct=pos["pnl_pct"],
                ))
                logger.info(
                    f"[HL-COPY] 📈 OPEN LONG | {short} | {coin} | "
                    f"${value:,.0f} @ ${pos['entry_px']:.2f} | "
                    f"{pos['leverage']:.0f}x"
                )
            elif old is None and size < 0:
                self._emit(CopySignal(
                    trader=wallet, coin=coin, symbol=symbol,
                    signal="COPY_OPEN_SHORT", size_usd=value,
                    entry_price=pos["entry_px"], leverage=pos["leverage"],
                    pnl_pct=pos["pnl_pct"],
                ))
                logger.info(
                    f"[HL-COPY] 📉 SHORT | {short} | {coin} | "
                    f"${value:,.0f} (info only)"
                )
            elif old is not None and size > 0 and old["size"] > 0:
                if old["size"] != 0:
                    change = abs(size - old["size"]) / abs(old["size"]) * 100
                else:
                    change = 100.0
                if size > old["size"] * 1.05:
                    self._emit(CopySignal(
                        trader=wallet, coin=coin, symbol=symbol,
                        signal="COPY_INCREASE", size_usd=value,
                        entry_price=pos["entry_px"],
                        leverage=pos["leverage"], pnl_pct=pos["pnl_pct"],
                    ))
                    logger.info(
                        f"[HL-COPY] ⬆️  INCREASE | {short} | {coin} | "
                        f"+{change:.0f}% → ${value:,.0f}"
                    )
                elif size < old["size"] * 0.95:
                    self._emit(CopySignal(
                        trader=wallet, coin=coin, symbol=symbol,
                        signal="COPY_DECREASE", size_usd=value,
                        entry_price=pos["entry_px"],
                        leverage=pos["leverage"], pnl_pct=pos["pnl_pct"],
                    ))
                    logger.info(
                        f"[HL-COPY] ⬇️  DECREASE | {short} | {coin} | "
                        f"-{change:.0f}% → ${value:,.0f}"
                    )

        # Closed positions
        for coin, old in old_pos.items():
            if coin not in new_pos and coin not in _UNSUPPORTED_COINS:
                symbol = hl_coin_to_binance_symbol(coin)
                if old["size"] > 0:
                    self._emit(CopySignal(
                        trader=wallet, coin=coin, symbol=symbol,
                        signal="COPY_CLOSE_LONG", size_usd=old["value_usd"],
                        entry_price=old["entry_px"],
                        leverage=old["leverage"], pnl_pct=old["pnl_pct"],
                    ))
                    logger.info(
                        f"[HL-COPY] 🔴 CLOSE | {short} | {coin} | "
                        f"PnL: {old['pnl_pct']:+.2f}%"
                    )

    def _emit(self, sig: CopySignal) -> None:
        self._signals.append(sig)
        if len(self._signals) > self._max_signals:
            self._signals = self._signals[-self._max_signals:]
        try:
            self.signal_queue.put_nowait(sig)
        except asyncio.QueueFull:
            logger.warning("[HL-COPY] Signal queue full")


    # ── WebSocket ─────────────────────────────────────────────────────────────

    async def _ws_loop(self) -> None:
        delay = 5
        while True:
            try:
                async with websockets.connect(HL_WS_URL) as ws:
                    self._ws_connected = True
                    delay = 5
                    logger.info("[HL-COPY] ✅ WebSocket connected")
                    subscribed: set[str] = set()
                    await self._ws_sync_subscriptions(ws, subscribed)

                    async def _resubscribe_loop() -> None:
                        # Wallets added later (dashboard activate / auto-discovery rescan)
                        # get subscribed here without needing a full reconnect.
                        while True:
                            await asyncio.sleep(10)
                            await self._ws_sync_subscriptions(ws, subscribed)

                    resub_task = asyncio.create_task(_resubscribe_loop())
                    try:
                        async for raw in ws:
                            try:
                                msg = json.loads(raw)
                                await self._handle_ws_message(msg)
                            except Exception as e:
                                logger.debug(f"[HL-COPY] WS parse: {e}")
                    finally:
                        resub_task.cancel()
            except Exception as e:
                self._ws_connected = False
                logger.warning(f"[HL-COPY] WS reconnect in {delay}s: {e}")
                await asyncio.sleep(delay)
                delay = min(delay * 2, 60)

    async def _ws_sync_subscriptions(self, ws, subscribed: set[str]) -> None:
        """Subscribe any newly-tracked wallet not yet subscribed on this connection."""
        for wallet in list(self._wallets):
            if wallet in subscribed:
                continue
            await ws.send(json.dumps({
                "method": "subscribe",
                "subscription": {"type": "userFills", "user": wallet},
            }))
            subscribed.add(wallet)

    async def _handle_ws_message(self, msg: dict) -> None:
        channel = msg.get("channel", "")
        data = msg.get("data", {})
        if channel == "userFills":
            user = data.get("user", "")
            if user in self._wallets:
                await asyncio.sleep(0.5)
                await self._poll_trader(user, emit_signals=True)

    # ── Trader Stats ──────────────────────────────────────────────────────────

    async def _fetch_all_trader_stats(self) -> None:
        for wallet in list(self._wallets):
            try:
                stats = await asyncio.to_thread(
                    self._api_trader_stats, wallet,
                )
                if stats:
                    self._trader_stats[wallet] = stats
                    short = f"{wallet[:6]}...{wallet[-4:]}"
                    logger.info(
                        f"[HL-COPY] 📊 {short} | "
                        f"Account: ${stats.total_pnl:,.0f} | "
                        f"Fills: {stats.total_trades}"
                    )
            except Exception as e:
                logger.error(f"[HL-COPY] Stats error: {e}")

    # ── Auto-Discovery ────────────────────────────────────────────────────────

    async def _rescan_loop(self) -> None:
        if not AUTO_DISCOVER:
            return
        while True:
            await asyncio.sleep(RESCAN_INTERVAL_SEC)
            try:
                logger.info("[HL-COPY] 🔄 Rescanning leaderboard for top scalpers...")
                self._discovering = True
                try:
                    discovered = await asyncio.to_thread(self._discover_scalpers)
                finally:
                    self._discovering = False
                pinned = set(MANUAL_WALLETS) | self._manual_active
                auto_wallets = [w for w in discovered if w not in pinned]
                new_wallets = list(dict.fromkeys(
                    list(MANUAL_WALLETS) + list(self._manual_active) + auto_wallets[:MAX_TRACKED_TRADERS]
                ))
                added = [w for w in new_wallets if w not in self._wallets]
                removed = [w for w in self._wallets if w not in new_wallets and w not in pinned]
                with self._wallets_lock:
                    self._wallets = new_wallets
                self._last_scan = time.time()
                for w in removed:
                    self._positions.pop(w, None)
                    self._trader_stats.pop(w, None)
                if added or removed:
                    await self._fetch_all_trader_stats()
                    logger.info(
                        f"[HL-COPY] Rescan complete — +{len(added)}/-{len(removed)} "
                        f"trader(s), {len(self._wallets)} tracked total"
                    )
                else:
                    logger.info("[HL-COPY] Rescan complete — trader list unchanged")
            except Exception as e:
                logger.error(f"[HL-COPY] Rescan error: {e}")

    def _discover_scalpers(self) -> list[str]:
        """Scan the HL leaderboard for active, high-frequency, high-winrate scalpers."""
        rows = self._fetch_leaderboard()
        if not rows:
            return []

        candidates: list[tuple[str, float]] = []
        for row in rows:
            wallet = row.get("ethAddress")
            if not wallet:
                continue
            acct_value = float(row.get("accountValue", "0") or "0")
            if acct_value < MIN_ACCOUNT_VALUE:
                continue
            day_pnl, day_vlm = self._leaderboard_window_metric(row, "day")
            if day_pnl <= 0 or day_vlm <= 0:
                continue
            candidates.append((wallet, day_vlm))

        # Prefer wallets with the highest daily volume (proxy for trade frequency)
        candidates.sort(key=lambda c: c[1], reverse=True)
        candidates = candidates[:DISCOVERY_CANDIDATE_POOL]
        logger.info(f"[HL-COPY] Evaluating {len(candidates)} leaderboard candidate(s)...")

        scored: list[tuple[str, TraderMetrics]] = []
        for wallet, _ in candidates:
            metrics = self._compute_trader_metrics(wallet)
            if metrics is None:
                continue
            if metrics.trades < MIN_TRADES:
                continue
            if metrics.win_rate < MIN_WIN_RATE:
                continue
            if metrics.avg_hold_sec > MAX_AVG_HOLD_SEC:
                continue
            if metrics.last_active_age > ACTIVE_WITHIN_SEC:
                continue
            scored.append((wallet, metrics))

        scored.sort(key=lambda x: x[1].win_rate, reverse=True)
        selected = scored[:MAX_TRACKED_TRADERS]
        for wallet, metrics in selected:
            short = f"{wallet[:6]}...{wallet[-4:]}"
            logger.info(
                f"[HL-COPY] ✅ Selected {short} | trades={metrics.trades} | "
                f"win_rate={metrics.win_rate:.0%} | "
                f"avg_hold={metrics.avg_hold_sec / 60:.1f}min"
            )
        return [w for w, _ in selected]

    @staticmethod
    def _fetch_leaderboard() -> list[dict]:
        try:
            resp = _hl_request("GET", LEADERBOARD_URL, timeout=20)
            if resp is None:
                return []
            resp.raise_for_status()
            return resp.json().get("leaderboardRows", [])
        except Exception as e:
            logger.error(f"[HL-COPY] Leaderboard fetch failed: {e}")
            return []

    @staticmethod
    def _leaderboard_window_metric(row: dict, window: str) -> tuple[float, float]:
        perf = dict(row.get("windowPerformances", []))
        w = perf.get(window, {})
        return float(w.get("pnl", "0") or "0"), float(w.get("vlm", "0") or "0")

    @staticmethod
    def _compute_trader_metrics(wallet: str) -> Optional[TraderMetrics]:
        """Derive trade count, win-rate and avg hold time from recent fills."""
        try:
            resp = _hl_request(
                "POST", HL_API_URL,
                json={"type": "userFills", "user": wallet},
                timeout=10,
            )
            if resp is None:
                return None
            resp.raise_for_status()
            fills = resp.json()
            if not isinstance(fills, list) or not fills:
                return None
        except Exception as e:
            logger.debug(f"[HL-COPY] Fills fetch failed for {wallet}: {e}")
            return None

        fills.sort(key=lambda f: f.get("time", 0))

        open_times: dict[str, float] = {}
        hold_durations: list[float] = []
        closed_trades = 0
        wins = 0

        for fill in fills:
            coin = fill.get("coin", "")
            direction = fill.get("dir", "")
            ts = float(fill.get("time", 0)) / 1000.0

            if direction.startswith("Open"):
                open_times[coin] = ts
            elif direction.startswith("Close"):
                closed_trades += 1
                if float(fill.get("closedPnl", "0") or "0") > 0:
                    wins += 1
                open_ts = open_times.pop(coin, None)
                if open_ts is not None:
                    hold_durations.append(ts - open_ts)

        if closed_trades == 0:
            return None

        avg_hold = (
            sum(hold_durations) / len(hold_durations)
            if hold_durations else MAX_AVG_HOLD_SEC + 1
        )
        last_active_age = time.time() - (fills[-1].get("time", 0) / 1000.0)

        return TraderMetrics(
            trades=closed_trades,
            win_rate=wins / closed_trades,
            avg_hold_sec=avg_hold,
            last_active_age=last_active_age,
        )

    @staticmethod
    def _api_trader_stats(wallet: str) -> Optional[TraderStats]:
        try:
            resp = _hl_request(
                "POST", HL_API_URL,
                json={"type": "clearinghouseState", "user": wallet},
                timeout=10,
            )
            if resp is None:
                return None
            resp.raise_for_status()
            data = resp.json()
            ms = data.get("marginSummary", {})
            acct_val = float(ms.get("accountValue", "0") or "0")

            resp2 = _hl_request(
                "POST", HL_API_URL,
                json={"type": "userFills", "user": wallet},
                timeout=10,
            )
            if resp2 is None:
                return None
            resp2.raise_for_status()
            fills = resp2.json()

            return TraderStats(
                wallet=wallet, total_pnl=acct_val,
                total_trades=len(fills) if isinstance(fills, list) else 0,
                last_update=time.time(),
            )
        except Exception as e:
            logger.debug(f"[HL-COPY] Stats API error: {e}")
            return None

    # ── Low-Level API ─────────────────────────────────────────────────────────

    @staticmethod
    def _api_clearinghouse_state(wallet: str) -> Optional[dict]:
        try:
            resp = _hl_request(
                "POST", HL_API_URL,
                json={"type": "clearinghouseState", "user": wallet},
                timeout=10,
            )
            if resp is None:
                return None
            resp.raise_for_status()
            return resp.json()
        except Exception as e:
            logger.debug(f"[HL-COPY] API error: {e}")
            return None

    # ── Cleanup ───────────────────────────────────────────────────────────────

    async def cleanup_loop(self) -> None:
        while True:
            await asyncio.sleep(60)
            st = self.status()
            logger.info(
                f"[HL-COPY] Status | WS:{st['ws_connected']} | "
                f"Traders:{st['tracked_traders']} | "
                f"Pos:{st['total_positions']} | "
                f"Signals:{st['fresh_signals']}"
            )
            for wallet in list(self._wallets):
                positions = self._positions.get(wallet, {})
                if positions:
                    short = f"{wallet[:6]}...{wallet[-4:]}"
                    coins = ", ".join(
                        f"{c} {'L' if p['size']>0 else 'S'} "
                        f"${p['value_usd']:,.0f}"
                        for c, p in positions.items()
                    )
                    logger.info(f"[HL-COPY]   {short}: {coins}")

