"""
src/adapters/binance_leaderboard.py — Binance Futures-Leaderboard Copy-Trader

Ersetzt den frueheren Hyperliquid-Adapter: Binance selbst betreibt ein
Copy-Trading-Leaderboard (futures-activities/leaderboard) mit oeffentlichen
Trader-Profilen. Dieser Adapter nutzt es als Signalquelle, Ausfuehrung bleibt
komplett auf Binance (Spot, ueber src/execution/binance_executor.py).

WICHTIG — inoffizielle API
---------------------------
Binance veroeffentlicht fuer dieses Leaderboard KEIN offizielles, dokumen-
tiertes API. Die hier verwendeten `bapi`-Endpunkte sind dieselben, die
binance.com/en/copy-trading im Browser aufruft (verifiziert per Netzwerk-
Mitschnitt am 2026-09-02), koennen sich aber jederzeit ohne Ankuendigung
aendern. Binance kennt dabei KEIN eigenes Tages-Zeitfenster (nur 7D/30D/90D)
— der Tages-Wert wird deshalb aus der taeglichen, kumulierten ROI-Kurve
(`chartItems`) der 7-Tage-Antwort abgeleitet (letzter Punkt minus vorletzter).
Die Feld-Extraktion ist bewusst defensiv (mehrere moegliche Schluesselnamen,
nie Absturz bei fehlenden Feldern).

Kernfunktion (das, was der Nutzer wollte)
------------------------------------------
`find_intraday_traders()` — findet Trader, die INNERHALB EINES TAGES mit
gutem Profit (ROI/PnL-Schwellen) handeln, prueft sie gegen 7-/30-Tage-Daten
(keine Eintagsfliegen) und filtert alles andere raus.

Signal-Flow (fuer die Bot-Pipeline)
------------------------------------
  Getrackter Trader eroeffnet BTCUSDT Long (Binance Futures, oeffentlich sichtbar)
    → COPY_OPEN_LONG Signal
    → Binance-Spot-Pruefung (Liquiditaet, Spread)
    → Market BUY + OCO Exit
"""
from __future__ import annotations

import asyncio
import math
import os
import threading
import time
from collections import defaultdict
from dataclasses import dataclass, field
from typing import Optional

import requests
from loguru import logger

from src.utils import trader_store

# ── Config ────────────────────────────────────────────────────────────────────

# Reale, per Netzwerk-Mitschnitt verifizierte Endpunkte von binance.com/en/copy-trading
BASE_URL = "https://www.binance.com/bapi/futures/v1/friendly/future/copy-trade/home-page"
QUERY_LIST_URL = f"{BASE_URL}/query-list"
POSITIONS_URL = "https://www.binance.com/bapi/futures/v1/friendly/future/copy-trade/lead-data/positions"
PAGE_SIZE = 30  # vom Server hart begrenzt, groessere pageSize-Werte werden ignoriert

_raw_uids = os.getenv("BNLB_TRADER_UIDS", "")
MANUAL_UIDS: list[str] = [u.strip() for u in _raw_uids.split(",") if u.strip()]

TRADE_TYPE = os.getenv("BNLB_TRADE_TYPE", "PERPETUAL")

POLL_INTERVAL = float(os.getenv("BNLB_POLL_INTERVAL", "5"))
MIN_COPY_SIZE_USD = float(os.getenv("BNLB_MIN_COPY_SIZE_USD", "1000"))
SIGNAL_TTL = float(os.getenv("BNLB_SIGNAL_TTL", "60.0"))

# Auto-discovery
AUTO_DISCOVER = os.getenv("BNLB_AUTO_DISCOVER", "True").lower() == "true"
MAX_TRACKED_TRADERS = int(os.getenv("BNLB_MAX_TRADERS", "5"))
RESCAN_INTERVAL_SEC = float(os.getenv("BNLB_RESCAN_HOURS", "6")) * 3600
DISCOVERY_POOL = int(os.getenv("BNLB_DISCOVERY_POOL", "50"))

# Intraday-Filter: "handelt innerhalb eines Tages mit gutem Profit"
MIN_DAY_ROI_PCT = float(os.getenv("BNLB_MIN_DAY_ROI_PCT", "3.0"))
MIN_DAY_PNL_USD = float(os.getenv("BNLB_MIN_DAY_PNL_USD", "50"))
MIN_FOLLOWERS = int(os.getenv("BNLB_MIN_FOLLOWERS", "0"))
REQUIRE_POSITION_SHARED = os.getenv("BNLB_REQUIRE_POSITION_SHARED", "True").lower() == "true"
REQUIRE_POSITIVE_WEEK = os.getenv("BNLB_REQUIRE_POSITIVE_WEEK", "True").lower() == "true"
REQUIRE_POSITIVE_MONTH = os.getenv("BNLB_REQUIRE_POSITIVE_MONTH", "True").lower() == "true"
ACTIVE_WITHIN_SEC = float(os.getenv("BNLB_ACTIVE_WITHIN_HOURS", "24")) * 3600

LEADERBOARD_WINDOWS = ("day", "week", "month")
# Binance kennt nur 7D/30D/90D — "day" wird aus der 7D-chartItems-Kurve abgeleitet.
_TIME_RANGE = {"week": "7D", "month": "30D"}

# Drosselung gegen Rate-Limits der oeffentlichen Binance-Webseite
MIN_REQUEST_INTERVAL = float(os.getenv("BNLB_MIN_REQUEST_INTERVAL", "1.0"))
METRICS_CACHE_TTL = float(os.getenv("BNLB_METRICS_CACHE_TTL", "300"))

