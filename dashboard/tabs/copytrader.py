"""
dashboard/tabs/copytrader.py — Binance Leaderboard Copy-Trader Dashboard

ARCHITEKTUR (wichtig zum Debuggen)
----------------------------------
Dashboard und Bot sind **zwei getrennte Prozesse**. Dieses Modul erzeugt
deshalb bewusst KEINEN laufenden Tracker: der Adapter hier dient nur zum
Schreiben der Trader-Auswahl und für den Leaderboard-Scanner. Alles, was
"live" aussieht, kommt aus der geteilten SQLite-DB (`src/utils/trader_store.py`),
die der Bot-Prozess befüllt:

    bot_heartbeat     → läuft der Bot? ist das REST-Polling aktiv? DRY_RUN?
    tracker_state     → pro Trader: WS-Subscription, Fills, Polls, Signale
    pipeline_events   → was die Pipeline mit jedem Signal gemacht hat
    positions.json    → meine offenen Binance-Positionen
    copy_history.json → meine abgeschlossenen Trades

Zeigt das Dashboard "Bot offline", läuft `main.py` nicht — dann trackt auch
nichts, egal was hier ausgewählt ist.

LAYOUT
------
  🏠 Übersicht     — mein Geld, mein Trading-Status, Bot-/Tracking-Zustand, Live-Feed
  👥 Meine Trader  — eine Zeile pro Trader; Header = Meta, aufgeklappt = Details
  🔍 Scanner       — Trader finden und mit Betrag übernehmen
  ⚙️ Setup         — Konfiguration und Pipeline-Protokoll zum Debuggen
"""
from __future__ import annotations

import datetime as _dt
import importlib
import json
import os
import time

import pandas as pd
import streamlit as st

from src.utils import trader_store

HISTORY_PATH = os.getenv("COPY_HISTORY_FILE", "copy_history.json")
CLOSE_REQUEST_PATH = os.getenv("COPY_CLOSE_REQUEST_FILE", "close_requests.json")

# Ab diesem Alter gilt der Bot-Heartbeat als tot (der Bot schreibt alle 3s).
HEARTBEAT_STALE_SEC = 15.0

_EVENT_ICON = {
    "SIGNAL": "📡", "BUY": "🟢", "SELL": "🔴", "SKIP": "⏭️", "ERROR": "❌",
    "SIMULATION": "🧪",
    "TRADER_ADDED": "➕", "TRADER_REMOVED": "➖", "TRADER_UPDATED": "✏️",
}


# ── Daten-Helpers ────────────────────────────────────────────────────────────

# JSON-Cache mit mtime-Invalidierung: verhindert, dass Fragmente (3-5s Rerun)
# positions.json / copy_history.json pro Trader-Zeile neu von Platte lesen.
_JSON_CACHE: dict[str, tuple[float, int, object]] = {}
_DERIVED_CACHE: dict[str, tuple[float, int, object]] = {}


def _load_json(path: str, fallback):
    try:
        stat = os.stat(path)
    except OSError:
        return fallback
    key = (stat.st_mtime, stat.st_size)
    cached = _JSON_CACHE.get(path)
    if cached and (cached[0], cached[1]) == key:
        return cached[2]
    try:
        with open(path, encoding="utf-8") as file:
            data = json.load(file)
    except (OSError, json.JSONDecodeError):
        return fallback
    _JSON_CACHE[path] = (stat.st_mtime, stat.st_size, data)
    return data


def _copy_positions() -> dict:
    """Eigene offene Binance-Positionen aus dem Copy-Trading (ohne Altlasten)."""
    raw = _load_json("positions.json", {})
    return {s: p for s, p in raw.items() if p.get("source") == "COPY"}


def _positions_index() -> dict[str, dict]:
    """{wallet_or_short: {symbol: pos}} — einmal berechnet, per mtime gecacht."""
    try:
        stat = os.stat("positions.json")
        cache_key = (stat.st_mtime, stat.st_size)
    except OSError:
        cache_key = (0.0, 0)
    cached = _DERIVED_CACHE.get("positions")
    if cached and (cached[0], cached[1]) == cache_key:
        return cached[2]  # type: ignore[return-value]
    index: dict[str, dict] = {}
    for sym, pos in _copy_positions().items():
        full = pos.get("trader_full") or ""
        owner = full or (pos.get("trader") or "")
        if owner:
            index.setdefault(owner, {})[sym] = pos
    _DERIVED_CACHE["positions"] = (cache_key[0], cache_key[1], index)
    return index


def _positions_of(wallet: str, short: str) -> dict:
    """Eigene offene Positionen, die genau diesem Leaderboard-Trader zuzuordnen sind."""
    index = _positions_index()
    return index.get(wallet) or index.get(short) or {}


def _history_index() -> dict[str, list[dict]]:
    """{wallet_or_short: [history_rows sorted desc]} — mtime-gecacht."""
    try:
        stat = os.stat(HISTORY_PATH)
        cache_key = (stat.st_mtime, stat.st_size)
    except OSError:
        cache_key = (0.0, 0)
    cached = _DERIVED_CACHE.get("history")
    if cached and (cached[0], cached[1]) == cache_key:
        return cached[2]  # type: ignore[return-value]
    rows = _load_json(HISTORY_PATH, [])
    index: dict[str, list[dict]] = {}
    if isinstance(rows, list):
        for row in rows:
            for k in (row.get("trader") or "", row.get("trader_short") or ""):
                if k:
                    index.setdefault(k, []).append(row)
        for lst in index.values():
            lst.sort(key=lambda h: h.get("closed_at", 0), reverse=True)
    _DERIVED_CACHE["history"] = (cache_key[0], cache_key[1], index)
    return index


def _history_of(wallet: str, short: str) -> list[dict]:
    index = _history_index()
    return index.get(wallet) or index.get(short) or []


def _request_close(symbols: list[str], reason: str) -> None:
    """Den Bot-Prozess bitten zu verkaufen — das Dashboard hat keinen Executor."""
    try:
        with open(CLOSE_REQUEST_PATH, "w", encoding="utf-8") as f:
            json.dump({"symbols": symbols, "reason": reason,
                       "requested_at": time.time()}, f, indent=2)
    except OSError as e:
        st.error(f"Verkaufs-Anfrage konnte nicht geschrieben werden: {e}")


def _bot_state() -> dict:
    """Heartbeat des Bot-Prozesses; `alive` ist False, wenn er zu alt ist."""
    hb = trader_store.get_heartbeat() or {}
    updated = float(hb.get("updated_at", 0) or 0)
    age = time.time() - updated if updated else float("inf")
    hb["age"] = age
    hb["alive"] = bool(hb) and age < HEARTBEAT_STALE_SEC
    return hb


@st.cache_data(ttl=30, show_spinner=False)
def _binance_balance() -> tuple[float, str]:
    """(freies USDT, Quelle). Die Abfrage ist lesend und funktioniert auch im Paper-Modus."""
    if not os.getenv("BINANCE_API_KEY") or not os.getenv("BINANCE_SECRET"):
        return 0.0, "paper" if _dry_run() else "error:missing_keys"
    try:
        from src.execution.binance_executor import get_account_balance
        return float(get_account_balance("USDT")), "live"
    except Exception as e:  # Keys fehlen, Netz weg, ...
        return 0.0, f"error:{e}"


def _dry_run() -> bool:
    return os.getenv("DRY_RUN", "True").lower() == "true"


def _default_size() -> float:
    try:
        return float(os.getenv("BINANCE_POSITION_SIZE_USDT", "10"))
    except ValueError:
        return 10.0


# ── Format-Helpers ───────────────────────────────────────────────────────────

def _short(wallet: str) -> str:
    return f"{wallet[:6]}...{wallet[-4:]}" if len(wallet) > 12 else wallet


def _fmt_hold(sec: float) -> str:
    if sec < 60:
        return f"{sec:.0f}s"
    if sec < 3600:
        return f"{sec / 60:.1f} min"
    if sec < 86400:
        return f"{sec / 3600:.1f} h"
    return f"{sec / 86400:.1f} d"


def _fmt_age(sec: float) -> str:
    if sec == float("inf") or sec != sec:
        return "nie"
    if sec < 60:
        return f"vor {sec:.0f}s"
    if sec < 3600:
        return f"vor {sec / 60:.0f}m"
    if sec < 86400:
        return f"vor {sec / 3600:.1f}h"
    return f"vor {sec / 86400:.1f}d"


def _kpi(label: str, value: str, sub: str, color: str = "") -> str:
    style = f'style="color:{color}"' if color else ""
    return (
        '<div class="kpi-card">'
        f'  <div class="label">{label}</div>'
        f'  <div class="value" {style}>{value}</div>'
        f'  <div class="sub">{sub}</div>'
        '</div>'
    )


