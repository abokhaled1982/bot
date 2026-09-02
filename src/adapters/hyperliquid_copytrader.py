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

from src.utils import trader_store

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
# Normale Profi-Trader dürfen Positionen im Mittel bis zu 24 Stunden halten.
MAX_AVG_HOLD_SEC = float(os.getenv("HL_MAX_AVG_HOLD_SEC", "86400"))
# Trader must have closed a trade within this window to count as "active"
ACTIVE_WITHIN_SEC = float(os.getenv("HL_ACTIVE_WITHIN_HOURS", "24")) * 3600
# How many leaderboard candidates to fetch detailed fill stats for
DISCOVERY_CANDIDATE_POOL = int(os.getenv("HL_DISCOVERY_POOL", "25"))
# userFills is the heaviest HL endpoint (weight 20 + 1 per 20 returned items, up to
# 2000 items — i.e. up to 120 weight per call against the 1200/min IP budget).
# Caching per-wallet results avoids refetching the same top-leaderboard wallets on
# every search/rescan, which is what actually caused repeated 429s, not the API itself.
METRICS_CACHE_TTL = float(os.getenv("HL_METRICS_CACHE_TTL", "300"))

LEADERBOARD_URL = "https://stats-data.hyperliquid.xyz/Mainnet/leaderboard"
LEADERBOARD_WINDOWS = ("day", "week", "month", "allTime")

# Seconds-per-window — used to restrict `userFills` metrics (win-rate, trades, hold)
# to the same window the user picked for PnL/Volume. `None` means "all HL keeps".
LEADERBOARD_WINDOW_SEC: dict[str, Optional[float]] = {
    "day":     86400.0,
    "week":    604800.0,
    "month":   2592000.0,
    "allTime": None,
}

# Minimum spacing between HL API calls to avoid 429 rate limiting
MIN_REQUEST_INTERVAL = float(os.getenv("HL_MIN_REQUEST_INTERVAL", "2.0"))

# Public API health, updated by _hl_request — surfaced on the dashboard so users
# can tell "still loading" from "rate-limited" from "network down".
_API_HEALTH_LOCK = threading.Lock()
_api_health_state: str = "ok"  # ok | rate_limited | unreachable


def _set_api_health(state: str) -> None:
    global _api_health_state
    with _API_HEALTH_LOCK:
        _api_health_state = state


def get_api_health() -> str:
    with _API_HEALTH_LOCK:
        return _api_health_state

# Gewählte Trader, Beträge und Anpinnung liegen in der SQLite-DB
# (src/utils/trader_store.py) — nur so sieht der laufende Bot-Prozess eine
# Änderung, die im separaten Dashboard-Prozess gemacht wurde.
# Wie oft der Bot diesen Store neu einliest:
STORE_SYNC_INTERVAL = float(os.getenv("HL_STORE_SYNC_SEC", "3"))
# Wie oft der Bot seine Tracking-Telemetrie zurück in die DB schreibt:
STATE_PUBLISH_INTERVAL = float(os.getenv("HL_STATE_PUBLISH_SEC", "3"))

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
            _set_api_health("unreachable")
            return None
        if resp.status_code == 429:
            retry_after = float(resp.headers.get("Retry-After", backoff))
            logger.debug(f"[HL] 429 rate limited — backing off {retry_after:.1f}s")
            _set_api_health("rate_limited")
            time.sleep(retry_after)
            backoff = min(backoff * 2, 10.0)
            continue
        if resp.ok:
            _set_api_health("ok")
        return resp
    _set_api_health("rate_limited")
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
    """Aus geschlossenen Fills abgeleitete Qualitäts- und Copy-Kennzahlen."""
    trades:           int
    win_rate:         float
    avg_hold_sec:      float
    last_active_age:  float
    net_pnl:           float
    profit_factor:     float
    max_drawdown:      float
    max_drawdown_pct:  float
    active_days:       int
    long_trades:       int
    long_net_pnl:      float
    long_profit_factor: float
    long_share:        float