STORE_SYNC_INTERVAL = float(os.getenv("BNLB_STORE_SYNC_SEC", "3"))
STATE_PUBLISH_INTERVAL = float(os.getenv("BNLB_STATE_PUBLISH_SEC", "3"))

# Futures-Contracts, deren Symbol vom Spot-Pendant abweicht (Micro-Preis-Token)
_FUTURES_SPOT_OVERRIDES = {
    "1000PEPEUSDT": "PEPEUSDT", "1000SHIBUSDT": "SHIBUSDT",
    "1000FLOKIUSDT": "FLOKIUSDT", "1000BONKUSDT": "BONKUSDT",
    "1000SATSUSDT": "SATSUSDT", "1000RATSUSDT": "RATSUSDT",
    "1000LUNCUSDT": "LUNCUSDT", "1000XECUSDT": "XECUSDT",
}

_rate_lock = threading.Lock()
_last_request_time = 0.0

_API_HEALTH_LOCK = threading.Lock()
_api_health_state: str = "ok"  # ok | rate_limited | unreachable


def _set_api_health(state: str) -> None:
    global _api_health_state
    with _API_HEALTH_LOCK:
        _api_health_state = state


def get_api_health() -> str:
    with _API_HEALTH_LOCK:
        return _api_health_state


def _throttle() -> None:
    global _last_request_time
    with _rate_lock:
        wait = MIN_REQUEST_INTERVAL - (time.time() - _last_request_time)
        if wait > 0:
            time.sleep(wait)
        _last_request_time = time.time()


def _bn_request(
    url: str, payload: Optional[dict] = None, *,
    method: str = "POST", params: Optional[dict] = None, timeout: float = 10,
) -> Optional[dict]:
    """Throttled Request gegen die inoffiziellen bapi-Copy-Trading-Endpunkte."""
    backoff = 1.0
    for _ in range(4):
        _throttle()
        try:
            if method == "GET":
                resp = requests.get(url, params=params, timeout=timeout)
            else:
                resp = requests.post(url, json=payload, timeout=timeout)
        except requests.RequestException as e:
            logger.debug(f"[BN-LB] Request error: {e}")
            _set_api_health("unreachable")
            return None
        if resp.status_code == 429:
            logger.debug(f"[BN-LB] 429 rate limited — backing off {backoff:.1f}s")
            _set_api_health("rate_limited")
            time.sleep(backoff)
            backoff = min(backoff * 2, 10.0)
            continue
        if not resp.ok:
            _set_api_health("unreachable" if resp.status_code >= 500 else "ok")
            return None
        _set_api_health("ok")
        try:
            return resp.json()
        except ValueError:
            return None
    _set_api_health("rate_limited")
    return None


def futures_symbol_to_spot_symbol(symbol: str) -> str:
    """Binance-Futures-Symbol auf sein Spot-Pendant abbilden (meist identisch)."""
    symbol = symbol.upper().strip()
    return _FUTURES_SPOT_OVERRIDES.get(symbol, symbol)


def binance_leaderboard_url(uid: str) -> str:
    """Oeffentliches Copy-Trading-Profil zur manuellen Gegenpruefung."""
    return f"https://www.binance.com/en/copy-trading/lead-details/{uid}?timeRange=30D"


@dataclass
class CopySignal:
    """Signal, wenn ein getrackter Trader eine Position aendert."""
    trader:      str        # encryptedUid
    coin:        str        # z.B. "BTC"
    symbol:      str        # Binance-Symbol (BTCUSDT, ...)
    signal:      str        # COPY_OPEN_LONG | COPY_CLOSE_LONG | ...
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
class TraderMetrics:
    """Aus dem oeffentlichen Leaderboard ableitbare Kennzahlen eines Traders."""
    day_roi:         float = 0.0
    day_pnl:         float = 0.0
    week_roi:        Optional[float] = None
    week_pnl:        Optional[float] = None
    month_roi:       Optional[float] = None
    month_pnl:       Optional[float] = None
    follower_count:  int = 0
    position_shared: bool = False
    nick_name:       str = ""
    last_update_age: float = 0.0


@dataclass
class TraderCandidate:
    uid:               str
    metrics:           TraderMetrics
    quality_score:     float
    verified:          bool
    reasons:           list[str]


# ── Reine Filter-/Scoring-Logik (ohne Netzwerk, gut testbar) ─────────────────

def evaluate_candidate(
    metrics: TraderMetrics,
    *,
    min_day_roi_pct: float = MIN_DAY_ROI_PCT,
    min_day_pnl_usd: float = MIN_DAY_PNL_USD,
    min_followers: int = MIN_FOLLOWERS,
    require_position_shared: bool = REQUIRE_POSITION_SHARED,
    require_positive_week: bool = REQUIRE_POSITIVE_WEEK,
    require_positive_month: bool = REQUIRE_POSITIVE_MONTH,
    max_last_update_age_sec: float = ACTIVE_WITHIN_SEC,
) -> tuple[bool, list[str]]:
    """Harte Gates: 'guter Intraday-Trader'? True + [] wenn ja."""
    gates = (
        (metrics.day_roi >= min_day_roi_pct,
         f"Tages-ROI {metrics.day_roi:.1f}% < {min_day_roi_pct:.1f}%"),
        (metrics.day_pnl >= min_day_pnl_usd,
         f"Tages-PnL ${metrics.day_pnl:,.0f} < ${min_day_pnl_usd:,.0f}"),
        (metrics.follower_count >= min_followers,
         f"nur {metrics.follower_count} Follower < {min_followers}"),
        (metrics.position_shared or not require_position_shared,
         "Positionen nicht oeffentlich geteilt"),
        (not require_positive_week or metrics.week_roi is None or metrics.week_roi > 0,
         f"7T-ROI negativ ({metrics.week_roi:+.1f}%)" if metrics.week_roi is not None else ""),
        (not require_positive_month or metrics.month_roi is None or metrics.month_roi > 0,
         f"30T-ROI negativ ({metrics.month_roi:+.1f}%)" if metrics.month_roi is not None else ""),
        (metrics.last_update_age <= max_last_update_age_sec,
         f"seit {metrics.last_update_age / 3600:.1f}h inaktiv"),
    )
    failures = [msg for ok, msg in gates if not ok and msg]
    return not failures, failures