def _banner(title: str, text: str, kind: str) -> None:
    color = {"amber": "#ffb400", "red": "#ff5c5c", "blue": "#3b8bff",
             "green": "#00e6a7"}.get(kind, "#3b8bff")
    st.markdown(
        f'<div class="insight-box" style="border-left-color:{color}">'
        f'  <div class="title" style="color:{color}">{title}</div>'
        f'  <div class="text">{text}</div>'
        '</div>',
        unsafe_allow_html=True,
    )


def _verification_links(wallet: str) -> list[tuple[str, str]]:
    from src.adapters.binance_leaderboard import binance_leaderboard_url
    return [
        ("Binance Leaderboard", binance_leaderboard_url(wallet)),
    ]


def _tracking_badge(track: dict | None, bot_alive: bool) -> tuple[str, str]:
    """(Badge-HTML, Kurztext) für den Tracking-Zustand eines Traders."""
    if not bot_alive:
        return '<span class="badge-loss">⛔ BOT AUS</span>', "main.py läuft nicht"
    if not track:
        return ('<span class="badge-warn">⏳ STARTET</span>',
                "Bot übernimmt gleich")
    if not track.get("ws_subscribed"):
        return '<span class="badge-warn">⚠️ NICHT AKTIV</span>', "Polling startet"
    last_poll = float(track.get("last_poll_at", 0) or 0)
    if last_poll and (time.time() - last_poll) > 120:
        return ('<span class="badge-warn">⚠️ POLL ALT</span>',
                _fmt_age(time.time() - last_poll))
    return ('<span class="badge-profit">● LIVE POLLING</span>',
            f"{int(track.get('poll_count', 0))} Abrufe")


# ── Adapter (nur Steuerung + Scanner, kein Tracking) ─────────────────────────

_ADAPTER_SCHEMA_VERSION = 3


@st.cache_resource
def _get_adapter(schema_version: int):
    """Adapter-Instanz OHNE `start()`.

    Das Tracking gehört dem Bot-Prozess. Würde das Dashboard einen zweiten
    Tracker starten, hätte man doppelte API-Last und zwei Wahrheiten.
    """
    del schema_version
    from src.adapters import binance_leaderboard
    module = importlib.reload(binance_leaderboard)
    return module.BinanceLeaderboardTrader(publish_state=False)


# ── Entry ────────────────────────────────────────────────────────────────────

def render():
    adapter = _get_adapter(_ADAPTER_SCHEMA_VERSION)
    adapter.refresh()

    st.markdown('<h2 style="margin-bottom:0;">🔄 Binance Copy-Trader</h2>',
                unsafe_allow_html=True)
    mode = "📝 PAPER (DRY-RUN)" if _dry_run() else "🔴 LIVE"
    st.caption(f"Signale vom Binance-Leaderboard · Ausführung auf Binance Spot · Modus: {mode}")

    bot = _bot_state()
    if not bot["alive"]:
        _banner(
            "⛔ Bot-Prozess läuft nicht",
            "Auswahl und Beträge werden gespeichert, aber es wird nichts "
            "getrackt und nichts gehandelt. Starte den Bot mit "
            "<code>python main.py</code>."
            + (f" Letztes Lebenszeichen {_fmt_age(bot['age'])}."
               if bot.get("updated_at") else ""),
            "red",
        )

    tab_home, tab_traders, tab_scan, tab_history, tab_setup = st.tabs(
        ["🏠 Übersicht", "👥 Meine Trader", "🔍 Scanner", "📜 Historie", "⚙️ Setup"]
    )
    with tab_home:
        _render_overview()
    with tab_traders:
        _render_my_traders(adapter)
    with tab_scan:
        _render_scanner(adapter)
    with tab_history:
        _render_history()
    with tab_setup:
        _render_setup(adapter)


# ── 🏠 Übersicht ─────────────────────────────────────────────────────────────

def _render_overview():
    """Meine Zahlen zuerst, dann der Nachweis, dass Bot und Tracking laufen."""

    @st.fragment(run_every="3s")
    def _live():
        bot = _bot_state()
        traders = trader_store.list_traders()
        tracking = trader_store.get_tracker_state()
        copied = [t for t in traders if t["is_copied"]]
        my_open = _copy_positions()
        history = _load_json(HISTORY_PATH, [])
        history = history if isinstance(history, list) else []

        health = bot.get("api_health", "ok")
        if bot["alive"] and health == "rate_limited":
            _banner("⚠️ Binance-Leaderboard rate-limited",
                    "Der Bot wartet auf das Rate-Limit-Reset. Tracking läuft weiter, "
                    "Kennzahlen kommen kurzzeitig aus dem Cache.", "amber")
        elif bot["alive"] and health == "unreachable":
            _banner("🔴 Binance-Leaderboard unerreichbar",
                    "Netzwerkfehler zu binance.com — der Bot versucht "
                    "automatisch erneut zu verbinden.", "red")

        # ── Zeile 1: Mein Geld ──────────────────────────────────────────
        exposure = sum(float(p.get("size_usdt", 0)) for p in my_open.values())
        realized = sum(float(h.get("pnl_usdt", 0)) for h in history)
        wins = sum(1 for h in history if float(h.get("pnl_usdt", 0)) > 0)
        win_rate = wins / len(history) if history else 0.0

        balance, source = _binance_balance()
        if source == "live":
            bal_value, bal_sub, bal_color = f"${balance:,.2f}", "freies USDT auf Binance", ""
        elif source == "paper":
            bal_value, bal_sub, bal_color = "—", "keine Binance-API-Schlüssel", "#946200"
        else:
            bal_value, bal_sub, bal_color = "—", "Keine API-Keys / Fehler", "#ff5c5c"

        c1, c2, c3, c4 = st.columns(4)
        c1.markdown(_kpi("💵 Binance-Guthaben", bal_value, bal_sub, bal_color),
                    unsafe_allow_html=True)
        c2.markdown(_kpi("📦 Meine offenen Trades", str(len(my_open)),
                         f"${exposure:,.0f} eingesetzt"), unsafe_allow_html=True)
        c3.markdown(_kpi("💰 Realisierter PnL", f"${realized:+,.2f}",
                         f"{len(history)} geschlossen · {win_rate:.0%} Win-Rate",
                         "#00e6a7" if realized >= 0 else "#ff5c5c"), unsafe_allow_html=True)
        c4.markdown(_kpi("👥 Kopierte Trader", str(len(copied)),
                         f"{len(traders)} insgesamt gewählt"), unsafe_allow_html=True)

        st.markdown("<br>", unsafe_allow_html=True)

        # ── Zeile 2: Läuft die Strategie wirklich? ──────────────────────
        if bot["alive"]:
            bot_val, bot_sub, bot_col = "● LÄUFT", f"PID {bot.get('pid', '?')}", "#00e6a7"
        else:
            bot_val, bot_sub, bot_col = "● AUS", "python main.py starten", "#ff5c5c"

        poll_ok = sum(1 for t in copied
                  if tracking.get(t["wallet"], {}).get("last_poll_at"))
        if not bot["alive"]:
            ws_val, ws_sub, ws_col = "—", "Bot offline", "#64748b"
        elif not copied:
            ws_val, ws_sub, ws_col = "—", "kein Trader ausgewählt", "#64748b"
        elif poll_ok == len(copied):
            ws_val, ws_sub, ws_col = (f"{poll_ok}/{len(copied)}",
                                      "alle Trader werden abgefragt", "#00e6a7")
        else:
            ws_val, ws_sub, ws_col = (f"{poll_ok}/{len(copied)}",
                                      "Polling unvollständig", "#ffb400")

        last_fill = max(
            (float(tracking.get(t["wallet"], {}).get("last_fill_at", 0) or 0)
             for t in copied),
            default=0.0,
        )
        d1, d2, d3 = st.columns(3)
        d1.markdown(_kpi("🤖 Bot-Prozess", bot_val, bot_sub, bot_col),
                    unsafe_allow_html=True)
        d2.markdown(_kpi("🔁 REST-Tracking", ws_val, ws_sub, ws_col),
                    unsafe_allow_html=True)
        d3.markdown(_kpi("⚡ Letzter Trader-Fill",
                         _fmt_age(time.time() - last_fill) if last_fill else "—",
                         "Aktivität der kopierten Trader"), unsafe_allow_html=True)

        st.markdown("<br>", unsafe_allow_html=True)

        # ── Meine offenen Trades + Pipeline-Feed ────────────────────────
        left, right = st.columns([3, 2])
        with left:
            st.markdown('<div class="section-header">📦 Meine offenen Trades</div>',
                        unsafe_allow_html=True)
            if not my_open:
                st.info("Keine offenen Positionen. Sobald ein kopierter Trader "
                        "ein Long öffnet, erscheint der Trade hier.")
            else:
                now = time.time()
                st.dataframe(
                    pd.DataFrame([{
                        "Symbol":  sym,
                        "Trader":  p.get("trader", "?"),
                        "Einsatz": float(p.get("size_usdt", 0)),
                        "Entry":   float(p.get("entry_price", 0)),
                        "Alter":   _fmt_age(now - float(p.get("opened_at", now))),
                        "Modus":   "Paper" if p.get("dry_run") else "Live",
                    } for sym, p in my_open.items()]),
                    width="stretch", hide_index=True,
                    column_config={
                        "Einsatz": st.column_config.NumberColumn(format="$%.0f"),
                        "Entry":   st.column_config.NumberColumn(format="$%.4f"),
                    },
                )

        with right:
            st.markdown('<div class="section-header">📡 Was die Pipeline tut</div>',
                        unsafe_allow_html=True)
            _event_feed(limit=12)

    _live()