class HyperliquidCopyTrader:
    """
    Monitors Hyperliquid traders and emits copy-signals.

    Usage:
        adapter = HyperliquidCopyTrader()
        asyncio.create_task(adapter.start())
        signal = await adapter.signal_queue.get()
        # → signal.symbol = "BTCUSDT" → execute on Binance
    """

    def __init__(self, publish_state: bool = False) -> None:
        # publish_state=True nur im Bot-Prozess: dann schreibt dieser Adapter
        # seine Tracking-Telemetrie in die DB, aus der das Dashboard liest.
        self._publish_state = publish_state
        trader_store.init_db()

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
        self._manual_active: set[str] = set()
        # Wallets tracked purely for observation (Focus tab). WS+poll, but no copy-signals.
        self._observed_only: set[str] = set()
        # Currently focused wallet — persisted across restarts and page reloads
        self._focus_wallet: Optional[str] = None
        # Per-wallet copy-size override in USD ({} means use global default)
        self._copy_sizes: dict[str, float] = {}
        # Dynamic trader list (env manual + dashboard-activated + focus + auto-discovered)
        self._wallets: list[str] = list(MANUAL_WALLETS)
        # Wallets aus der Auto-Discovery — nur Beobachtung, nie Copy-Quelle
        self._auto_wallets: list[str] = []
        # Per-Wallet-Nachweis, dass Tracking wirklich läuft (Dashboard-Spalte
        # "Tracking" und Konsolen-Diagnose speisen sich hieraus).
        self._telemetry: dict[str, dict] = {}
        self._seed_env_wallets()
        self._sync_from_store(initial=True)
        self._last_scan = 0.0
        # Runtime-adjustable settings (dashboard "Einstellungen" tab), default from env
        self._poll_interval = POLL_INTERVAL
        self._min_copy_size_usd = MIN_COPY_SIZE_USD
        # (wallet, since_ts) -> (fetched_at, TraderMetrics) — since_ts=None means lifetime
        self._metrics_cache: dict[tuple[str, Optional[float]], tuple[float, TraderMetrics]] = {}
        # wallet -> (fetched_at, raw fills) — shared across windows to avoid re-hitting the
        # heaviest HL endpoint (Focus tab computes 4 windows from the same fetch)
        self._fills_cache: dict[str, tuple[float, list[dict]]] = {}

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
            "metrics_cache_ttl_min": METRICS_CACHE_TTL / 60,
        }

    def set_poll_interval(self, seconds: float) -> None:
        self._poll_interval = max(1.0, float(seconds))

    def set_min_copy_size(self, usd: float) -> None:
        self._min_copy_size_usd = max(0.0, float(usd))

    # ── Trader-Auswahl (DB-gestützt, prozessübergreifend) ────────────────────
    #
    # Alle Schreibzugriffe gehen in die SQLite-Tabelle `copy_traders`. Der
    # Bot-Prozess liest sie alle STORE_SYNC_INTERVAL Sekunden neu ein, deshalb
    # wirkt eine Änderung im Dashboard ohne Neustart.

    @staticmethod
    def _seed_env_wallets() -> None:
        """HL_TRADER_WALLETS aus der .env als kopierte Trader in die DB spiegeln."""
        for wallet in MANUAL_WALLETS:
            trader_store.upsert_trader(wallet, is_copied=True, source="env")

    def _rebuild_wallet_list(self) -> None:
        """`_wallets` neu aufbauen: gewählte Trader zuerst, dann Auto-Discovery.

        Muss unter `_wallets_lock` laufen. `_observed_only` enthält danach genau
        die Wallets, die zwar getrackt, aber NICHT kopiert werden — daran hängt
        sowohl die Signal-Freigabe als auch das Konsolen-Logging.
        """
        chosen = list(dict.fromkeys(list(MANUAL_WALLETS) + sorted(self._manual_active)))
        if self._focus_wallet and self._focus_wallet not in chosen:
            chosen.append(self._focus_wallet)
        auto = [w for w in self._auto_wallets if w not in chosen]
        self._wallets = chosen + auto
        observed = set(auto)
        if (self._focus_wallet
                and self._focus_wallet not in self._manual_active
                and self._focus_wallet not in MANUAL_WALLETS):
            observed.add(self._focus_wallet)
        self._observed_only = observed

    def _sync_from_store(self, initial: bool = False) -> None:
        """Trader-Auswahl aus der DB übernehmen und Änderungen protokollieren.

        Das ist die einzige Stelle, an der `_manual_active`, `_focus_wallet`
        und `_copy_sizes` im laufenden Betrieb gesetzt werden.
        """
        rows = trader_store.list_traders()
        copied = {r["wallet"] for r in rows if r["is_copied"]}
        focus = next((r["wallet"] for r in rows if r["is_focus"]), None)
        sizes = {
            r["wallet"]: float(r["size_usdt"])
            for r in rows if r["size_usdt"] and float(r["size_usdt"]) > 0
        }

        with self._wallets_lock:
            added = copied - self._manual_active
            removed = self._manual_active - copied
            resized = {
                w: s for w, s in sizes.items()
                if not initial and self._copy_sizes.get(w) != s and w not in added
            }
            focus_changed = (not initial) and focus != self._focus_wallet
            self._manual_active = copied
            self._focus_wallet = focus
            self._copy_sizes = sizes
            self._rebuild_wallet_list()
            stale = [w for w in list(self._positions) if w not in self._wallets]

        for wallet in stale:
            self._positions.pop(wallet, None)
            self._telemetry.pop(wallet, None)

        if initial:
            return
        # Nur der Bot-Prozess protokolliert Auswahl-Ereignisse. Das Dashboard
        # ruft dieselbe Methode auf und würde sonst jede Zeile verdoppeln.
        if not self._publish_state:
            return

        # Konsole: nur Auswahl-Ereignisse, kein Rauschen von beobachteten Wallets.
        for wallet in sorted(added):
            size = sizes.get(wallet)
            amount = f"${size:,.0f}" if size else "Standardbetrag"
            logger.success(
                f"[HL-COPY] ➕ Trader übernommen: {self._short(wallet)} | "
                f"{amount}/Trade | Tracking + Binance-Pipeline aktiv"
            )
            trader_store.log_event(
                "TRADER_ADDED", f"Copy aktiviert mit {amount}/Trade",
                wallet=wallet, level="success",
            )
        for wallet in sorted(removed):
            logger.info(
                f"[HL-COPY] ➖ Trader entfernt: {self._short(wallet)} | "
                f"kein Copy-Trading mehr"
            )
            trader_store.log_event(
                "TRADER_REMOVED", "Copy deaktiviert", wallet=wallet,
            )
        for wallet, size in sorted(resized.items()):
            logger.info(
                f"[HL-COPY] ✏️  Betrag geändert: {self._short(wallet)} → ${size:,.0f}/Trade"
            )
            trader_store.log_event(
                "TRADER_UPDATED", f"Einsatz auf ${size:,.0f}/Trade gesetzt",
                wallet=wallet,
            )
        if focus_changed:
            logger.info(
                f"[HL-COPY] 📌 Angepinnt: {self._short(focus) if focus else 'keiner'}"
            )

    async def _sync_loop(self) -> None:
        while True:
            await asyncio.sleep(STORE_SYNC_INTERVAL)
            try:
                await asyncio.to_thread(self._sync_from_store)
            except Exception as e:
                logger.error(f"[HL-COPY] Store-Sync fehlgeschlagen: {e}")

    def refresh(self) -> None:
        """Trader-Auswahl aus der DB neu einlesen.

        Für Prozesse ohne laufenden `_sync_loop` — vor allem das Dashboard,
        das den Adapter nur zum Schreiben und für den Scanner benutzt.
        """
        self._sync_from_store()

    @staticmethod
    def _short(wallet: str) -> str:
        return f"{wallet[:6]}...{wallet[-4:]}" if len(wallet) > 12 else wallet

    def activate_wallet(self, wallet: str) -> bool:
        """Trader für Copy-Trading aktivieren. True, wenn er neu dazukam."""
        wallet = wallet.strip()
        if not wallet:
            return False
        is_new = wallet not in self._manual_active
        trader_store.upsert_trader(wallet, is_copied=True, source="dashboard")
        self._sync_from_store()
        if is_new:
            stats = self._api_trader_stats(wallet)
            if stats:
                self._trader_stats[wallet] = stats
                trader_store.update_trader_stats(
                    wallet, account_usd=stats.total_pnl, trades=stats.total_trades,
                )
        return is_new

    def deactivate_wallet(self, wallet: str) -> None:
        """Copy-Trading für einen Trader ausschalten; Anpinnung bleibt bestehen."""
        trader_store.upsert_trader(wallet, is_copied=False)
        self._sync_from_store()

    def remove_trader(self, wallet: str) -> None:
        """Trader vollständig aus Auswahl, Betrag, Anpinnung und Tracking löschen."""
        wallet = wallet.strip()
        trader_store.remove_trader(wallet)
        self._sync_from_store()
        self._positions.pop(wallet, None)
        self._trader_stats.pop(wallet, None)
        self._telemetry.pop(wallet, None)

    def get_focus_wallet(self) -> Optional[str]:
        return self._focus_wallet

    def set_focus_wallet(self, wallet: str) -> None:
        """Trader anpinnen — wird live getrackt (WS + Poll), löst aber allein
        noch kein Copy-Trading aus."""
        if not wallet.strip():
            return
        trader_store.set_focus(wallet.strip())
        self._sync_from_store()

    def clear_focus_wallet(self) -> None:
        trader_store.set_focus(None)
        self._sync_from_store()

    def _should_emit_signals_for(self, wallet: str) -> bool:
        """Nur bewusst gewählte Trader werden kopiert.

        Auto-entdeckte Wallets sind reine Beobachtung, bis der Nutzer sie im
        Dashboard aktiviert — sonst würde jeder Leaderboard-Treffer echte
        Orders auslösen.
        """
        if wallet in self._observed_only:
            return False
        return wallet in MANUAL_WALLETS or wallet in self._manual_active

    def is_copied(self, wallet: str) -> bool:
        """Öffentliche Prüfung für die Pipeline, bevor eine Order ausgelöst wird."""
        return self._should_emit_signals_for(wallet)

    def get_copy_size(self, wallet: str) -> Optional[float]:
        """Einsatz pro Trade in USD für diesen Trader, sonst None (= Default)."""
        return self._copy_sizes.get(wallet.strip())

    def set_copy_size(self, wallet: str, size_usd: float) -> None:
        wallet = wallet.strip()
        if not wallet or size_usd <= 0:
            return
        trader_store.upsert_trader(wallet, size_usdt=float(size_usd))
        self._sync_from_store()

    def clear_copy_size(self, wallet: str) -> None:
        trader_store.upsert_trader(wallet.strip(), size_usdt=0)
        self._sync_from_store()

    # ── Tracking-Nachweis ────────────────────────────────────────────────────

    def _tel(self, wallet: str) -> dict:
        """Telemetrie-Datensatz eines Wallets (legt ihn bei Bedarf an)."""
        return self._telemetry.setdefault(wallet, {
            "ws_subscribed": False, "ws_sub_at": 0.0,
            "last_fill_at": 0.0, "fill_count": 0,
            "last_poll_at": 0.0, "poll_count": 0, "poll_error": "",
            "signal_count": 0, "last_signal_at": 0.0,
        })

    def get_tracking(self) -> dict[str, dict]:
        """Pro Wallet: WS-Subscription, letzter Fill, Poll- und Signal-Zähler.

        Der Bot veröffentlicht dasselbe zyklisch in `tracker_state`, damit das
        Dashboard (eigener Prozess!) den echten Tracking-Zustand sieht.
        """
        out: dict[str, dict] = {}
        for wallet in list(self._wallets):
            tel = dict(self._tel(wallet))
            tel["wallet"] = wallet
            tel["is_copied"] = self._should_emit_signals_for(wallet)
            tel["open_positions"] = len(self._positions.get(wallet, {}))
            out[wallet] = tel
        return out

    def _publish_tracker_state(self) -> None:
        """Telemetrie der gewählten Trader in die DB schreiben."""
        rows = []
        for wallet in list(self._wallets):
            if wallet not in self._manual_active and wallet != self._focus_wallet \
                    and wallet not in MANUAL_WALLETS:
                continue  # Auto-Discovery-Kandidaten interessieren das UI nicht
            tel = dict(self._tel(wallet))
            positions = dict(self._positions.get(wallet, {}))
            stats = self._trader_stats.get(wallet)
            tel.update({
                "wallet": wallet,
                "is_copied": self._should_emit_signals_for(wallet),
                "open_positions": len(positions),
                "positions": positions,
                "account_usd": stats.total_pnl if stats else 0.0,
            })
            rows.append(tel)
        trader_store.publish_tracker_state(rows)

    async def _publish_loop(self) -> None:
        while True:
            await asyncio.sleep(STATE_PUBLISH_INTERVAL)
            try:
                await asyncio.to_thread(self._publish_tracker_state)
            except Exception as e:
                logger.debug(f"[HL-COPY] Tracker-State publish: {e}")


    def list_leaderboard(
        self, window: str = "day", limit: int = 30,
        min_trades: int = 0, min_win_rate: float = 0.0,
        min_avg_hold_sec: float = 120.0,
        max_avg_hold_sec: Optional[float] = None,
        min_account_value: float = 0.0,
        verified_only: bool = True,
        progress_cb: Optional[callable] = None,
    ) -> list[dict]:
        """Nur quantitativ verifizierte, Spot-Copy-taugliche Trader liefern.

        Ranking-PnL stammt aus dem gewählten Fenster. Die Verifikation verwendet
        immer 30 Tage, damit ein guter einzelner Tag keinen Trader qualifiziert.
        "Verifiziert" bezeichnet ausschließlich die messbare Handelsqualität der
        öffentlichen HL-Wallet, nicht die Identität einer Person.
        """
        rows = self._fetch_leaderboard()
        if not rows:
            return []

        window_sec = LEADERBOARD_WINDOW_SEC.get(window)
        since_ts = time.time() - window_sec if window_sec is not None else None

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
            day_pnl, _ = self._leaderboard_window_metric(row, "day")
            week_pnl, _ = self._leaderboard_window_metric(row, "week")
            month_pnl, _ = self._leaderboard_window_metric(row, "month")
            ranked.append((wallet, pnl, {
                "account_value": acct_value, "pnl": pnl, "vlm": vlm,
                "day_pnl": day_pnl, "week_pnl": week_pnl,
                "month_pnl": month_pnl,
            }))

        ranked.sort(key=lambda r: r[1], reverse=True)
        candidates = ranked[:limit]

        results = []
        for i, (wallet, _, info) in enumerate(candidates, 1):
            if progress_cb:
                progress_cb(i, len(candidates), wallet)
            self._trader_stats[wallet] = TraderStats(
                wallet=wallet, total_pnl=info["account_value"],
            )
            display_metrics = self._compute_trader_metrics(wallet, since_ts=since_ts)
            verification_since = time.time() - LEADERBOARD_WINDOW_SEC["month"]
            metrics = self._compute_trader_metrics(wallet, since_ts=verification_since)
            if metrics is None or display_metrics is None:
                continue
            verified, reasons = self._verify_copy_candidate(
                metrics,
                day_pnl=info["day_pnl"],
                week_pnl=info["week_pnl"],
                month_pnl=info["month_pnl"],
                min_trades=max(30, min_trades),
                min_win_rate=max(0.45, min_win_rate),
                min_avg_hold_sec=min_avg_hold_sec,
                max_avg_hold_sec=max_avg_hold_sec or 86_400.0,
            )
            score = self._copy_quality_score(metrics, info)
            candidate = {
                "wallet":           wallet,
                "account_value":    info["account_value"],
                "window_pnl":       info["pnl"],
                "window_volume":    info["vlm"],
                "day_pnl":          info["day_pnl"],
                "week_pnl":         info["week_pnl"],
                "month_pnl":        info["month_pnl"],
                "trades":           metrics.trades,
                "win_rate":         metrics.win_rate,
                "avg_hold_sec":     metrics.avg_hold_sec,
                "last_active_age":  metrics.last_active_age,
                "profit_factor":    metrics.profit_factor,
                "max_drawdown":     metrics.max_drawdown,
                "max_drawdown_pct": metrics.max_drawdown_pct,
                "active_days":      metrics.active_days,
                "long_trades":      metrics.long_trades,
                "long_net_pnl":     metrics.long_net_pnl,
                "long_profit_factor": metrics.long_profit_factor,
                "long_share":       metrics.long_share,
                "quality_score":    score,
                "verified":         verified,
                "verification_reasons": reasons,
                "metrics_window":   window,
                "verification_window": "month",
                "metrics_source":   "hl_public_api",
            }
            if not verified_only or verified:
                results.append(candidate)
        return sorted(results, key=lambda result: result["quality_score"], reverse=True)

    def get_trader_focus(self, wallet: str) -> Optional[dict]:
        """Deep-dive snapshot of one trader for the dashboard Focus tab.

        Returns metrics across all 4 HL windows (1d/7d/30d/all), current
        positions (fetched on-demand if not already tracked) and recent fills.
        All windows share one `userFills` fetch via the fills cache, so this
        is cheaper than calling `list_leaderboard` per window.
        """
        wallet = wallet.strip()
        if not wallet:
            return None

        metrics_by_window: dict[str, Optional[TraderMetrics]] = {}
        for w in LEADERBOARD_WINDOWS:
            w_sec = LEADERBOARD_WINDOW_SEC.get(w)
            since = time.time() - w_sec if w_sec is not None else None
            metrics_by_window[w] = self._compute_trader_metrics(wallet, since_ts=since)

        positions = dict(self._positions.get(wallet, {}))
        if not positions:
            data = self._api_clearinghouse_state(wallet)
            if data:
                for ap in data.get("assetPositions", []):
                    pos = ap.get("position", {})
                    coin = pos.get("coin", "")
                    size = float(pos.get("sizeDecimal", pos.get("size", "0")) or "0")
                    if abs(size) < 1e-12:
                        continue
                    entry = float(pos.get("entryPx", "0") or "0")
                    lev = pos.get("leverage", {})
                    lev_val = float(lev.get("value", 1) if isinstance(lev, dict) else (lev or 1))
                    upnl = float(pos.get("unrealizedPnl", "0") or "0")
                    value_usd = float(pos.get("positionValue", "0") or "0")
                    cost = abs(size) * entry
                    pnl_pct = (upnl / cost * 100) if cost > 0 else 0.0
                    positions[coin] = {
                        "coin": coin, "size": size, "entry_px": entry,
                        "leverage": lev_val, "pnl_pct": pnl_pct,
                        "value_usd": value_usd, "unrealized_pnl": upnl,
                    }

        stats = self._trader_stats.get(wallet) or self._api_trader_stats(wallet)
        recent_fills = self._fetch_user_fills(wallet) or []
        recent_fills = sorted(recent_fills, key=lambda f: f.get("time", 0), reverse=True)[:25]

        return {
            "wallet":            wallet,
            "account_value":     stats.total_pnl if stats else 0.0,
            "total_fills":       stats.total_trades if stats else 0,
            "metrics_by_window": metrics_by_window,
            "positions":         positions,
            "recent_fills":      recent_fills,
            "is_tracked":        wallet in self._wallets,
            "is_active":         wallet in self._manual_active,
        }

    def status(self) -> dict:
        total_positions = sum(len(p) for p in self._positions.values())
        copied = [w for w in self._wallets if self._should_emit_signals_for(w)]
        ws_ok = sum(1 for w in copied if self._tel(w)["ws_subscribed"])
        return {
            "connected":       self._connected,
            "ws_connected":    self._ws_connected,
            "discovering":     self._discovering,
            "tracked_traders": len(self._wallets),
            "copied_traders":  len(copied),
            "ws_subscribed":   ws_ok,
            "total_positions": total_positions,
            "fresh_signals":   len([s for s in self._signals if s.is_fresh]),
            "poll_count":      self._poll_count,
            "last_poll_age":   round(time.time() - self._last_poll, 1) if self._last_poll else None,
            "auto_discover":   AUTO_DISCOVER,
            "last_scan_age":   round(time.time() - self._last_scan, 1) if self._last_scan else None,
            "next_scan_in":    round(RESCAN_INTERVAL_SEC - (time.time() - self._last_scan), 1) if self._last_scan else None,
            "api_health":      get_api_health(),
        }

    # ── Startup ───────────────────────────────────────────────────────────────

    async def start(self) -> None:
        # Auto-discover traders if no manual wallets and auto-discover is on
        if AUTO_DISCOVER:
            logger.debug("[HL-COPY] Auto-Discovery aktiv — scanne Leaderboard …")
            self._discovering = True
            try:
                discovered = await asyncio.to_thread(self._discover_scalpers)
            finally:
                self._discovering = False
            with self._wallets_lock:
                self._auto_wallets = list(discovered)
                self._rebuild_wallet_list()
            self._last_scan = time.time()

        # Ohne Trader läuft trotzdem der Sync-Loop weiter: sobald im Dashboard
        # einer übernommen wird, startet Tracking + Pipeline ohne Neustart.
        copied = [w for w in self._wallets if self._should_emit_signals_for(w)]
        if copied:
            selected = ", ".join(
                f"{self._short(wallet)} (${self._copy_sizes[wallet]:,.0f})"
                if wallet in self._copy_sizes else self._short(wallet)
                for wallet in copied
            )
            logger.info(f"[INIT] Gewählte Trader: {selected}")
        else:
            logger.info("[INIT] Kein Trader ausgewählt")
        if self._auto_wallets:
            logger.debug(
                f"[HL-COPY] {len(self._auto_wallets)} auto-entdeckte Wallet(s) "
                f"nur zur Beobachtung getrackt"
            )

        await self._fetch_all_trader_stats()
        await self._poll_all_positions(emit_signals=False)
        self._connected = True

        loops = [
            self._poll_loop(),
            self._ws_loop(),
            self._rescan_loop(),
            self._sync_loop(),
        ]
        if self._publish_state:
            loops.append(self._publish_loop())
        await asyncio.gather(*loops)

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
                self._tel(wallet)["poll_error"] = str(e)
                logger.error(f"[HL-COPY] Error polling {self._short(wallet)}: {e}")
        self._last_poll = time.time()
        self._poll_count += 1

    async def _poll_trader(self, wallet: str, emit_signals: bool) -> None:
        data = await asyncio.to_thread(self._api_clearinghouse_state, wallet)
        tel = self._tel(wallet)
        if data is None:
            tel["poll_error"] = "clearinghouseState nicht erreichbar"
            return
        tel["poll_error"] = ""
        tel["last_poll_at"] = time.time()
        tel["poll_count"] += 1

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
        # Watch-only wallets still update state and the dashboard, but stay off
        # the INFO console — only copied traders are console-worthy.
        emit_signals = self._should_emit_signals_for(wallet)
        old_pos = self._positions.get(wallet, {})
        short = f"{wallet[:6]}...{wallet[-4:]}" if len(wallet) > 12 else wallet
        tag = "COPY" if emit_signals else "WATCH"
        log = logger.info if emit_signals else logger.debug

        for coin, pos in new_pos.items():
            if coin in _UNSUPPORTED_COINS:
                continue
            symbol = hl_coin_to_binance_symbol(coin)
            old = old_pos.get(coin)
            size = pos["size"]
            value = pos["value_usd"]

            if old is None and size > 0 and value >= self._min_copy_size_usd:
                if emit_signals:
                    self._emit(CopySignal(
                        trader=wallet, coin=coin, symbol=symbol,
                        signal="COPY_OPEN_LONG", size_usd=value,
                        entry_price=pos["entry_px"], leverage=pos["leverage"],
                        pnl_pct=pos["pnl_pct"],
                    ))
                log(
                    f"[HL-{tag}] 📈 OPEN LONG | {short} | {coin} | "
                    f"${value:,.0f} @ ${pos['entry_px']:.4f} | "
                    f"{pos['leverage']:.0f}x"
                )
            elif old is None and size < 0:
                if emit_signals:
                    self._emit(CopySignal(
                        trader=wallet, coin=coin, symbol=symbol,
                        signal="COPY_OPEN_SHORT", size_usd=value,
                        entry_price=pos["entry_px"], leverage=pos["leverage"],
                        pnl_pct=pos["pnl_pct"],
                    ))
                log(
                    f"[HL-{tag}] 📉 OPEN SHORT | {short} | {coin} | "
                    f"${value:,.0f} @ ${pos['entry_px']:.4f} | "
                    f"{pos['leverage']:.0f}x (info only)"
                )
            elif old is not None and size > 0 and old["size"] > 0:
                if old["size"] != 0:
                    change = abs(size - old["size"]) / abs(old["size"]) * 100
                else:
                    change = 100.0
                if size > old["size"] * 1.05:
                    if emit_signals:
                        self._emit(CopySignal(
                            trader=wallet, coin=coin, symbol=symbol,
                            signal="COPY_INCREASE", size_usd=value,
                            entry_price=pos["entry_px"],
                            leverage=pos["leverage"], pnl_pct=pos["pnl_pct"],
                        ))
                    log(
                        f"[HL-{tag}] ⬆️  INCREASE | {short} | {coin} | "
                        f"+{change:.0f}% → ${value:,.0f}"
                    )
                elif size < old["size"] * 0.95:
                    if emit_signals:
                        self._emit(CopySignal(
                            trader=wallet, coin=coin, symbol=symbol,
                            signal="COPY_DECREASE", size_usd=value,
                            entry_price=pos["entry_px"],
                            leverage=pos["leverage"], pnl_pct=pos["pnl_pct"],
                        ))
                    log(
                        f"[HL-{tag}] ⬇️  DECREASE | {short} | {coin} | "
                        f"-{change:.0f}% → ${value:,.0f}"
                    )

        # Closed positions
        for coin, old in old_pos.items():
            if coin not in new_pos and coin not in _UNSUPPORTED_COINS:
                symbol = hl_coin_to_binance_symbol(coin)
                if old["size"] > 0:
                    if emit_signals:
                        self._emit(CopySignal(
                            trader=wallet, coin=coin, symbol=symbol,
                            signal="COPY_CLOSE_LONG", size_usd=old["value_usd"],
                            entry_price=old["entry_px"],
                            leverage=old["leverage"], pnl_pct=old["pnl_pct"],
                        ))
                    log(
                        f"[HL-{tag}] 🔴 CLOSE LONG | {short} | {coin} | "
                        f"PnL: {old['pnl_pct']:+.2f}%"
                    )
                elif old["size"] < 0:
                    log(
                        f"[HL-{tag}] 🔴 CLOSE SHORT | {short} | {coin} | "
                        f"PnL: {old['pnl_pct']:+.2f}% (info only)"
                    )

    def _emit(self, sig: CopySignal) -> None:
        self._signals.append(sig)
        if len(self._signals) > self._max_signals:
            self._signals = self._signals[-self._max_signals:]
        tel = self._tel(sig.trader)
        tel["signal_count"] += 1
        tel["last_signal_at"] = sig.timestamp
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
                    logger.debug("[HL-COPY] WebSocket connected")
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
                for tel in self._telemetry.values():
                    tel["ws_subscribed"] = False
                logger.debug(f"[HL] WS reconnect in {delay}s: {e}")
                await asyncio.sleep(delay)
                delay = min(delay * 2, 60)

    async def _ws_sync_subscriptions(self, ws, subscribed: set[str]) -> None:
        """Subscriptions an die aktuelle Trader-Liste angleichen.

        Neu übernommene Trader werden hier abonniert (ohne Reconnect), entfernte
        wieder abgemeldet — sonst liefe das Tracking für gelöschte Trader weiter.
        """
        wanted = set(self._wallets)

        for wallet in wanted - subscribed:
            await ws.send(json.dumps({
                "method": "subscribe",
                "subscription": {"type": "userFills", "user": wallet},
            }))
            subscribed.add(wallet)
            tel = self._tel(wallet)
            tel["ws_subscribed"] = True
            tel["ws_sub_at"] = time.time()
            if self._should_emit_signals_for(wallet):
                logger.debug(f"[HL-COPY] WebSocket abonniert: {self._short(wallet)}")

        for wallet in subscribed - wanted:
            try:
                await ws.send(json.dumps({
                    "method": "unsubscribe",
                    "subscription": {"type": "userFills", "user": wallet},
                }))
            except Exception as e:
                logger.debug(f"[HL-COPY] Unsubscribe {self._short(wallet)}: {e}")
            subscribed.discard(wallet)
            self._tel(wallet)["ws_subscribed"] = False

    async def _handle_ws_message(self, msg: dict) -> None:
        channel = msg.get("channel", "")
        data = msg.get("data", {})
        if channel == "userFills":
            user = data.get("user", "")
            if user not in self._wallets:
                return
            short = f"{user[:6]}...{user[-4:]}"
            is_copied = self._should_emit_signals_for(user)
            tel = self._tel(user)
            for f in data.get("fills", []) or []:
                try:
                    coin = f.get("coin", "?")
                    px = float(f.get("px", "0") or "0")
                    sz = float(f.get("sz", "0") or "0")
                    side = f.get("side", "")  # "B" = buy, "A" = sell
                    direction = f.get("dir", "")
                    closed_pnl = float(f.get("closedPnl", "0") or "0")
                    arrow = "🟢 BUY " if side == "B" else "🔴 SELL"
                    tel["fill_count"] += 1
                    tel["last_fill_at"] = float(f.get("time", 0) or 0) / 1000.0 or time.time()
                    if is_copied:
                        logger.debug(
                            f"[HL-COPY] Fill {arrow} {short} | {coin} | "
                            f"{sz:.4f} @ ${px:.4f} | {direction} | "
                            f"PnL: ${closed_pnl:+,.2f}"
                        )
                except Exception as e:
                    if is_copied:
                        logger.debug(f"[HL-COPY] Fill konnte nicht gelesen werden: {e}")
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
                    is_copied = self._should_emit_signals_for(wallet)
                    if is_copied or wallet == self._focus_wallet:
                        await asyncio.to_thread(
                            trader_store.update_trader_stats, wallet,
                            account_usd=stats.total_pnl, trades=stats.total_trades,
                        )
                    logger.debug(
                        f"[HL-COPY] 📊 {self._short(wallet)} | "
                        f"Account: ${stats.total_pnl:,.0f} | "
                        f"Fills: {stats.total_trades}"
                    )
            except Exception as e:
                logger.debug(f"[HL] Stats error: {e}")

    # ── Auto-Discovery ────────────────────────────────────────────────────────

    async def _rescan_loop(self) -> None:
        if not AUTO_DISCOVER:
            return
        while True:
            await asyncio.sleep(RESCAN_INTERVAL_SEC)
            try:
                logger.debug("[HL-COPY] Rescanning leaderboard for top scalpers...")
                self._discovering = True
                try:
                    discovered = await asyncio.to_thread(self._discover_scalpers)
                finally:
                    self._discovering = False
                with self._wallets_lock:
                    before = set(self._wallets)
                    self._auto_wallets = list(discovered)[:MAX_TRACKED_TRADERS]
                    self._rebuild_wallet_list()
                    removed = before - set(self._wallets)
                self._last_scan = time.time()
                for w in removed:
                    self._positions.pop(w, None)
                    self._trader_stats.pop(w, None)
                    self._telemetry.pop(w, None)
                # Auto-Discovery ist reine Beobachtung — kein Konsolen-Rauschen.
                logger.debug(
                    f"[HL-COPY] Rescan fertig — {len(self._auto_wallets)} "
                    f"Beobachtungs-Wallet(s), {len(self._wallets)} getrackt gesamt"
                )
            except Exception as e:
                logger.debug(f"[HL] Rescan error: {e}")

    def _discover_scalpers(self) -> list[str]:
        """Quantitativ verifizierte Spot-Copy-Kandidaten automatisch auswählen."""
        results = self.list_leaderboard(
            window="month",
            limit=DISCOVERY_CANDIDATE_POOL,
            min_trades=MIN_TRADES,
            min_win_rate=MIN_WIN_RATE,
            min_avg_hold_sec=120.0,
            max_avg_hold_sec=MAX_AVG_HOLD_SEC,
            min_account_value=MIN_ACCOUNT_VALUE,
            verified_only=True,
        )
        selected = results[:MAX_TRACKED_TRADERS]
        for candidate in selected:
            logger.debug(
                f"[HL] Verifizierter Kandidat {self._short(candidate['wallet'])} | "
                f"Score={candidate['quality_score']:.1f} | "
                f"PF={candidate['profit_factor']:.2f} | "
                f"Long-PF={candidate['long_profit_factor']:.2f} | "
                f"DD={candidate['max_drawdown_pct']:.1f}%"
            )
        return [candidate["wallet"] for candidate in selected]

    @staticmethod
    def _fetch_leaderboard() -> list[dict]:
        try:
            resp = _hl_request("GET", LEADERBOARD_URL, timeout=20)
            if resp is None:
                return []
            resp.raise_for_status()
            return resp.json().get("leaderboardRows", [])
        except Exception as e:
            logger.debug(f"[HL] Leaderboard fetch failed: {e}")
            return []

    @staticmethod
    def _leaderboard_window_metric(row: dict, window: str) -> tuple[float, float]:
        perf = dict(row.get("windowPerformances", []))
        w = perf.get(window, {})
        return float(w.get("pnl", "0") or "0"), float(w.get("vlm", "0") or "0")

    @staticmethod
    def _verify_copy_candidate(
        metrics: TraderMetrics,
        *,
        day_pnl: float,
        week_pnl: float,
        month_pnl: float,
        min_trades: int,
        min_win_rate: float,
        min_avg_hold_sec: float,
        max_avg_hold_sec: float,
    ) -> tuple[bool, list[str]]:
        """Harte Gates für einen statistisch belastbaren Spot-Copy-Kandidaten."""
        failures = []
        gates = (
            (metrics.trades >= min_trades, f"nur {metrics.trades}/{min_trades} Trades (30T)"),
            (metrics.active_days >= 5, f"nur {metrics.active_days}/5 aktive Tage"),
            (metrics.win_rate >= min_win_rate,
             f"Win-Rate {metrics.win_rate:.0%} < {min_win_rate:.0%}"),
            (metrics.profit_factor >= 1.30,
             f"Profit-Faktor {metrics.profit_factor:.2f} < 1.30"),
            (metrics.max_drawdown_pct <= 15.0,
             f"Drawdown {metrics.max_drawdown_pct:.1f}% > 15%"),
            (metrics.long_trades >= 10,
             f"nur {metrics.long_trades}/10 kopierbare Long-Trades"),
            (metrics.long_share >= 0.25,
             f"Long-Anteil {metrics.long_share:.0%} < 25%"),
            (metrics.long_net_pnl > 0,
             f"Long-PnL nicht positiv (${metrics.long_net_pnl:+,.0f})"),
            (metrics.long_profit_factor >= 1.20,
             f"Long-Profit-Faktor {metrics.long_profit_factor:.2f} < 1.20"),
            (metrics.avg_hold_sec >= min_avg_hold_sec,
             f"Ø Haltedauer {metrics.avg_hold_sec / 60:.1f}min zu kurz"),
            (metrics.avg_hold_sec <= max_avg_hold_sec,
             f"Ø Haltedauer {metrics.avg_hold_sec / 60:.1f}min zu lang"),
            (metrics.last_active_age <= ACTIVE_WITHIN_SEC,
             f"letzte Aktivität {metrics.last_active_age / 3600:.1f}h her"),
            (week_pnl > 0, f"7T-PnL negativ (${week_pnl:+,.0f})"),
            (month_pnl > 0, f"30T-PnL negativ (${month_pnl:+,.0f})"),
        )
        failures.extend(message for passed, message in gates if not passed)
        return not failures, failures

    @staticmethod
    def _copy_quality_score(metrics: TraderMetrics, performance: dict) -> float:
        """Score 0–100; harte Verifikation entscheidet, Score sortiert danach."""
        def bounded(value: float, low: float, high: float) -> float:
            return max(0.0, min(1.0, (value - low) / (high - low)))

        profit_factor = min(metrics.profit_factor, 4.0)
        long_profit_factor = min(metrics.long_profit_factor, 4.0)
        consistency = sum(
            float(performance[key] > 0)
            for key in ("day_pnl", "week_pnl", "month_pnl")
        ) / 3
        score = (
            20 * bounded(profit_factor, 1.0, 2.5)
            + 20 * bounded(long_profit_factor, 1.0, 2.5)
            + 15 * (1 - min(metrics.max_drawdown_pct, 15.0) / 15.0)
            + 15 * min(metrics.trades / 100, 1.0)
            + 10 * bounded(metrics.win_rate, 0.40, 0.65)
            + 10 * consistency
            + 5 * min(metrics.active_days / 15, 1.0)
            + 5 * min(metrics.long_share / 0.60, 1.0)
        )
        return round(score, 1)

    def _fetch_user_fills(self, wallet: str) -> Optional[list[dict]]:
        """Fetch raw userFills for a wallet, cached for METRICS_CACHE_TTL.

        `userFills` is the heaviest HL endpoint. The Focus tab needs the same
        list to compute 4 windows AND to render "recent fills" — sharing this
        cache means all of that costs one network call, not five.
        """
        cached = self._fills_cache.get(wallet)
        if cached and (time.time() - cached[0]) < METRICS_CACHE_TTL:
            return cached[1]
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
            if not isinstance(fills, list):
                return None
        except Exception as e:
            logger.debug(f"[HL-COPY] Fills fetch failed for {wallet}: {e}")
            return None
        self._fills_cache[wallet] = (time.time(), fills)
        return fills

    def _compute_trader_metrics(
        self, wallet: str, since_ts: Optional[float] = None,
    ) -> Optional[TraderMetrics]:
        """Profitabilität, Risiko und Spot-Copy-Eignung aus Fills ableiten.

        `since_ts` restricts fills to a wall-clock window so the resulting metrics
        actually match the PnL/Volume window the user picked in the scanner —
        without it, trades/win-rate/hold were previously LIFETIME numbers next to
        windowed PnL, which produced the misleading columns in the old dashboard.
        """
        cache_key = (wallet, since_ts)
        cached = self._metrics_cache.get(cache_key)
        if cached and (time.time() - cached[0]) < METRICS_CACHE_TTL:
            return cached[1]

        fills = self._fetch_user_fills(wallet)
        if not fills:
            return None

        fills = sorted(fills, key=lambda f: f.get("time", 0))
        if since_ts is not None:
            fills = [f for f in fills if float(f.get("time", 0)) / 1000.0 >= since_ts]
        if not fills:
            return None

        open_times: dict[tuple[str, str], float] = {}
        hold_durations: list[float] = []
        realized: list[float] = []
        long_realized: list[float] = []
        active_dates: set[str] = set()
        closed_trades = 0
        wins = 0

        for fill in fills:
            coin = fill.get("coin", "")
            direction = fill.get("dir", "")
            ts = float(fill.get("time", 0)) / 1000.0
            active_dates.add(time.strftime("%Y-%m-%d", time.gmtime(ts)))

            if direction.startswith("Open"):
                side = "long" if "Long" in direction else "short"
                open_times.setdefault((coin, side), ts)
            elif direction.startswith("Close"):
                closed_trades += 1
                pnl = float(fill.get("closedPnl", "0") or "0")
                fee = abs(float(fill.get("fee", "0") or "0"))
                net = pnl - fee
                realized.append(net)
                if net > 0:
                    wins += 1
                side = "long" if "Long" in direction else "short"
                if side == "long":
                    long_realized.append(net)
                open_ts = open_times.pop((coin, side), None)
                if open_ts is not None:
                    hold_durations.append(ts - open_ts)

        if closed_trades == 0:
            return None

        avg_hold = (
            sum(hold_durations) / len(hold_durations)
            if hold_durations else MAX_AVG_HOLD_SEC + 1
        )
        last_active_age = time.time() - (fills[-1].get("time", 0) / 1000.0)

        gross_profit = sum(value for value in realized if value > 0)
        gross_loss = abs(sum(value for value in realized if value < 0))
        profit_factor = gross_profit / gross_loss if gross_loss > 0 else float("inf")
        long_profit = sum(value for value in long_realized if value > 0)
        long_loss = abs(sum(value for value in long_realized if value < 0))
        long_profit_factor = long_profit / long_loss if long_loss > 0 else (
            float("inf") if long_profit > 0 else 0.0
        )

        equity = peak = max_drawdown = 0.0
        for value in realized:
            equity += value
            peak = max(peak, equity)
            max_drawdown = max(max_drawdown, peak - equity)
        stats = self._trader_stats.get(wallet)
        account_value = stats.total_pnl if stats else 0.0
        max_drawdown_pct = (
            max_drawdown / account_value * 100 if account_value > 0 else float("inf")
        )

        metrics = TraderMetrics(
            trades=closed_trades,
            win_rate=wins / closed_trades,
            avg_hold_sec=avg_hold,
            last_active_age=last_active_age,
            net_pnl=sum(realized),
            profit_factor=profit_factor,
            max_drawdown=max_drawdown,
            max_drawdown_pct=max_drawdown_pct,
            active_days=len(active_dates),
            long_trades=len(long_realized),
            long_net_pnl=sum(long_realized),
            long_profit_factor=long_profit_factor,
            long_share=len(long_realized) / closed_trades,
        )
        self._metrics_cache[cache_key] = (time.time(), metrics)
        return metrics

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
        """Regelmäßiger Tracking-Nachweis für die gewählten Trader.

        Loggt bewusst nur Trader, die wir kopieren — pro Trader wird sichtbar,
        ob der WebSocket abonniert ist, wann der letzte Fill kam und welche
        Positionen der Trader gerade auf Hyperliquid hält.
        """
        while True:
            await asyncio.sleep(60)
            copied = [w for w in list(self._wallets) if self._should_emit_signals_for(w)]
            if not copied:
                logger.debug("[HL] Kein Trader ausgewählt — nichts zu tracken")
                continue

            st = self.status()
            logger.debug(
                f"[HL] Tracking | WS:{'auf' if st['ws_connected'] else 'ab'} | "
                f"{st['ws_subscribed']}/{len(copied)} Trader abonniert | "
                f"Polls:{st['poll_count']} | frische Signale:{st['fresh_signals']}"
            )
            now = time.time()
            for wallet in copied:
                tel = self._tel(wallet)
                positions = self._positions.get(wallet, {})
                coins = ", ".join(
                    f"{c} {'L' if p['size'] > 0 else 'S'} ${p['value_usd']:,.0f}"
                    for c, p in positions.items()
                ) or "keine Position"
                fill_age = (f"{(now - tel['last_fill_at']) / 60:.0f}min"
                            if tel["last_fill_at"] else "noch keiner")
                poll_age = (f"{now - tel['last_poll_at']:.0f}s"
                            if tel["last_poll_at"] else "nie")
                logger.debug(
                    f"[HL-COPY]   {self._short(wallet)} | "
                    f"WS:{'✓' if tel['ws_subscribed'] else '✗'} | "
                    f"Fills:{tel['fill_count']} (letzter {fill_age}) | "
                    f"Poll vor {poll_age} | Signale:{tel['signal_count']} | {coins}"
                )
                if tel["poll_error"]:
                    logger.debug(
                        f"[HL-COPY]   {self._short(wallet)} Poll-Fehler: {tel['poll_error']}"
                    )