def quality_score(metrics: TraderMetrics) -> float:
    """Score 0-100, hoeher = besser. Tagesgewinn zaehlt am meisten (Intraday-Fokus)."""
    def bounded(value: float, low: float, high: float) -> float:
        return max(0.0, min(1.0, (value - low) / (high - low)))

    periods = [metrics.day_roi]
    if metrics.week_roi is not None:
        periods.append(metrics.week_roi)
    if metrics.month_roi is not None:
        periods.append(metrics.month_roi)
    consistency = sum(1 for value in periods if value > 0) / len(periods)

    score = (
        40 * bounded(metrics.day_roi, 0, 20)
        + 20 * bounded(metrics.day_pnl, 0, 2000)
        + 15 * consistency
        + 15 * bounded(math.log10(max(metrics.follower_count, 0) + 1), 0, 3)
        + 10 * (1.0 if metrics.position_shared else 0.0)
    )
    return round(score, 1)


def _extract_rank_row(row: dict) -> tuple[Optional[str], float, dict]:
    """Ein Leaderboard-Eintrag → (uid, Rank-Wert, gemeinsame Metadaten)."""
    uid = row.get("leadPortfolioId") or row.get("encryptedUid") or row.get("uid")
    if not uid:
        return None, 0.0, {}
    raw_value = row.get("value", row.get("rankValue", row.get("pnl", row.get("roi", 0))))
    try:
        value = float(raw_value)
    except (TypeError, ValueError):
        value = 0.0
    meta = {
        "nick_name": row.get("nickname") or row.get("nickName", ""),
        "follower_count": int(row.get("currentCopyCount", row.get("followerCount", 0)) or 0),
        # Binance liefert kein Sharing-Flag mehr auf Listen-Eintraegen — alles,
        # was hier zurueckkommt, ist Teil der oeffentlichen Liste und damit
        # per `getOtherPosition`-Aequivalent (lead-data/positions) einsehbar.
        "position_shared": bool(row.get("positionShared", row.get("isShared", True))),
        "update_time_ms": float(row.get("updateTime", 0) or 0),
    }
    return uid, value, meta


def merge_rank_rows(rank_data: dict[tuple[str, str], list[dict]]) -> dict[str, TraderMetrics]:
    """(periodType, statisticsType) → Rohzeilen  in  uid → TraderMetrics  zusammenfuehren.

    Reine Funktion ohne Netzwerkzugriff — der Kern von `find_intraday_traders`
    und deshalb direkt testbar mit handgebauten Fixtures.
    """
    now = time.time()
    metrics: dict[str, TraderMetrics] = {}

    def _get(uid: str) -> TraderMetrics:
        return metrics.setdefault(uid, TraderMetrics())

    for (period, stat), rows in rank_data.items():
        for row in rows:
            uid, value, meta = _extract_rank_row(row)
            if uid is None:
                continue
            m = _get(uid)
            if meta["nick_name"]:
                m.nick_name = meta["nick_name"]
            m.follower_count = max(m.follower_count, meta["follower_count"])
            m.position_shared = m.position_shared or meta["position_shared"]
            if meta["update_time_ms"]:
                age = max(0.0, now - meta["update_time_ms"] / 1000.0)
                m.last_update_age = age if m.last_update_age == 0.0 else min(m.last_update_age, age)

            if period == "day" and stat == "ROI":
                m.day_roi = value
            elif period == "day" and stat == "PNL":
                m.day_pnl = value
            elif period == "week" and stat == "ROI":
                m.week_roi = value
            elif period == "week" and stat == "PNL":
                m.week_pnl = value
            elif period == "month" and stat == "ROI":
                m.month_roi = value
            elif period == "month" and stat == "PNL":
                m.month_pnl = value

    return metrics


# ── Netzwerk-Zugriff ──────────────────────────────────────────────────────────

def _fetch_query_list_pages(
    time_range: str, data_type: str, limit: int, page_size: int = PAGE_SIZE,
) -> list[dict]:
    """Durch `query-list` bis `limit` Eintraege gesammelt (Server begrenzt pageSize hart)."""
    rows: list[dict] = []
    page_number = 1
    while len(rows) < limit:
        payload = {
            "pageNumber": page_number, "pageSize": page_size,
            "timeRange": time_range, "dataType": data_type,
            "favoriteOnly": False, "hideFull": False, "nickname": "",
            "order": "DESC", "userAsset": 0, "portfolioType": "ALL",
            "useAiRecommended": True, "PAGE_SIZE": page_size,
        }
        data = _bn_request(QUERY_LIST_URL, payload)
        if not data:
            break
        page_rows = (data.get("data") or {}).get("list") or []
        if not isinstance(page_rows, list) or not page_rows:
            break
        rows.extend(page_rows)
        if len(page_rows) < page_size:
            break
        page_number += 1
    return rows[:limit]