def _event_feed(limit: int = 12, wallet: str = "") -> None:
    """Live-Protokoll der Pipeline-Entscheidungen aus der DB."""
    events = trader_store.recent_events(limit=limit, wallet=wallet)
    if not events:
        st.info(
            "Noch keine Pipeline-Ereignisse. Sie entstehen, sobald ein "
            "kopierter Trader eine Position eröffnet oder schließt."
        )
        return
    now = time.time()
    html = '<div class="log-terminal" style="max-height:340px">'
    for e in events:
        html += (
            '<div class="event-row">'
            f'<span class="event-icon">{_EVENT_ICON.get(e["kind"], "•")}</span>'
            f'<span class="event-time">{_fmt_age(now - e["ts"])}</span>'
            f'<span class="event-sym">{e["symbol"] or _short(e["wallet"])}</span>'
            f'<span class="event-msg">{e["message"]}</span>'
            '</div>'
        )
    st.markdown(html + "</div>", unsafe_allow_html=True)


# ── 👥 Meine Trader ──────────────────────────────────────────────────────────

def _render_my_traders(adapter):
    st.markdown('<div class="section-header">👥 Meine Trader</div>',
                unsafe_allow_html=True)
    st.caption(
        "Hier stehen ausschließlich Trader, die du selbst übernommen hast. "
        "**Kopiert** = der Bot handelt ihn (DRY-RUN = Paper). "
        "**Angepinnt** = wird überwacht, aber nicht automatisch gehandelt."
    )

    with st.form("add_trader_form", clear_on_submit=True):
        a1, a2, a3 = st.columns([3, 1, 1])
        new_wallet = a1.text_input("Wallet hinzufügen", label_visibility="collapsed",
                                   placeholder="0x1234…abcd")
        new_size = a2.number_input("Betrag $", min_value=1.0, value=_default_size(),
                                   step=5.0, label_visibility="collapsed")
        added = a3.form_submit_button("➕ Kopieren", type="primary", width="stretch")
    if added:
        wallet = new_wallet.strip()
        if not wallet.startswith("0x") or len(wallet) < 10:
            st.error("Ungültige Wallet-Adresse.")
        else:
            adapter.set_copy_size(wallet, new_size)
            adapter.activate_wallet(wallet)
            st.rerun()

    traders_snapshot = trader_store.list_traders()
    if not traders_snapshot:
        st.info(
            "Noch kein Trader übernommen. Gehe zum **🔍 Scanner**, finde einen "
            "passenden Trader und übernimm ihn mit deinem Wunschbetrag."
        )
        return

    _render_bulk_actions(adapter, traders_snapshot)

    @st.fragment(run_every="3s")
    def _live_trader_rows() -> None:
        bot = _bot_state()
        bot_alive = bot["alive"]
        traders = trader_store.list_traders()
        tracking = trader_store.get_tracker_state()

        if bot_alive:
            mode = "DRY-RUN" if _dry_run() else "LIVE"
            pipe_html = (
                f'<span class="badge-profit">● PIPELINE LÄUFT · {mode}</span>'
                f'<em>Heartbeat {_fmt_age(bot["age"])}</em>'
            )
        else:
            pipe_html = (
                '<span class="badge-loss">⛔ PIPELINE AUS</span>'
                '<em>starte <code>python3 main.py</code></em>'
            )
        st.markdown(
            f'<div class="pipeline-strip">{pipe_html}</div>',
            unsafe_allow_html=True,
        )

        for trader in traders:
            wallet = trader["wallet"]
            short = _short(wallet)
            track = tracking.get(wallet)
            my_open = _positions_of(wallet, short)
            history = _history_of(wallet, short)
            realized = sum(float(h.get("pnl_usdt", 0)) for h in history)
            exposure = sum(float(p.get("size_usdt", 0)) for p in my_open.values())
            lb_positions = (track or {}).get("positions", {}) or {}
            trader_upnl = sum(float(p.get("unrealized_pnl", 0))
                              for p in lb_positions.values())

            _trader_row_compact(trader, short, track, bot_alive,
                                my_open, realized, exposure, trader_upnl,
                                len(history))

            with st.expander("Details, Steuerung, Verifikation", expanded=False):
                _trader_controls(adapter, trader, short)
                t_mine, t_track, t_detail, t_verify = st.tabs(
                    ["💼 Meine Trades", "📡 Tracking", "🎯 Trader-Details", "🔗 Verifikation"]
                )
                with t_mine:
                    _tab_my_trades(my_open, history, realized)
                with t_track:
                    _tab_tracking(wallet, track, bot_alive)
                with t_detail:
                    _tab_trader_detail(adapter, wallet, lb_positions)
                with t_verify:
                    _tab_verify(wallet)

    _live_trader_rows()


def _render_bulk_actions(adapter, traders: list[dict]) -> None:
    wallets = [t["wallet"] for t in traders]
    copied = [t for t in traders if t["is_copied"]]
    open_syms = sorted({sym for w in wallets for sym in _positions_of(w, _short(w))})

    st.markdown(
        f'<div class="badge-info">{len(traders)} Trader · {len(copied)} kopiert · '
        f'{len(open_syms)} offene Position(en)</div>',
        unsafe_allow_html=True,
    )
    with st.expander("⛔ Alles stoppen / entfernen", expanded=False):
        st.caption(
            "**Stoppen** beendet nur neue Copy-Orders. **Entfernen** löscht "
            "zusätzlich Betrag, Anpinnung und Tracking. **Verkaufen** schließt "
            "die offenen Positionen — der Bot führt das aus, sobald er die "
            "Anfrage sieht (max. 2s)."
        )
        confirm = st.checkbox("Ja, ausführen", key="bulk_confirm")

        b1, b2 = st.columns(2)
        if b1.button(f"⛔ Alles stoppen, verkaufen & entfernen ({len(open_syms)} Position(en))",
                     key="bulk_nuke", type="primary",
                     disabled=not confirm, width="stretch"):
            if open_syms:
                _request_close(open_syms, "MANUAL_STOP_ALL")
            for wallet in wallets:
                adapter.remove_trader(wallet)
            st.success(
                f"{len(wallets)} Trader entfernt. "
                + (f"Verkauf für {', '.join(open_syms)} angefordert."
                   if open_syms else "Es waren keine Positionen offen.")
            )
            st.rerun()

        if b2.button(f"⏸️ Nur Copy stoppen ({len(copied)})", key="bulk_stop",
                     disabled=not confirm or not copied, width="stretch"):
            for trader in copied:
                adapter.deactivate_wallet(trader["wallet"])
            st.rerun()

        if open_syms:
            st.caption("Betroffene Positionen: " + ", ".join(open_syms))
        if not _dry_run():
            st.warning(
                "🔴 Im LIVE-Modus werden Positionen von ihrer OCO-Order verwaltet. "
                "Der Bot kann sie nicht automatisch schließen — bitte direkt auf "
                "Binance verkaufen."
            )


def _trader_row_compact(trader: dict, short: str, track: dict | None,
                        bot_alive: bool, my_open: dict, realized: float,
                        exposure: float, trader_upnl: float,
                        closed_count: int) -> None:
    """Kompakte Trader-Zeile im Scanner-Stil — Metadaten + Live-Backend-Status."""
    wallet = trader["wallet"]
    nick = trader.get("nick_name") or short
    size = float(trader["size_usdt"] or 0) or _default_size()

    status_pill = (
        '<span class="badge-profit">KOPIERT</span>' if trader["is_copied"]
        else '<span class="badge-info">👁 BEOBACHTET</span>'
    )
    focus_pill = (
        '<span class="badge-info">📌 pin</span>' if trader["is_focus"] else ""
    )
    verification = trader_store.get_verification(wallet)
    if verification:
        verification_age = time.time() - float(verification["verified_at"])
        score = float(verification["quality_score"])
        if verification_age <= 7 * 86400:
            verified_pill = (
                f'<span class="badge-profit">✅ {score:.1f}</span>'
            )
        else:
            verified_pill = '<span class="badge-warn">⚠️ 7d+</span>'
    else:
        verified_pill = '<span class="badge-warn">⚠️ n.v.</span>'

    if trader["is_copied"]:
        live_badge, live_sub = _tracking_badge(track, bot_alive)
    else:
        live_badge = '<span class="badge-info">◌ WATCH</span>'
        live_sub = "kein Copy"

    now = time.time()
    last_poll = float((track or {}).get("last_poll_at", 0) or 0)
    poll_age = _fmt_age(now - last_poll) if last_poll else "—"
    signal_count = int((track or {}).get("signal_count", 0))
    fill_count = int((track or {}).get("fill_count", 0))
    last_signal = float((track or {}).get("last_signal_at", 0) or 0)
    sig_age = _fmt_age(now - last_signal) if last_signal else "—"

    pnl_cls = "profit" if realized >= 0 else "loss"
    upnl_cls = "profit" if trader_upnl >= 0 else "loss"
    open_count = len(my_open)

    st.markdown(
        f'''
        <div class="scan-row">
          <div class="scan-row-main">
            <div class="scan-row-name">
              <span class="scan-name">{nick}</span>
              <span class="scan-short">{short}</span>
              {status_pill}{focus_pill}{verified_pill}
            </div>
            <div class="scan-row-metrics">
              <span class="scan-metric"><em>Betrag</em><b>${size:,.0f}</b></span>
              <span class="scan-metric"><em>Mein PnL</em><b class="{pnl_cls}">${realized:+,.2f}</b></span>
              <span class="scan-metric"><em>Offen</em><b>{open_count} · ${exposure:,.0f}</b></span>
              <span class="scan-metric"><em>Trader uPnL</em><b class="{upnl_cls}">${trader_upnl:+,.0f}</b></span>
              <span class="scan-metric"><em>Signals</em><b>{signal_count} · {sig_age}</b></span>
              <span class="scan-metric"><em>Fills</em><b>{fill_count}</b></span>
              <span class="scan-metric"><em>Poll</em><b>{poll_age}</b></span>
              <span class="scan-metric"><em>Trades</em><b>{closed_count}</b></span>
              <span class="scan-live">{live_badge}<em>{live_sub}</em></span>
            </div>
          </div>
        </div>
        ''',
        unsafe_allow_html=True,
    )


def _trader_controls(adapter, trader: dict, short: str) -> None:
    """Zeilen-Steuerung: Betrag ändern, Copy an/aus, anpinnen, entfernen."""
    wallet = trader["wallet"]
    size = float(trader["size_usdt"] or 0) or _default_size()

    c1, c2, c3, c4, c5 = st.columns([2, 1.4, 1, 1, 1])
    amount = c1.number_input(
        "Betrag pro Trade (USDT)", min_value=1.0, max_value=100000.0,
        value=float(size), step=5.0, key=f"amt_{wallet}",
        help="Gilt nur für diesen Trader und überschreibt "
             "BINANCE_POSITION_SIZE_USDT aus der .env.",
    )
    copy_on = c2.checkbox("Copy aktiv", value=bool(trader["is_copied"]),
                          key=f"copy_{wallet}")

    c3.markdown("<div style='height:28px'></div>", unsafe_allow_html=True)
    if c3.button("💾 Speichern", key=f"save_{wallet}", type="primary", width="stretch"):
        adapter.set_copy_size(wallet, amount)
        if copy_on and not trader["is_copied"]:
            adapter.activate_wallet(wallet)
        elif not copy_on and trader["is_copied"]:
            adapter.deactivate_wallet(wallet)
        st.rerun()

    c4.markdown("<div style='height:28px'></div>", unsafe_allow_html=True)
    pin_label = "📌 Lösen" if trader["is_focus"] else "📌 Anpinnen"
    if c4.button(pin_label, key=f"pin_{wallet}", width="stretch"):
        if trader["is_focus"]:
            adapter.clear_focus_wallet()
        else:
            adapter.set_focus_wallet(wallet)
        st.rerun()

    c5.markdown("<div style='height:28px'></div>", unsafe_allow_html=True)
    if c5.button("🗑️ Entfernen", key=f"del_{wallet}", width="stretch"):
        open_syms = list(_positions_of(wallet, short))
        if open_syms:
            _request_close(open_syms, "MANUAL_REMOVE_TRADER")
        adapter.remove_trader(wallet)
        st.rerun()

    mode = "📝 DRY-RUN — Orders werden simuliert" if _dry_run() else "🔴 LIVE — echtes Geld"
    st.caption(
        f"{mode} · Konsolen-Präfix `[BN-{'COPY' if trader['is_copied'] else 'WATCH'}]` "
        f"für {short} · Entfernen verkauft offene Positionen dieses Traders"
    )


# ── Trader-Details (Sub-Tabs) ────────────────────────────────────────────────

def _tab_my_trades(my_open: dict, history: list[dict], realized: float) -> None:
    st.markdown("**Offene Positionen**")
    if not my_open:
        st.caption("Keine offene Position aus diesem Trader.")
    else:
        now = time.time()
        st.dataframe(
            pd.DataFrame([{
                "Symbol":   sym,
                "Einsatz":  float(p.get("size_usdt", 0)),
                "Entry":    float(p.get("entry_price", 0)),
                "Menge":    float(p.get("qty", 0)),
                "Alter":    _fmt_age(now - float(p.get("opened_at", now))),
                "Coin":     p.get("coin", ""),
                "Hebel":    float(p.get("leverage", 0)),
                "Modus":    "Paper" if p.get("dry_run") else "Live",
            } for sym, p in my_open.items()]),
            width="stretch", hide_index=True,
            column_config={
                "Einsatz":  st.column_config.NumberColumn(format="$%.0f"),
                "Entry":    st.column_config.NumberColumn(format="$%.4f"),
                "Menge":    st.column_config.NumberColumn(format="%.6f"),
                "Hebel":    st.column_config.NumberColumn(format="%.0fx"),
            },
        )

    st.markdown("**Trade-Historie**")
    if not history:
        st.caption("Noch keine abgeschlossenen Trades aus diesem Trader.")
        return

    wins = sum(1 for h in history if float(h.get("pnl_usdt", 0)) > 0)
    m1, m2, m3 = st.columns(3)
    m1.metric("Realisiert", f"${realized:+,.2f}")
    m2.metric("Trades", str(len(history)))
    m3.metric("Win-Rate", f"{wins / len(history):.0%}")

    st.dataframe(
        pd.DataFrame([{
            "Geschlossen": _dt.datetime.fromtimestamp(h.get("closed_at", 0)).strftime("%d.%m %H:%M")
                           if h.get("closed_at") else "—",
            "Symbol":  h.get("symbol", ""),
            "Entry":   float(h.get("entry_price", 0)),
            "Exit":    float(h.get("exit_price", 0)),
            "Einsatz": float(h.get("size_usdt", 0)),
            "PnL $":   float(h.get("pnl_usdt", 0)),
            "PnL %":   float(h.get("pnl_pct", 0)),
            "Haltezeit": _fmt_hold(max(0.0, h.get("closed_at", 0) - h.get("opened_at", 0))),
            "Grund":   h.get("reason", ""),
        } for h in history]),
        width="stretch", hide_index=True,
        column_config={
            "Entry":   st.column_config.NumberColumn(format="$%.4f"),
            "Exit":    st.column_config.NumberColumn(format="$%.4f"),
            "Einsatz": st.column_config.NumberColumn(format="$%.0f"),
            "PnL $":   st.column_config.NumberColumn(format="$%+.2f"),
            "PnL %":   st.column_config.NumberColumn(format="%+.2f%%"),
        },
    )