def _synthesize_day_row(row: dict, statistics_type: str) -> dict:
    """Tages-Metrik aus der taeglichen `chartItems`-ROI-Kurve ableiten.

    Binance liefert nur kumulierte 7T/30T/90T-Fenster, aber `chartItems`
    enthaelt fuer jeden Trader eine taegliche, kumulierte ROI-Reihe — die
    Differenz der letzten beiden Punkte ist der ROI von HEUTE. Tages-PnL gibt
    es oeffentlich nicht; er wird anteilig aus dem 7-Tage-PnL geschaetzt
    (Tages-ROI / 7-Tage-ROI * 7-Tage-PnL).
    """
    chart = row.get("chartItems") or []
    day_roi = 0.0
    if len(chart) >= 2:
        try:
            day_roi = float(chart[-1]["value"]) - float(chart[-2]["value"])
        except (KeyError, TypeError, ValueError):
            day_roi = 0.0
    if statistics_type == "PNL":
        try:
            week_roi = float(row.get("roi", 0) or 0)
            week_pnl = float(row.get("pnl", 0) or 0)
        except (TypeError, ValueError):
            week_roi, week_pnl = 0.0, 0.0
        metric = (day_roi / week_roi * week_pnl) if week_roi else 0.0
    else:
        metric = day_roi
    synthesized = dict(row)
    synthesized["value"] = metric
    return synthesized


def fetch_leaderboard_rank(
    period: str = "day", statistics_type: str = "ROI",
    trade_type: str = TRADE_TYPE, limit: int = DISCOVERY_POOL,
) -> list[dict]:
    """Top-N Trader fuer ein Zeitfenster + eine Rangier-Metrik.

    `trade_type` bleibt fuer Abwaertskompatibilitaet des Funktionssignatur
    erhalten, wird von der echten API aber nicht mehr benoetigt (nur Futures).
    """
    del trade_type
    if period == "day":
        rows = _fetch_query_list_pages(time_range="7D", data_type=statistics_type, limit=limit)
        return [_synthesize_day_row(row, statistics_type) for row in rows]
    time_range = _TIME_RANGE.get(period, "7D")
    return _fetch_query_list_pages(time_range=time_range, data_type=statistics_type, limit=limit)


def fetch_other_positions(uid: str, trade_type: str = TRADE_TYPE) -> list[dict]:
    """Oeffentlich sichtbare, aktuell offene Positionen eines Lead-Portfolios."""
    del trade_type
    data = _bn_request(POSITIONS_URL, method="GET", params={"portfolioId": uid})
    if not data:
        return []
    rows = data.get("data") or []
    return rows if isinstance(rows, list) else []


def find_intraday_traders(
    limit: int = 30,
    pool_size: int = DISCOVERY_POOL,
    trade_type: str = TRADE_TYPE,
    verified_only: bool = True,
    min_day_roi_pct: float = MIN_DAY_ROI_PCT,
    min_day_pnl_usd: float = MIN_DAY_PNL_USD,
    min_followers: int = MIN_FOLLOWERS,
    require_position_shared: bool = REQUIRE_POSITION_SHARED,
    require_positive_week: bool = REQUIRE_POSITIVE_WEEK,
    require_positive_month: bool = REQUIRE_POSITIVE_MONTH,
) -> list[TraderCandidate]:
    """Trader finden, die INNERHALB EINES TAGES mit gutem Profit handeln.

    Holt Binances eigenes Tages-Leaderboard (periodType=DAILY) als Kandidaten-
    Pool, prueft jeden Kandidaten zusaetzlich gegen die Wochen-/Monatsrangliste
    (keine Eintagsfliegen) und filtert alles heraus, was die Mindestwerte
    nicht erreicht. Das Ergebnis ist nach Quality-Score sortiert.
    """
    rank_data: dict[tuple[str, str], list[dict]] = {}
    for period in LEADERBOARD_WINDOWS:
        for stat in ("ROI", "PNL"):
            rank_data[(period, stat)] = fetch_leaderboard_rank(
                period=period, statistics_type=stat,
                trade_type=trade_type, limit=pool_size,
            )

    all_metrics = merge_rank_rows(rank_data)
    # Nur wer heute unter den Top-Performern ist, gilt als "Intraday-Kandidat".
    day_pool = {row.get("encryptedUid") or row.get("uid") for row in rank_data[("day", "ROI")]}
    day_pool.discard(None)

    results: list[TraderCandidate] = []
    for uid in day_pool:
        metrics = all_metrics.get(uid)
        if metrics is None:
            continue
        verified, reasons = evaluate_candidate(
            metrics,
            min_day_roi_pct=min_day_roi_pct,
            min_day_pnl_usd=min_day_pnl_usd,
            min_followers=min_followers,
            require_position_shared=require_position_shared,
            require_positive_week=require_positive_week,
            require_positive_month=require_positive_month,
        )
        if verified_only and not verified:
            continue
        results.append(TraderCandidate(
            uid=uid, metrics=metrics,
            quality_score=quality_score(metrics),
            verified=verified, reasons=reasons,
        ))

    results.sort(key=lambda c: c.quality_score, reverse=True)
    return results[:limit]


# ── Copy-Trader-Adapter (Polling, kein WebSocket — Binance bietet dafuer keine
#    oeffentliche API fuer fremde Trader) ───────────────────────────────────