def _tab_tracking(wallet: str, track: dict | None, bot_alive: bool) -> None:
    """Nachweis, dass dieser Trader wirklich live verfolgt wird."""
    if not bot_alive:
        st.error(
            "Der Bot-Prozess läuft nicht — für diesen Trader wird aktuell nichts "
            "getrackt und nichts gehandelt. Starte `python main.py`."
        )
        return
    if not track:
        st.warning(
            "Der Bot hat diesen Trader noch nicht übernommen. Er liest die "
            "Auswahl alle paar Sekunden neu ein — kurz warten."
        )
        return

    now = time.time()
    polling_ok = bool(track.get("last_poll_at"))
    c1, c2, c3, c4 = st.columns(4)
    c1.markdown(_kpi("🔁 REST-Polling", "aktiv" if polling_ok else "wartet",
                     _fmt_age(now - float(track.get("last_poll_at") or now)) if polling_ok else "—",
                     "#00e6a7" if polling_ok else "#ff5c5c"), unsafe_allow_html=True)
    c2.markdown(_kpi("⚡ Fills gesehen", str(int(track.get("fill_count", 0))),
                     _fmt_age(now - float(track["last_fill_at"]))
                     if track.get("last_fill_at") else "noch keiner"),
                unsafe_allow_html=True)
    c3.markdown(_kpi("🔁 REST-Polls", str(int(track.get("poll_count", 0))),
                     _fmt_age(now - float(track["last_poll_at"]))
                     if track.get("last_poll_at") else "nie"), unsafe_allow_html=True)
    c4.markdown(_kpi("📶 Copy-Signale", str(int(track.get("signal_count", 0))),
                     _fmt_age(now - float(track["last_signal_at"]))
                     if track.get("last_signal_at") else "noch keins"),
                unsafe_allow_html=True)

    if track.get("poll_error"):
        st.error(f"Letzter Poll-Fehler: {track['poll_error']}")

    if track.get("is_copied") and polling_ok:
        if int(track.get("signal_count", 0)) == 0:
            st.success(
                "Copy-Pipeline ist bereit. Der Ausgangszustand wurde eingelesen; "
                "der Bot wartet auf die nächste neue Long-Eröffnung oder eine "
                "Long-Erhöhung über 5 %. Bereits offene Trader-Positionen werden "
                "nicht nachträglich gekauft."
            )
        else:
            st.success("Copy-Pipeline ist aktiv und hat bereits Signale verarbeitet.")

    st.caption(
        f"Zuletzt vom Bot aktualisiert: "
        f"{_fmt_age(now - float(track.get('updated_at') or now))} · "
        f"Copy-Status im Bot: "
        f"{'aktiv' if track.get('is_copied') else 'nur Beobachtung'}"
    )

    st.markdown("**Copy-Pipeline sicher testen**")
    if not _dry_run():
        st.warning("Simulation gesperrt: Sie ist ausschließlich mit `DRY_RUN=True` erlaubt.")
    else:
        coin = st.text_input(
            "Binance-Coin", value="BTC", key=f"sim_coin_{wallet}",
            help="Zum Beispiel BTC oder ETH. Geprüft und gehandelt wird COINUSDT.",
        ).strip().upper()
        symbol = f"{coin}USDT"
        own_positions = _positions_of(wallet, _short(wallet))
        disabled = not bool(track.get("is_copied")) or not coin.isalnum()
        buy_col, sell_col = st.columns(2)
        if buy_col.button(
            f"🧪 PAPER BUY {symbol}", key=f"sim_buy_{wallet}",
            type="primary", width="stretch", disabled=disabled or symbol in own_positions,
        ):
            request_id = trader_store.enqueue_simulation(wallet, "BUY", coin)
            st.success(f"Testauftrag #{request_id} an den Bot gesendet.")
            st.rerun()
        if sell_col.button(
            f"🧪 PAPER SELL {symbol}", key=f"sim_sell_{wallet}",
            width="stretch", disabled=disabled or symbol not in own_positions,
        ):
            request_id = trader_store.enqueue_simulation(wallet, "SELL", coin)
            st.success(f"Testauftrag #{request_id} an den Bot gesendet.")
            st.rerun()
        if disabled:
            st.caption("Aktiviere zuerst **Copy aktiv** und speichere die Trader-Zeile.")
        elif symbol not in own_positions:
            st.caption("BUY durchläuft die echte Copy-Pipeline. SELL wird erst aktiv, wenn die Paper-Position offen ist.")
        else:
            st.caption(f"{symbol} ist als Paper-Position offen; SELL schließt sie über denselben Signal-Handler.")

        @st.fragment(run_every="2s")
        def _simulation_status() -> None:
            simulations = trader_store.recent_simulations(wallet=wallet, limit=5)
            if not simulations:
                return
            labels = {"pending": "wartet", "processing": "läuft",
                      "done": "erfolgreich", "failed": "fehlgeschlagen"}
            st.dataframe(
                pd.DataFrame([{
                    "ID": row["id"],
                    "Zeit": _dt.datetime.fromtimestamp(row["created_at"]).strftime("%H:%M:%S"),
                    "Aktion": f"{row['action']} {row['coin']}USDT",
                    "Status": labels.get(row["status"], row["status"]),
                    "Ergebnis": row["result"],
                } for row in simulations]),
                width="stretch", hide_index=True,
            )

        _simulation_status()

    st.markdown("**Pipeline-Ereignisse zu diesem Trader**")
    _event_feed(limit=25, wallet=wallet)


def _tab_trader_detail(adapter, wallet: str, live_positions: dict) -> None:
    if live_positions:
        st.markdown("**Aktuelle Positionen des Traders (vom Bot getrackt)**")
        st.dataframe(
            pd.DataFrame([{
                "Coin":  coin,
                "Seite": "LONG" if float(p.get("size", 0)) > 0 else "SHORT",
                "Wert":  float(p.get("value_usd", 0)),
                "Entry": float(p.get("entry_px", 0)),
                "Hebel": float(p.get("leverage", 0)),
                "PnL %": float(p.get("pnl_pct", 0)),
                "PnL $": float(p.get("unrealized_pnl", 0)),
            } for coin, p in live_positions.items()]),
            width="stretch", hide_index=True,
            column_config={
                "Wert":  st.column_config.NumberColumn(format="$%.0f"),
                "Entry": st.column_config.NumberColumn(format="$%.4f"),
                "Hebel": st.column_config.NumberColumn(format="%.0fx"),
                "PnL %": st.column_config.NumberColumn(format="%+.2f%%"),
                "PnL $": st.column_config.NumberColumn(format="$%+.0f"),
            },
        )
    else:
        st.caption("Aktuell keine offene Position bei diesem Trader.")

    if not st.button("📊 Details laden", key=f"deep_{wallet}"):
        st.caption("Lädt öffentlich geteilte Positionen vom Binance-Leaderboard — ein API-Call.")
        return

    with st.spinner("Lade Binance-Leaderboard-Daten …"):
        try:
            focus = adapter.get_trader_focus(wallet)
        except Exception as e:
            st.error(f"Daten konnten nicht geladen werden: {e}")
            return
    if focus is None:
        st.error("Ungültige Trader-UID.")
        return

    st.markdown(f"**{focus.get('nick_name') or 'Unbenannt'}**")
    st.caption(
        f"Getrackt: {'ja' if focus['is_tracked'] else 'nein'} · "
        f"Aktiv kopiert: {'ja' if focus['is_active'] else 'nein'}"
    )
    positions = focus.get("positions", {})
    if not positions:
        st.caption("Keine öffentlich geteilten Positionen gefunden.")
    else:
        st.dataframe(
            pd.DataFrame([{
                "Coin":  coin,
                "Seite": "LONG" if float(p.get("size", 0)) > 0 else "SHORT",
                "Wert":  float(p.get("value_usd", 0)),
                "Entry": float(p.get("entry_px", 0)),
                "Hebel": float(p.get("leverage", 0)),
                "PnL %": float(p.get("pnl_pct", 0)),
                "PnL $": float(p.get("unrealized_pnl", 0)),
            } for coin, p in positions.items()]),
            width="stretch", hide_index=True,
            column_config={
                "Wert":  st.column_config.NumberColumn(format="$%.0f"),
                "Entry": st.column_config.NumberColumn(format="$%.4f"),
                "Hebel": st.column_config.NumberColumn(format="%.0fx"),
                "PnL %": st.column_config.NumberColumn(format="%+.2f%%"),
                "PnL $": st.column_config.NumberColumn(format="$%+.0f"),
            },
        )
    st.markdown(
        f'<a href="{focus["leaderboard_url"]}" target="_blank" '
        'style="text-decoration:none"><span class="badge-info">'
        '🔗 Binance-Leaderboard-Profil öffnen ↗</span></a>',
        unsafe_allow_html=True,
    )


def _tab_verify(wallet: str) -> None:
    verification = trader_store.get_verification(wallet)
    if verification:
        metrics = verification["metrics"]
        verified_at = _dt.datetime.fromtimestamp(
            verification["verified_at"]
        ).strftime("%d.%m.%Y %H:%M")
        st.markdown(f"**Scanner-Verifikation vom {verified_at}**")
        v1, v2, v3, v4, v5 = st.columns(5)
        v1.metric("Quality-Score", f"{verification['quality_score']:.1f}/100")
        v2.metric("Tages-ROI", f"{float(metrics.get('day_roi', 0)):+.1f}%")
        v3.metric("Tages-PnL", f"${float(metrics.get('day_pnl', 0)):+,.0f}")
        v4.metric("7T-ROI", f"{float(metrics.get('week_roi', 0) or 0):+.1f}%")
        v5.metric("30T-ROI", f"{float(metrics.get('month_roi', 0) or 0):+.1f}%")
        st.caption(
            f"{int(metrics.get('follower_count', 0))} Follower · "
            f"Positionen {'öffentlich geteilt' if metrics.get('position_shared') else 'nicht geteilt'}"
        )
    else:
        st.warning(
            "Für diese Trader-UID liegt kein Scanner-Verifikationsbefund vor. "
            "Manuell hinzugefügte Trader gelten nicht als verifiziert."
        )

    st.caption(
        "Das Binance-Leaderboard-Profil dient als unabhängige Gegenprüfung der "
        "öffentlichen Trader-Daten. Quantitative Verifikation garantiert keine "
        "zukünftigen Gewinne."
    )
    st.caption(
        "Das Binance-Leaderboard-Profil dient als unabhängige Gegenprüfung der "
        "öffentlichen Trader-Daten. Quantitative Verifikation garantiert keine "
        "zukünftigen Gewinne."
    )
    html = " · ".join(
        f'<a href="{url}" target="_blank" style="text-decoration:none">'
        f'<span class="badge-info">{label} ↗</span></a>'
        for label, url in _verification_links(wallet)
    )
    st.markdown(f'<div style="margin:10px 0">{html}</div>', unsafe_allow_html=True)
    st.code(wallet, language=None)


# ── 🔍 Scanner ───────────────────────────────────────────────────────────────