class BinanceLeaderboardTrader:
    """Trackt ausgewaehlte Binance-Leaderboard-Trader und emittiert Copy-Signale.

    Usage:
        adapter = BinanceLeaderboardTrader()
        asyncio.create_task(adapter.start())
        signal = await adapter.signal_queue.get()
    """

    def __init__(self, publish_state: bool = False) -> None:
        self._publish_state = publish_state
        trader_store.init_db()

        self._positions: dict[str, dict[str, dict]] = defaultdict(dict)
        self.signal_queue: asyncio.Queue[CopySignal] = asyncio.Queue(maxsize=500)
        self._signals: list[CopySignal] = []
        self._max_signals = 200
        self._connected = False
        self._discovering = False
        self._last_poll = 0.0
        self._poll_count = 0
        self._uids_lock = threading.Lock()
        self._manual_active: set[str] = set()
        self._observed_only: set[str] = set()
        self._focus_uid: Optional[str] = None
        self._copy_sizes: dict[str, float] = {}
        self._uids: list[str] = list(MANUAL_UIDS)
        self._auto_uids: list[str] = []
        self._telemetry: dict[str, dict] = {}
        self._nick_names: dict[str, str] = {}
        self._seed_env_uids()
        self._sync_from_store(initial=True)
        self._last_scan = 0.0
        self._poll_interval = POLL_INTERVAL
        self._min_copy_size_usd = MIN_COPY_SIZE_USD

    # ── Public API ────────────────────────────────────────────────────────

    def get_signals(self, signal_type: Optional[str] = None) -> list[CopySignal]:
        fresh = [s for s in self._signals if s.is_fresh]
        if signal_type:
            fresh = [s for s in fresh if s.signal == signal_type]
        return sorted(fresh, key=lambda s: s.timestamp, reverse=True)

    def get_trader_positions(self, uid: str) -> dict[str, dict]:
        return dict(self._positions.get(uid, {}))

    def get_all_positions(self) -> dict[str, dict[str, dict]]:
        return {u: dict(p) for u, p in self._positions.items()}

    def get_wallets(self) -> list[str]:
        return list(self._uids)

    def get_settings(self) -> dict:
        return {
            "poll_interval": self._poll_interval,
            "min_copy_size_usd": self._min_copy_size_usd,
            "auto_discover": AUTO_DISCOVER,
            "max_tracked_traders": MAX_TRACKED_TRADERS,
            "rescan_hours": RESCAN_INTERVAL_SEC / 3600,
            "min_day_roi_pct": MIN_DAY_ROI_PCT,
            "min_day_pnl_usd": MIN_DAY_PNL_USD,
            "min_followers": MIN_FOLLOWERS,
            "signal_ttl": SIGNAL_TTL,
        }

    def set_poll_interval(self, seconds: float) -> None:
        self._poll_interval = max(1.0, float(seconds))

    def set_min_copy_size(self, usd: float) -> None:
        self._min_copy_size_usd = max(0.0, float(usd))

    # ── Trader-Auswahl (DB-gestuetzt, prozessuebergreifend) ─────────────────

    @staticmethod
    def _seed_env_uids() -> None:
        for uid in MANUAL_UIDS:
            trader_store.upsert_trader(uid, is_copied=True, source="env")

    def _rebuild_uid_list(self) -> None:
        chosen = list(dict.fromkeys(list(MANUAL_UIDS) + sorted(self._manual_active)))
        if self._focus_uid and self._focus_uid not in chosen:
            chosen.append(self._focus_uid)
        auto = [u for u in self._auto_uids if u not in chosen]
        self._uids = chosen + auto
        observed = set(auto)
        if (self._focus_uid and self._focus_uid not in self._manual_active
                and self._focus_uid not in MANUAL_UIDS):
            observed.add(self._focus_uid)
        self._observed_only = observed

    def _sync_from_store(self, initial: bool = False) -> None:
        rows = trader_store.list_traders()
        copied = {r["wallet"] for r in rows if r["is_copied"]}
        focus = next((r["wallet"] for r in rows if r["is_focus"]), None)
        sizes = {
            r["wallet"]: float(r["size_usdt"])
            for r in rows if r["size_usdt"] and float(r["size_usdt"]) > 0
        }

        with self._uids_lock:
            added = copied - self._manual_active
            removed = self._manual_active - copied
            resized = {
                u: s for u, s in sizes.items()
                if not initial and self._copy_sizes.get(u) != s and u not in added
            }
            focus_changed = (not initial) and focus != self._focus_uid
            self._manual_active = copied
            self._focus_uid = focus
            self._copy_sizes = sizes
            self._rebuild_uid_list()
            stale = [u for u in list(self._positions) if u not in self._uids]

        for uid in stale:
            self._positions.pop(uid, None)
            self._telemetry.pop(uid, None)

        if initial or not self._publish_state:
            return

        for uid in sorted(added):
            size = sizes.get(uid)
            amount = f"${size:,.0f}" if size else "Standardbetrag"
            logger.success(
                f"[BN-LB] ➕ Trader uebernommen: {self._short(uid)} | "
                f"{amount}/Trade | Tracking + Binance-Pipeline aktiv"
            )
            trader_store.log_event(
                "TRADER_ADDED", f"Copy aktiviert mit {amount}/Trade",
                wallet=uid, level="success",
            )
        for uid in sorted(removed):
            logger.info(f"[BN-LB] ➖ Trader entfernt: {self._short(uid)}")
            trader_store.log_event("TRADER_REMOVED", "Copy deaktiviert", wallet=uid)
        for uid, size in sorted(resized.items()):
            logger.info(f"[BN-LB] ✏️  Betrag geaendert: {self._short(uid)} → ${size:,.0f}/Trade")
            trader_store.log_event(
                "TRADER_UPDATED", f"Einsatz auf ${size:,.0f}/Trade gesetzt", wallet=uid,
            )
        if focus_changed:
            logger.info(f"[BN-LB] 📌 Angepinnt: {self._short(focus) if focus else 'keiner'}")

    async def _sync_loop(self) -> None:
        while True:
            await asyncio.sleep(STORE_SYNC_INTERVAL)
            try:
                await asyncio.to_thread(self._sync_from_store)
            except Exception as e:
                logger.error(f"[BN-LB] Store-Sync fehlgeschlagen: {e}")

    def refresh(self) -> None:
        self._sync_from_store()

    @staticmethod
    def _short(uid: str) -> str:
        return f"{uid[:6]}...{uid[-4:]}" if len(uid) > 12 else uid

    def activate_wallet(self, uid: str) -> bool:
        uid = uid.strip()
        if not uid:
            return False
        is_new = uid not in self._manual_active
        trader_store.upsert_trader(uid, is_copied=True, source="dashboard")
        self._sync_from_store()
        return is_new

    def deactivate_wallet(self, uid: str) -> None:
        trader_store.upsert_trader(uid, is_copied=False)
        self._sync_from_store()

    def remove_trader(self, uid: str) -> None:
        uid = uid.strip()
        trader_store.remove_trader(uid)
        self._sync_from_store()
        self._positions.pop(uid, None)
        self._telemetry.pop(uid, None)

    def get_focus_wallet(self) -> Optional[str]:
        return self._focus_uid

    def set_focus_wallet(self, uid: str) -> None:
        if not uid.strip():
            return
        trader_store.set_focus(uid.strip())
        self._sync_from_store()

    def clear_focus_wallet(self) -> None:
        trader_store.set_focus(None)
        self._sync_from_store()

    def _should_emit_signals_for(self, uid: str) -> bool:
        if uid in self._observed_only:
            return False
        return uid in MANUAL_UIDS or uid in self._manual_active

    def is_copied(self, uid: str) -> bool:
        return self._should_emit_signals_for(uid)

    def get_copy_size(self, uid: str) -> Optional[float]:
        return self._copy_sizes.get(uid.strip())

    def set_copy_size(self, uid: str, size_usd: float) -> None:
        uid = uid.strip()
        if not uid or size_usd <= 0:
            return
        trader_store.upsert_trader(uid, size_usdt=float(size_usd))
        self._sync_from_store()

    def clear_copy_size(self, uid: str) -> None:
        trader_store.upsert_trader(uid.strip(), size_usdt=0)
        self._sync_from_store()

    # ── Tracking-Nachweis ────────────────────────────────────────────────

    def _tel(self, uid: str) -> dict:
        return self._telemetry.setdefault(uid, {
            "ws_subscribed": False, "ws_sub_at": 0.0,
            "last_fill_at": 0.0, "fill_count": 0,
            "last_poll_at": 0.0, "poll_count": 0, "poll_error": "",
            "signal_count": 0, "last_signal_at": 0.0,
        })

    def get_tracking(self) -> dict[str, dict]:
        out: dict[str, dict] = {}
        for uid in list(self._uids):
            tel = dict(self._tel(uid))
            tel["wallet"] = uid
            tel["is_copied"] = self._should_emit_signals_for(uid)
            tel["open_positions"] = len(self._positions.get(uid, {}))
            out[uid] = tel
        return out

    def _publish_tracker_state(self) -> None:
        rows = []
        for uid in list(self._uids):
            if uid not in self._manual_active and uid != self._focus_uid \
                    and uid not in MANUAL_UIDS:
                continue
            tel = dict(self._tel(uid))
            positions = dict(self._positions.get(uid, {}))
            tel.update({
                "wallet": uid,
                "is_copied": self._should_emit_signals_for(uid),
                "open_positions": len(positions),
                "positions": positions,
                "account_usd": 0.0,
            })
            rows.append(tel)
        trader_store.publish_tracker_state(rows)

    async def _publish_loop(self) -> None:
        while True:
            await asyncio.sleep(STATE_PUBLISH_INTERVAL)
            try:
                await asyncio.to_thread(self._publish_tracker_state)
            except Exception as e:
                logger.debug(f"[BN-LB] Tracker-State publish: {e}")

    def list_leaderboard(
        self, window: str = "day", limit: int = 30,
        min_day_roi_pct: float = MIN_DAY_ROI_PCT,
        min_day_pnl_usd: float = MIN_DAY_PNL_USD,
        min_followers: int = MIN_FOLLOWERS,
        verified_only: bool = True,
        progress_cb: Optional[callable] = None,
    ) -> list[dict]:
        """Fuer das Dashboard: verifizierte Kandidaten als Liste von Dicts."""
        del window  # find_intraday_traders bewertet immer alle drei Fenster
        candidates = find_intraday_traders(
            limit=limit, verified_only=verified_only,
            min_day_roi_pct=min_day_roi_pct, min_day_pnl_usd=min_day_pnl_usd,
            min_followers=min_followers,
        )
        results = []
        for i, candidate in enumerate(candidates, 1):
            if progress_cb:
                progress_cb(i, len(candidates), candidate.uid)
            m = candidate.metrics
            results.append({
                "wallet": candidate.uid,
                "nick_name": m.nick_name,
                "day_roi": m.day_roi, "day_pnl": m.day_pnl,
                "week_roi": m.week_roi, "week_pnl": m.week_pnl,
                "month_roi": m.month_roi, "month_pnl": m.month_pnl,
                "follower_count": m.follower_count,
                "position_shared": m.position_shared,
                "last_active_age": m.last_update_age,
                "quality_score": candidate.quality_score,
                "verified": candidate.verified,
                "verification_reasons": candidate.reasons,
                "metrics_source": "binance_leaderboard",
            })
        return results

    def get_trader_focus(self, uid: str) -> Optional[dict]:
        uid = uid.strip()
        if not uid:
            return None
        positions = dict(self._positions.get(uid, {}))
        if not positions:
            rows = fetch_other_positions(uid)
            positions = {p["coin"]: p for p in (self._parse_position_row(r) for r in rows) if p}
        return {
            "wallet": uid,
            "nick_name": self._nick_names.get(uid, ""),
            "positions": positions,
            "is_tracked": uid in self._uids,
            "is_active": uid in self._manual_active,
            "leaderboard_url": binance_leaderboard_url(uid),
        }

    def status(self) -> dict:
        total_positions = sum(len(p) for p in self._positions.values())
        copied = [u for u in self._uids if self._should_emit_signals_for(u)]
        return {
            "connected":       self._connected,
            "ws_connected":    self._connected,  # kein echter WS — Polling steht fuer "verbunden"
            "discovering":     self._discovering,
            "tracked_traders": len(self._uids),
            "copied_traders":  len(copied),
            "ws_subscribed":   len(copied) if self._connected else 0,
            "total_positions": total_positions,
            "fresh_signals":   len([s for s in self._signals if s.is_fresh]),
            "poll_count":      self._poll_count,
            "last_poll_age":   round(time.time() - self._last_poll, 1) if self._last_poll else None,
            "auto_discover":   AUTO_DISCOVER,
            "last_scan_age":   round(time.time() - self._last_scan, 1) if self._last_scan else None,
            "next_scan_in":    round(RESCAN_INTERVAL_SEC - (time.time() - self._last_scan), 1) if self._last_scan else None,
            "api_health":      get_api_health(),
        }

    # ── Startup ───────────────────────────────────────────────────────────

    async def start(self) -> None:
        if AUTO_DISCOVER:
            logger.debug("[BN-LB] Auto-Discovery aktiv — scanne Leaderboard …")
            self._discovering = True
            try:
                discovered = await asyncio.to_thread(self._discover_traders)
            finally:
                self._discovering = False
            with self._uids_lock:
                self._auto_uids = list(discovered)
                self._rebuild_uid_list()
            self._last_scan = time.time()

        copied = [u for u in self._uids if self._should_emit_signals_for(u)]
        if copied:
            selected = ", ".join(
                f"{self._short(u)} (${self._copy_sizes[u]:,.0f})"
                if u in self._copy_sizes else self._short(u)
                for u in copied
            )
            logger.info(f"[INIT] Gewaehlte Trader: {selected}")
        else:
            logger.info("[INIT] Kein Trader ausgewaehlt")

        await self._poll_all_positions(emit_signals=False)
        self._connected = True

        loops = [self._poll_loop(), self._rescan_loop(), self._sync_loop()]
        if self._publish_state:
            loops.append(self._publish_loop())
        await asyncio.gather(*loops)

    # ── Polling ───────────────────────────────────────────────────────────

    async def _poll_loop(self) -> None:
        while True:
            await asyncio.sleep(self._poll_interval)
            try:
                await self._poll_all_positions(emit_signals=True)
            except Exception as e:
                logger.error(f"[BN-LB] Poll error: {e}")

    async def _poll_all_positions(self, emit_signals: bool = True) -> None:
        for uid in list(self._uids):
            try:
                await self._poll_trader(uid, emit_signals)
            except Exception as e:
                self._tel(uid)["poll_error"] = str(e)
                logger.error(f"[BN-LB] Error polling {self._short(uid)}: {e}")
        self._last_poll = time.time()
        self._poll_count += 1

    @staticmethod
    def _parse_position_row(row: dict) -> Optional[dict]:
        symbol = str(row.get("symbol", "")).upper()
        if not symbol:
            return None
        spot_symbol = futures_symbol_to_spot_symbol(symbol)
        coin = spot_symbol.replace("USDT", "")

        raw_amt = row.get("amount", row.get("positionAmount", 0))
        try:
            amount = float(raw_amt)
        except (TypeError, ValueError):
            amount = 0.0
        side = str(row.get("positionSide", "")).upper()
        if side == "SHORT" and amount > 0:
            amount = -amount
        elif side == "LONG" and amount < 0:
            amount = -amount

        try:
            entry = float(row.get("entryPrice", 0) or 0)
        except (TypeError, ValueError):
            entry = 0.0
        try:
            leverage = float(row.get("leverage", 1) or 1)
        except (TypeError, ValueError):
            leverage = 1.0
        try:
            pnl = float(row.get("pnl", row.get("unrealizedProfit", 0)) or 0)
        except (TypeError, ValueError):
            pnl = 0.0
        try:
            roe = float(row.get("roe", 0) or 0)
        except (TypeError, ValueError):
            roe = 0.0

        value_usd = abs(amount) * entry
        pnl_pct = roe * 100 if roe else (pnl / value_usd * 100 if value_usd > 0 else 0.0)

        return {
            "coin": coin, "symbol": spot_symbol, "size": amount,
            "entry_px": entry, "leverage": leverage, "pnl_pct": pnl_pct,
            "value_usd": value_usd, "unrealized_pnl": pnl,
            "updated_at": time.time(),
        }

    async def _poll_trader(self, uid: str, emit_signals: bool) -> None:
        rows = await asyncio.to_thread(fetch_other_positions, uid)
        tel = self._tel(uid)
        tel["poll_error"] = ""
        tel["last_poll_at"] = time.time()
        tel["poll_count"] += 1
        tel["ws_subscribed"] = True
        if not tel["ws_sub_at"]:
            tel["ws_sub_at"] = time.time()

        new_positions: dict[str, dict] = {}
        for row in rows:
            parsed = self._parse_position_row(row)
            if parsed and abs(parsed["size"]) > 1e-12:
                new_positions[parsed["coin"]] = parsed

        if emit_signals:
            self._diff_positions(uid, new_positions)
        self._positions[uid] = new_positions

    def _diff_positions(self, uid: str, new_pos: dict[str, dict]) -> None:
        emit_signals = self._should_emit_signals_for(uid)
        old_pos = self._positions.get(uid, {})
        short = self._short(uid)
        tag = "COPY" if emit_signals else "WATCH"
        log = logger.info if emit_signals else logger.debug

        for coin, pos in new_pos.items():
            old = old_pos.get(coin)
            size, value = pos["size"], pos["value_usd"]

            if old is None and size > 0 and value >= self._min_copy_size_usd:
                if emit_signals:
                    self._emit(CopySignal(
                        trader=uid, coin=coin, symbol=pos["symbol"],
                        signal="COPY_OPEN_LONG", size_usd=value,
                        entry_price=pos["entry_px"], leverage=pos["leverage"],
                        pnl_pct=pos["pnl_pct"],
                    ))
                log(f"[BN-{tag}] 📈 OPEN LONG | {short} | {coin} | "
                    f"${value:,.0f} @ ${pos['entry_px']:.4f} | {pos['leverage']:.0f}x")
            elif old is None and size < 0:
                log(f"[BN-{tag}] 📉 OPEN SHORT | {short} | {coin} | "
                    f"${value:,.0f} @ ${pos['entry_px']:.4f} (info only)")
            elif old is not None and size > 0 and old["size"] > 0:
                change = (abs(size - old["size"]) / abs(old["size"]) * 100
                          if old["size"] != 0 else 100.0)
                if size > old["size"] * 1.05:
                    if emit_signals:
                        self._emit(CopySignal(
                            trader=uid, coin=coin, symbol=pos["symbol"],
                            signal="COPY_INCREASE", size_usd=value,
                            entry_price=pos["entry_px"], leverage=pos["leverage"],
                            pnl_pct=pos["pnl_pct"],
                        ))
                    log(f"[BN-{tag}] ⬆️  INCREASE | {short} | {coin} | +{change:.0f}% → ${value:,.0f}")
                elif size < old["size"] * 0.95:
                    if emit_signals:
                        self._emit(CopySignal(
                            trader=uid, coin=coin, symbol=pos["symbol"],
                            signal="COPY_DECREASE", size_usd=value,
                            entry_price=pos["entry_px"], leverage=pos["leverage"],
                            pnl_pct=pos["pnl_pct"],
                        ))
                    log(f"[BN-{tag}] ⬇️  DECREASE | {short} | {coin} | -{change:.0f}% → ${value:,.0f}")

        for coin, old in old_pos.items():
            if coin not in new_pos:
                if old["size"] > 0:
                    if emit_signals:
                        self._emit(CopySignal(
                            trader=uid, coin=coin, symbol=old["symbol"],
                            signal="COPY_CLOSE_LONG", size_usd=old["value_usd"],
                            entry_price=old["entry_px"], leverage=old["leverage"],
                            pnl_pct=old["pnl_pct"],
                        ))
                    log(f"[BN-{tag}] 🔴 CLOSE LONG | {short} | {coin} | PnL: {old['pnl_pct']:+.2f}%")
                elif old["size"] < 0:
                    log(f"[BN-{tag}] 🔴 CLOSE SHORT | {short} | {coin} | "
                        f"PnL: {old['pnl_pct']:+.2f}% (info only)")

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
            logger.warning("[BN-LB] Signal queue full")

    # ── Auto-Discovery ────────────────────────────────────────────────────

    async def _rescan_loop(self) -> None:
        if not AUTO_DISCOVER:
            return
        while True:
            await asyncio.sleep(RESCAN_INTERVAL_SEC)
            try:
                logger.debug("[BN-LB] Rescanning leaderboard for top intraday trader...")
                self._discovering = True
                try:
                    discovered = await asyncio.to_thread(self._discover_traders)
                finally:
                    self._discovering = False
                with self._uids_lock:
                    before = set(self._uids)
                    self._auto_uids = list(discovered)[:MAX_TRACKED_TRADERS]
                    self._rebuild_uid_list()
                    removed = before - set(self._uids)
                self._last_scan = time.time()
                for u in removed:
                    self._positions.pop(u, None)
                    self._telemetry.pop(u, None)
                logger.debug(
                    f"[BN-LB] Rescan fertig — {len(self._auto_uids)} "
                    f"Beobachtungs-Trader, {len(self._uids)} getrackt gesamt"
                )
            except Exception as e:
                logger.debug(f"[BN-LB] Rescan error: {e}")

    def _discover_traders(self) -> list[str]:
        candidates = find_intraday_traders(limit=MAX_TRACKED_TRADERS, verified_only=True)
        for c in candidates:
            self._nick_names[c.uid] = c.metrics.nick_name
            logger.debug(
                f"[BN-LB] Verifizierter Kandidat {self._short(c.uid)} | "
                f"Score={c.quality_score:.1f} | Tages-ROI={c.metrics.day_roi:.1f}% | "
                f"Tages-PnL=${c.metrics.day_pnl:,.0f}"
            )
        return [c.uid for c in candidates]

    # ── Cleanup ───────────────────────────────────────────────────────────

    async def cleanup_loop(self) -> None:
        while True:
            await asyncio.sleep(60)
            copied = [u for u in list(self._uids) if self._should_emit_signals_for(u)]
            if not copied:
                logger.debug("[BN-LB] Kein Trader ausgewaehlt — nichts zu tracken")
                continue
            st = self.status()
            logger.debug(
                f"[BN-LB] Tracking | {len(copied)} Trader gepollt | "
                f"Polls:{st['poll_count']} | frische Signale:{st['fresh_signals']}"
            )