def _render_scanner(adapter):
    st.markdown('<div class="section-header">🔍 Passenden Trader finden</div>',
                unsafe_allow_html=True)
    st.caption(
        "Es erscheinen nur quantitativ verifizierte Trader vom Binance-Leaderboard. "
        "Das ist eine Prüfung der öffentlichen Handelsdaten, keine Identitätsprüfung "
        "der Person. 'Intraday' heißt: gutes Ergebnis HEUTE (Tages-Leaderboard), "
        "geprüft gegen 7-Tage- und 30-Tage-Konsistenz."
    )
    st.info(
        "Verifikations-Gates: Mindest-Tages-ROI/-PnL, Positionen öffentlich "
        "geteilt, positive 7T/30T-Performance, innerhalb 24h aktiv, "
        "Mindest-Follower."
    )

    _scan_defaults = {
        "scan_min_day_roi": 0.0, "scan_min_day_pnl": 0.0,
        "scan_min_followers": 0, "scan_min_win_rate": 0.0,
        "scan_min_open_pos": 0, "scan_limit": 30, "scan_only_shared": True,
    }
    for _k, _v in _scan_defaults.items():
        st.session_state.setdefault(_k, _v)

    st.caption(
        "Tipp für einen End-to-End Test: alle Gates auf 0 lassen (auch negative "
        "Tages-ROI/-PnL erlaubt) und **öffentliche Positionen** anlassen — sonst "
        "kann der Bot keine Öffnungen/Änderungen sehen. Beim Klick auf **Copy "
        "übernehmen** repliziert der Bot beim nächsten Poll (~5 s) alle aktuell "
        "offenen Longs des Traders mit deinem Betrag."
    )
    st.caption(
        "Hinweis zu **Trades pro Tag**: Die öffentliche Binance-Leaderboard-API "
        "liefert diesen Wert nicht. Die zuverlässigste Aktivitäts-Näherung ist "
        "**Min. offene Positionen** — Trader mit vielen offenen Longs handeln "
        "in der Regel häufiger und liefern schneller Copy-Signale."
    )

    with st.form("scanner_form"):
        c1, c2, c3, c4, c5, c6 = st.columns(6)
        min_day_roi = c1.number_input("Min. Tages-ROI (%)", min_value=-100.0,
                                      max_value=1000.0, step=0.5,
                                      key="scan_min_day_roi")
        min_day_pnl = c2.number_input("Min. Tages-PnL ($)", min_value=-1_000_000.0,
                                      max_value=10_000_000.0, step=10.0,
                                      key="scan_min_day_pnl")
        min_followers = c3.number_input("Min. Follower", min_value=0, step=10,
                                        key="scan_min_followers")
        min_win_rate = c4.number_input("Min. Win-Rate (%)", min_value=0.0,
                                       max_value=100.0, step=5.0,
                                       key="scan_min_win_rate")
        min_open_pos = c5.number_input(
            "Min. offene Positionen", min_value=0, max_value=50, step=1,
            key="scan_min_open_pos",
            help="Live-Aktivitätsfilter: prüft pro Kandidat die aktuell offenen "
                 "Longs (kostet zusätzliche API-Requests). 0 = ignorieren.",
        )
        limit = c6.slider("Max. Ergebnisse", 5, 2000, step=5, key="scan_limit")
        only_shared = st.checkbox(
            "Nur Trader mit öffentlichen Positionen (Pflicht für Live-Signale)",
            key="scan_only_shared",
        )
        submitted = st.form_submit_button("🔎 Trader suchen", type="primary",
                                          width="stretch")

    if submitted:
        bar = st.progress(0.0)
        note = st.empty()

        def _progress(done: int, total: int, wallet: str) -> None:
            bar.progress(done / max(total, 1))
            note.caption(f"Prüfe {done}/{total}: {_short(wallet)} …")

        try:
            results = adapter.list_leaderboard(
                limit=limit, min_day_roi_pct=min_day_roi,
                min_day_pnl_usd=min_day_pnl,
                min_win_rate_pct=min_win_rate or None,
                min_followers=min_followers,
                min_open_positions=int(min_open_pos),
                verified_only=False, progress_cb=_progress,
            )
        except Exception as e:
            bar.empty()
            note.empty()
            st.error(f"Scan fehlgeschlagen: {e}")
            return
        bar.empty()
        note.empty()
        if only_shared:
            results = [r for r in results if r.get("position_shared")]
        st.session_state["scan_results"] = [
            result for result in results if result["verified"]
        ]
        st.session_state["scan_rejected"] = [
            result for result in results if not result["verified"]
        ]
        st.session_state["scan_ts"] = _dt.datetime.now().strftime("%H:%M:%S")

    results = list(st.session_state.get("scan_results", []))
    if not results:
        if submitted:
            rejected = list(st.session_state.get("scan_rejected", []))
            st.warning(
                f"Kein Trader vollständig verifiziert. {len(rejected)} Kandidaten "
                "wurden quantitativ geprüft und abgelehnt."
            )
            if rejected:
                reason_counts: dict[str, int] = {}
                for candidate in rejected:
                    for reason in candidate["verification_reasons"]:
                        category = reason.split(" ", 1)[0]
                        reason_counts[category] = reason_counts.get(category, 0) + 1
                common = sorted(reason_counts.items(), key=lambda item: item[1], reverse=True)
                st.caption(
                    "Häufigste Ausschlussgründe: "
                    + " · ".join(f"{name}: {count}" for name, count in common[:5])
                )
                with st.expander("Warum wurden die besten Kandidaten abgelehnt?"):
                    st.dataframe(
                        pd.DataFrame([{
                            "Trader": _short(candidate["wallet"]),
                            "Score": candidate["quality_score"],
                            "Gründe": " · ".join(candidate["verification_reasons"]),
                        } for candidate in rejected[:10]]),
                        width="stretch", hide_index=True,
                    )
        return

    st.session_state.setdefault("scan_sort", "Letzte erkannte Änderung")
    sort_by = st.selectbox(
        "Sortieren nach",
        ["Letzte erkannte Änderung", "Quality-Score", "Tages-ROI",
         "Tages-PnL", "Follower"],
        key="scan_sort",
        help="'Letzte erkannte Änderung' schiebt Trader, deren Position der "
             "Bot gerade neu gesehen hat, ganz nach oben — ideal für den "
             "End-to-End-Test.",
    )
    if sort_by != "Letzte erkannte Änderung":
        sort_key = {
            "Quality-Score": "quality_score", "Tages-ROI": "day_roi",
            "Tages-PnL": "day_pnl", "Follower": "follower_count",
        }[sort_by]
        results.sort(key=lambda r: r.get(sort_key, 0), reverse=True)

    _render_scan_bulk_bar(adapter, results)

    st.markdown(
        f'<div class="badge-info">{len(results)} Treffer · '
        f'gescannt {st.session_state.get("scan_ts", "")}</div>',
        unsafe_allow_html=True,
    )

    @st.fragment(run_every="5s")
    def _live_scan_rows() -> None:
        bot_alive = _bot_state().get("alive", False)
        chosen = {t["wallet"] for t in trader_store.list_traders() if t["is_copied"]}
        tracking = trader_store.get_tracker_state()
        last_actions = _latest_trader_actions()
        ordered_results = results
        if sort_by == "Letzte erkannte Änderung":
            ordered_results = sorted(
                results,
                key=lambda result: max(
                    float(
                        (tracking.get(result["wallet"]) or {}).get(
                            "last_signal_at", 0
                        ) or 0
                    ),
                    float((last_actions.get(result["wallet"]) or {}).get("ts", 0) or 0),
                ),
                reverse=True,
            )
        for result in ordered_results:
            wallet = result["wallet"]
            is_copied = wallet in chosen
            track = tracking.get(wallet)
            live_positions = (track or {}).get("positions", {}) or {}
            live_sym_count = len(live_positions)
            scan_pos_count = int(result.get("open_positions_count", 0))
            scan_symbols = list(result.get("open_symbols", []) or [])
            pos_count = live_sym_count if is_copied and live_sym_count else scan_pos_count
            preview_symbols = sorted(live_positions) if live_sym_count else scan_symbols
            live_syms_preview = ", ".join(preview_symbols[:3])
            if pos_count > 3:
                live_syms_preview += f" +{pos_count - 3}"
            last_poll = float((track or {}).get("last_poll_at", 0) or 0)
            poll_age = time.time() - last_poll if last_poll else None

            if is_copied:
                live_badge, live_sub = _tracking_badge(track, bot_alive)
            else:
                live_badge = '<span class="badge-info">◌ NICHT KOPIERT</span>'
                live_sub = "kein Tracking"

            roi = result["day_roi"]
            roi_cls = "profit" if roi >= 0 else "loss"
            pnl = result["day_pnl"]
            pnl_cls = "profit" if pnl >= 0 else "loss"
            wr = float(result.get("win_rate") or 0)
            name = result["nick_name"] or _short(wallet)
            profile_url = (
                f"https://www.binance.com/en/copy-trading/lead-details/"
                f"{wallet}?timeRange=30D"
            )
            status_pill = (
                '<span class="badge-profit">KOPIERT</span>' if is_copied
                else '<span class="badge-info">BEREIT</span>'
            )
            pos_html = (
                f'{pos_count} · <span style="color:#475569">{live_syms_preview}</span>'
                if pos_count else '<span style="color:#94a3b8">keine offen</span>'
            )
            poll_html = _fmt_age(poll_age) if poll_age is not None else "—"
            action = last_actions.get(wallet)
            if action:
                act_ts = float(action.get("ts", 0) or 0)
                act_age = _fmt_age(time.time() - act_ts) if act_ts else "—"
                act_sym = action.get("symbol") or ""
                act_msg = action.get("message") or action.get("kind") or ""
                act_kind = action.get("kind") or ""
                act_icon = _EVENT_ICON.get(act_kind, "•")
                act_html = (
                    f'<b>{act_icon} {act_sym}</b>'
                    f'<span style="color:#475569;margin-left:4px">{act_msg[:38]}</span>'
                    f' · {act_age}'
                )
            else:
                act_html = '<span style="color:#94a3b8">noch keine erkannt</span>'

            st.markdown(
                f'''
                <div class="scan-row">
                  <div class="scan-row-main">
                    <div class="scan-row-name">
                      <span class="scan-name">{name}</span>
                      <span class="scan-short">{_short(wallet)}</span>
                      {status_pill}
                    </div>
                    <div class="scan-row-metrics">
                      <span class="scan-metric"><em>ROI 1d</em><b class="{roi_cls}">{roi:+.1f}%</b></span>
                      <span class="scan-metric"><em>PnL 1d</em><b class="{pnl_cls}">${pnl:+,.0f}</b></span>
                      <span class="scan-metric"><em>WR</em><b>{wr:.0f}%</b></span>
                      <span class="scan-metric"><em>Follower</em><b>{result["follower_count"]}</b></span>
                      <span class="scan-metric"><em>Positionen</em><b>{pos_html}</b></span>
                      <span class="scan-metric"><em>Letzte Aktion</em><b>{act_html}</b></span>
                      <span class="scan-metric"><em>Poll</em><b>{poll_html}</b></span>
                      <span class="scan-live">{live_badge}<em>{live_sub}</em></span>
                    </div>
                  </div>
                </div>
                ''',
                unsafe_allow_html=True,
            )

            with st.expander("Details, Verifikation und Copy-Steuerung", expanded=False):
                d1, d2, d3, d4 = st.columns(4)
                d1.metric("7T ROI", f"{result['week_roi'] or 0:+.1f}%")
                d2.metric("30T ROI", f"{result['month_roi'] or 0:+.1f}%")
                d3.metric("Quality-Score", f"{result.get('quality_score', 0):.1f}")
                d4.metric(
                    "Positionen öffentlich",
                    "ja" if result["position_shared"] else "nein",
                )

                if live_sym_count:
                    live_rows = []
                    for sym, pos in sorted(live_positions.items()):
                        amt = pos.get("amount") or pos.get("qty") or 0
                        entry = pos.get("entry_price") or pos.get("entry") or 0
                        live_rows.append(
                            f"<div class='scan-live-row'><b>{sym}</b>"
                            f"<span>Menge {amt}</span><span>Entry {entry}</span></div>"
                        )
                    st.markdown(
                        "<div class='scan-live-list'>"
                        + "".join(live_rows) + "</div>",
                        unsafe_allow_html=True,
                    )
                else:
                    st.caption("Aktuell keine offenen Positionen vom Backend gemeldet.")

                links_html = " · ".join(
                    f"<a href='{url}' target='_blank'>{label}</a>"
                    for label, url in [
                        ("Binance Profil", profile_url),
                        *_verification_links(wallet),
                    ]
                )
                st.markdown(
                    f"<div class='scan-links'>🔗 {links_html}</div>",
                    unsafe_allow_html=True,
                )

                c1, c2 = st.columns([1, 1])
                amount = c1.number_input(
                    "USDT / Trade",
                    min_value=1.0,
                    value=float(_default_size()),
                    step=5.0,
                    key=f"scan_amt_{wallet}",
                )
                action = "Decopy" if is_copied else "Copy übernehmen"
                icon = "⛔" if is_copied else "✅"
                if c2.button(
                    f"{icon} {action}",
                    key=f"scan_copy_{wallet}",
                    width="stretch",
                ):
                    trader_store.save_verification(wallet, result)
                    adapter.set_copy_size(wallet, amount)
                    if is_copied:
                        adapter.deactivate_wallet(wallet)
                    else:
                        adapter.activate_wallet(wallet)
                        st.toast(
                            f"Copy aktiv für {_short(wallet)} mit ${amount:,.0f}/Trade. "
                            f"Bot repliziert offene Longs beim nächsten Poll (~5 s).",
                            icon="✅",
                        )
                    st.rerun()

    _live_scan_rows()
    st.caption(
        "Übernehmen setzt Betrag, aktiviert Copy-Trading und pinnt den Trader an. "
        "Der Bot fährt beim nächsten Poll (~5 s) alle aktuell offenen Long-Positionen "
        "des Traders mit deinem Betrag als Paper-Order nach. Danach folgt er live "
        "jedem `OPEN` / `INCREASE ±5 %` / `CLOSE`."
    )


def _latest_trader_actions() -> dict[str, dict]:
    """Pro Wallet das jüngste Pipeline-Event, das eine Trader-Aktion darstellt.

    Genutzt für die Scanner-Spalte 'Letzte Aktion' und zum Sortieren nach
    tatsächlich beobachtetem Trade-Zeitpunkt (nicht nur `last_signal_at`).
    """
    events = trader_store.recent_events(limit=150)
    relevant = {"SIGNAL", "BUY", "SELL", "SIMULATION"}
    latest: dict[str, dict] = {}
    for event in events:
        wallet = event.get("wallet") or ""
        if not wallet or event.get("kind") not in relevant:
            continue
        if wallet in latest:
            continue
        latest[wallet] = event
    return latest


def _render_scan_bulk_bar(adapter, results: list[dict]) -> None:
    """Ein-Klick-Abo für alle gefundenen Trader zum End-to-End-Test."""
    if not results:
        return
    chosen = {t["wallet"] for t in trader_store.list_traders() if t["is_copied"]}
    not_yet = [r for r in results if r["wallet"] not in chosen]
    already = len(results) - len(not_yet)

    col_amount, col_button, col_info = st.columns([1, 2, 3])
    bulk_amount = col_amount.number_input(
        "USDT / Trade", min_value=1.0, value=float(_default_size()),
        step=5.0, key="scan_bulk_amount",
        help="Wird für alle Trader gesetzt, die noch nicht kopiert werden.",
    )
    label = (
        f"🚀 Alle {len(not_yet)} verifizierten Trader abonnieren"
        if not_yet else "✅ Alle verifizierten Trader sind bereits abonniert"
    )
    if col_button.button(
        label, key="scan_bulk_subscribe", type="primary",
        width="stretch", disabled=not not_yet,
    ):
        for result in not_yet:
            wallet = result["wallet"]
            trader_store.save_verification(wallet, result)
            adapter.set_copy_size(wallet, float(bulk_amount))
            adapter.activate_wallet(wallet)
        st.toast(
            f"{len(not_yet)} Trader abonniert · ${bulk_amount:,.0f}/Trade. "
            "Der Bot repliziert die Longs beim nächsten Poll (~5 s).",
            icon="🚀",
        )
        st.rerun()
    col_info.caption(
        f"{len(results)} verifiziert · {already} bereits kopiert · "
        f"{len(not_yet)} bereit. Nach dem Klick sortiert der Scanner nach "
        "'Letzte erkannte Änderung' — Trader mit frischem Trade stehen oben."
    )


# ── 📜 Historie ──────────────────────────────────────────────────────────────

def _render_history() -> None:
    st.markdown('<div class="section-header">📜 Meine Trade-Historie</div>',
                unsafe_allow_html=True)
    history = _load_json(HISTORY_PATH, [])
    history = history if isinstance(history, list) else []
    history.sort(key=lambda row: row.get("closed_at", 0), reverse=True)

    realized = sum(float(row.get("pnl_usdt", 0)) for row in history)
    wins = sum(1 for row in history if float(row.get("pnl_usdt", 0)) > 0)
    c1, c2, c3 = st.columns(3)
    c1.metric("Geschlossene Trades", len(history))
    c2.metric("Realisierter PnL", f"${realized:+,.2f}")
    c3.metric("Win-Rate", f"{wins / len(history):.0%}" if history else "—")

    if not history:
        st.info("Noch keine abgeschlossenen Paper- oder Live-Trades.")
        return

    st.dataframe(
        pd.DataFrame([{
            "Zeit": _dt.datetime.fromtimestamp(row.get("closed_at", 0)).strftime(
                "%d.%m.%Y %H:%M:%S"
            ) if row.get("closed_at") else "—",
            "Trader": _short(row.get("trader", "")) if row.get("trader") else "—",
            "Symbol": row.get("symbol", ""),
            "Einsatz": float(row.get("size_usdt", 0)),
            "Entry": float(row.get("entry_price", 0)),
            "Exit": float(row.get("exit_price", 0)),
            "PnL $": float(row.get("pnl_usdt", 0)),
            "PnL %": float(row.get("pnl_pct", 0)),
            "Haltezeit": _fmt_hold(max(0.0, row.get("closed_at", 0) - row.get("opened_at", 0))),
            "Grund": row.get("reason", ""),
            "Modus": "Paper" if row.get("dry_run") else "Live",
        } for row in history]),
        width="stretch", hide_index=True,
        column_config={
            "Einsatz": st.column_config.NumberColumn(format="$%.2f"),
            "Entry": st.column_config.NumberColumn(format="$%.4f"),
            "Exit": st.column_config.NumberColumn(format="$%.4f"),
            "PnL $": st.column_config.NumberColumn(format="$%+.2f"),
            "PnL %": st.column_config.NumberColumn(format="%+.2f%%"),
        },
    )



# ── ⚙️ Setup ─────────────────────────────────────────────────────────────────

def _render_setup(adapter):
    settings = adapter.get_settings()
    bot = _bot_state()

    st.markdown('<div class="section-header">🤖 Bot-Prozess</div>',
                unsafe_allow_html=True)
    if bot["alive"]:
        started = float(bot.get("started_at", 0) or 0)
        st.success(
            f"Läuft (PID {bot.get('pid')}) · gestartet "
            f"{_fmt_age(time.time() - started) if started else '—'} · "
            f"REST-Polling {'aktiv' if bot.get('ws_connected') else 'getrennt'} · "
            f"{bot.get('copied', 0)} Trader kopiert · "
            f"{bot.get('open_trades', 0)} eigene Position(en) · "
            f"API: {bot.get('api_health', '?')}"
        )
    else:
        st.error(
            "Bot-Prozess läuft nicht. Ohne ihn wird nichts getrackt und nichts "
            "gehandelt — starte ihn mit `python main.py`."
        )

    st.markdown('<div class="section-header">⚙️ Konfiguration (aus der .env)</div>',
                unsafe_allow_html=True)
    st.caption(
        "Diese Werte liest der Bot-Prozess beim Start aus der `.env`. Eine "
        "Änderung im Browser würde nur den Dashboard-Prozess betreffen und wäre "
        "irreführend — deshalb sind sie bewusst nur lesbar."
    )
    st.markdown(f"""
    <div class="detail-grid">
      <div class="detail-item"><div class="detail-label">Modus</div><div class="detail-value">{"DRY-RUN" if _dry_run() else "LIVE"}</div></div>
      <div class="detail-item"><div class="detail-label">Standard-Betrag</div><div class="detail-value">${_default_size():,.0f}</div></div>
      <div class="detail-item"><div class="detail-label">Polling-Intervall</div><div class="detail-value">{settings['poll_interval']:.0f}s</div></div>
      <div class="detail-item"><div class="detail-label">Min. Signalgröße</div><div class="detail-value">${settings['min_copy_size_usd']:,.0f}</div></div>
      <div class="detail-item"><div class="detail-label">Auto-Discovery</div><div class="detail-value">{"AN" if settings['auto_discover'] else "AUS"}</div></div>
      <div class="detail-item"><div class="detail-label">Max. Trader</div><div class="detail-value">{settings['max_tracked_traders']}</div></div>
      <div class="detail-item"><div class="detail-label">Rescan-Intervall</div><div class="detail-value">{settings['rescan_hours']:.1f}h</div></div>
      <div class="detail-item"><div class="detail-label">Min. Tages-ROI</div><div class="detail-value">{settings['min_day_roi_pct']:.1f}%</div></div>
      <div class="detail-item"><div class="detail-label">Min. Tages-PnL</div><div class="detail-value">${settings['min_day_pnl_usd']:,.0f}</div></div>
      <div class="detail-item"><div class="detail-label">Min. Follower</div><div class="detail-value">{settings['min_followers']}</div></div>
      <div class="detail-item"><div class="detail-label">Signal-TTL</div><div class="detail-value">{settings['signal_ttl']:.0f}s</div></div>
    </div>
    """, unsafe_allow_html=True)

    st.markdown('<div class="section-header">🧾 Pipeline-Protokoll</div>',
                unsafe_allow_html=True)
    st.caption(
        "Jede Entscheidung der Pipeline: Signal empfangen, übersprungen (mit "
        "Grund), gekauft, verkauft. Erste Anlaufstelle beim Debuggen."
    )
    f1, f2 = st.columns(2)
    kind_filter = f1.selectbox("Kategorie", ["alle"] + sorted(_EVENT_ICON),
                               key="event_kind")
    max_rows = f2.slider("Max. Zeilen", 20, 300, 100, key="event_rows")

    @st.fragment(run_every="3s")
    def _events_live():
        events = trader_store.recent_events(limit=max_rows)
        if kind_filter != "alle":
            events = [e for e in events if e["kind"] == kind_filter]
        if not events:
            st.caption("Noch keine Ereignisse protokolliert.")
            return
        st.dataframe(
            pd.DataFrame([{
                "Zeit":    _dt.datetime.fromtimestamp(e["ts"]).strftime("%d.%m %H:%M:%S"),
                "Typ":     e["kind"],
                "Level":   e["level"],
                "Trader":  _short(e["wallet"]) if e["wallet"] else "",
                "Symbol":  e["symbol"],
                "Meldung": e["message"],
            } for e in events]),
            width="stretch", hide_index=True,
        )

    _events_live()
