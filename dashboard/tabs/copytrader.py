"""
dashboard/tabs/copytrader.py — Hyperliquid Copy-Trader Dashboard

ARCHITEKTUR (wichtig zum Debuggen)
----------------------------------
Dashboard und Bot sind **zwei getrennte Prozesse**. Dieses Modul erzeugt
deshalb bewusst KEINEN laufenden Tracker: der Adapter hier dient nur zum
Schreiben der Trader-Auswahl und für den Leaderboard-Scanner. Alles, was
"live" aussieht, kommt aus der geteilten SQLite-DB (`src/utils/trader_store.py`),
die der Bot-Prozess befüllt:

    bot_heartbeat     → läuft der Bot? steht der WebSocket? DRY_RUN?
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

_WINDOW_LABEL = {"day": "1 Tag", "week": "1 Woche", "month": "1 Monat", "allTime": "Gesamt"}

_EVENT_ICON = {
    "SIGNAL": "📡", "BUY": "🟢", "SELL": "🔴", "SKIP": "⏭️", "ERROR": "❌",
    "SIMULATION": "🧪",
    "TRADER_ADDED": "➕", "TRADER_REMOVED": "➖", "TRADER_UPDATED": "✏️",
}


# ── Daten-Helpers ────────────────────────────────────────────────────────────

def _load_json(path: str, fallback):
    try:
        with open(path, encoding="utf-8") as file:
            return json.load(file)
    except (OSError, json.JSONDecodeError):
        return fallback


def _copy_positions() -> dict:
    """Eigene offene Binance-Positionen aus dem Copy-Trading (ohne Altlasten)."""
    raw = _load_json("positions.json", {})
    return {s: p for s, p in raw.items() if p.get("source") == "COPY"}


def _positions_of(wallet: str, short: str) -> dict:
    """Eigene offene Positionen, die genau diesem HL-Trader zuzuordnen sind."""
    return {
        sym: p for sym, p in _copy_positions().items()
        if p.get("hl_trader_full") == wallet
        or (not p.get("hl_trader_full") and p.get("hl_trader") == short)
    }


def _history_of(wallet: str, short: str) -> list[dict]:
    rows = _load_json(HISTORY_PATH, [])
    if not isinstance(rows, list):
        return []
    out = [
        h for h in rows
        if h.get("trader") == wallet
        or (not h.get("trader") and h.get("trader_short") == short)
    ]
    out.sort(key=lambda h: h.get("closed_at", 0), reverse=True)
    return out


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
    """(Guthaben, Quelle). Echte Abfrage nur mit Keys und DRY_RUN=False."""
    if _dry_run():
        return 0.0, "paper"
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
    return [
        ("Hyperliquid Explorer", f"https://app.hyperliquid.xyz/explorer/address/{wallet}"),
        ("Hyperdash",            f"https://hyperdash.info/trader/{wallet}"),
        ("Purrsec",              f"https://purrsec.com/address/{wallet}"),
        ("HypurrScan",           f"https://hypurrscan.io/address/{wallet}"),
        ("ASXN",                 f"https://asxn.xyz/user/{wallet}"),
    ]


def _tracking_badge(track: dict | None, bot_alive: bool) -> tuple[str, str]:
    """(Badge-HTML, Kurztext) für den Tracking-Zustand eines Traders."""
    if not bot_alive:
        return '<span class="badge-loss">⛔ BOT AUS</span>', "main.py läuft nicht"
    if not track:
        return ('<span class="badge-warn">⏳ STARTET</span>',
                "Bot übernimmt gleich")
    if not track.get("ws_subscribed"):
        return '<span class="badge-warn">⚠️ KEIN WS</span>', "WebSocket fehlt"
    last_poll = float(track.get("last_poll_at", 0) or 0)
    if last_poll and (time.time() - last_poll) > 120:
        return ('<span class="badge-warn">⚠️ POLL ALT</span>',
                _fmt_age(time.time() - last_poll))
    return ('<span class="badge-profit">📡 GETRACKT</span>',
            f"{int(track.get('fill_count', 0))} Fills")


# ── Adapter (nur Steuerung + Scanner, kein Tracking) ─────────────────────────

@st.cache_resource
def _get_adapter():
    """Adapter-Instanz OHNE `start()`.

    Das Tracking gehört dem Bot-Prozess. Würde das Dashboard einen zweiten
    Tracker starten, hätte man doppelte API-Last und zwei Wahrheiten.
    """
    from src.adapters.hyperliquid_copytrader import HyperliquidCopyTrader
    return HyperliquidCopyTrader(publish_state=False)


# ── Entry ────────────────────────────────────────────────────────────────────

def render():
    adapter = _get_adapter()
    adapter.refresh()

    st.markdown('<h2 style="margin-bottom:0;">🔄 Hyperliquid Copy-Trader</h2>',
                unsafe_allow_html=True)
    mode = "📝 PAPER (DRY-RUN)" if _dry_run() else "🔴 LIVE"
    st.caption(f"Signale von Hyperliquid-Tradern · Ausführung auf Binance Spot · Modus: {mode}")

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

    tab_home, tab_traders, tab_scan, tab_setup = st.tabs(
        ["🏠 Übersicht", "👥 Meine Trader", "🔍 Scanner", "⚙️ Setup"]
    )
    with tab_home:
        _render_overview()
    with tab_traders:
        _render_my_traders(adapter)
    with tab_scan:
        _render_scanner(adapter)
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
            _banner("⚠️ Hyperliquid rate-limited",
                    "Der Bot wartet auf das API-Reset. Tracking läuft weiter, "
                    "Kennzahlen kommen kurzzeitig aus dem Cache.", "amber")
        elif bot["alive"] and health == "unreachable":
            _banner("🔴 Hyperliquid unerreichbar",
                    "Netzwerkfehler zu api.hyperliquid.xyz — der Bot versucht "
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
            bal_value, bal_sub, bal_color = "PAPER", "DRY_RUN=True — kein echtes Geld", "#ffb400"
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

        ws_ok = sum(1 for t in copied
                    if tracking.get(t["wallet"], {}).get("ws_subscribed"))
        if not bot["alive"]:
            ws_val, ws_sub, ws_col = "—", "Bot offline", "#64748b"
        elif not copied:
            ws_val, ws_sub, ws_col = "—", "kein Trader ausgewählt", "#64748b"
        elif ws_ok == len(copied):
            ws_val, ws_sub, ws_col = (f"{ws_ok}/{len(copied)}",
                                      "alle Trader live abonniert", "#00e6a7")
        else:
            ws_val, ws_sub, ws_col = (f"{ws_ok}/{len(copied)}",
                                      "Subscription unvollständig", "#ffb400")

        last_fill = max(
            (float(tracking.get(t["wallet"], {}).get("last_fill_at", 0) or 0)
             for t in copied),
            default=0.0,
        )
        d1, d2, d3 = st.columns(3)
        d1.markdown(_kpi("🤖 Bot-Prozess", bot_val, bot_sub, bot_col),
                    unsafe_allow_html=True)
        d2.markdown(_kpi("📡 WebSocket-Tracking", ws_val, ws_sub, ws_col),
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
                        "Trader":  p.get("hl_trader", "?"),
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
    bot = _bot_state()
    traders = trader_store.list_traders()
    tracking = trader_store.get_tracker_state()

    st.markdown('<div class="section-header">👥 Meine Trader</div>',
                unsafe_allow_html=True)
    st.caption(
        "Hier stehen ausschließlich Trader, die du selbst übernommen hast. "
        "**Kopiert** = der Bot handelt ihn auf Binance. **Angepinnt** = wird "
        "überwacht, aber nicht automatisch gehandelt."
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

    if not traders:
        st.info(
            "Noch kein Trader übernommen. Gehe zum **🔍 Scanner**, finde einen "
            "passenden Trader und übernimm ihn mit deinem Wunschbetrag."
        )
        return

    _render_bulk_actions(adapter, traders)

    for trader in traders:
        wallet = trader["wallet"]
        short = _short(wallet)
        track = tracking.get(wallet)
        my_open = _positions_of(wallet, short)
        history = _history_of(wallet, short)
        realized = sum(float(h.get("pnl_usdt", 0)) for h in history)
        exposure = sum(float(p.get("size_usdt", 0)) for p in my_open.values())
        hl_positions = (track or {}).get("positions", {})
        trader_upnl = sum(float(p.get("unrealized_pnl", 0))
                          for p in hl_positions.values())

        with st.container(border=True):
            _trader_header(trader, short, track, bot["alive"], realized,
                           len(history), len(my_open), exposure, trader_upnl)
            _trader_controls(adapter, trader, short)

            with st.expander("▾ Details ansehen", expanded=False):
                t_mine, t_track, t_detail, t_verify = st.tabs(
                    ["💼 Meine Trades", "📡 Tracking", "🎯 Trader-Details", "🔗 Verifikation"]
                )
                with t_mine:
                    _tab_my_trades(my_open, history, realized)
                with t_track:
                    _tab_tracking(wallet, track, bot["alive"])
                with t_detail:
                    _tab_trader_detail(adapter, wallet, hl_positions)
                with t_verify:
                    _tab_verify(wallet)


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


def _trader_header(trader: dict, short: str, track: dict | None, bot_alive: bool,
                   realized: float, closed: int, open_count: int,
                   exposure: float, trader_upnl: float) -> None:
    """Kopfzeile eines Traders — alles Wichtige ohne Aufklappen sichtbar."""
    pin = '<span class="badge-info">📌 angepinnt</span> ' if trader["is_focus"] else ""
    state = ('<span class="badge-profit">🟢 KOPIERT</span>' if trader["is_copied"]
             else '<span class="badge-info">👁 NUR BEOBACHTET</span>')
    badge, badge_sub = _tracking_badge(track, bot_alive)

    size = float(trader["size_usdt"] or 0) or _default_size()
    amount_sub = "eigener Betrag" if trader["size_usdt"] else "Standard aus .env"
    pnl_color = "#00e6a7" if realized >= 0 else "#ff5c5c"
    account = float((track or {}).get("account_usd") or trader["account_usd"] or 0)

    st.markdown(
        '<div style="display:flex;align-items:center;gap:10px;flex-wrap:wrap;'
        'margin-bottom:6px">'
        f'<span style="font-family:JetBrains Mono,monospace;font-size:1.05rem;'
        f'font-weight:600">{short}</span>{pin}{state}{badge}</div>',
        unsafe_allow_html=True,
    )
    h1, h2, h3, h4, h5 = st.columns(5)
    h1.markdown(_kpi("💵 Betrag", f"${size:,.0f}", amount_sub), unsafe_allow_html=True)
    h2.markdown(_kpi("💰 Mein PnL", f"${realized:+,.2f}", f"{closed} geschlossen",
                     pnl_color), unsafe_allow_html=True)
    h3.markdown(_kpi("📦 Offen", str(open_count), f"${exposure:,.0f} eingesetzt"),
                unsafe_allow_html=True)
    h4.markdown(_kpi("🎯 Trader uPnL", f"${trader_upnl:+,.0f}", "auf Hyperliquid",
                     "#00e6a7" if trader_upnl >= 0 else "#ff5c5c"),
                unsafe_allow_html=True)
    h5.markdown(_kpi("📡 Tracking", badge_sub,
                     f"Account ${account:,.0f}" if account else "Account —"),
                unsafe_allow_html=True)


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
        f"{mode} · Konsolen-Präfix `[HL-{'COPY' if trader['is_copied'] else 'WATCH'}]` "
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
                "HL-Coin":  p.get("hl_coin", ""),
                "HL-Hebel": float(p.get("hl_leverage", 0)),
                "Modus":    "Paper" if p.get("dry_run") else "Live",
            } for sym, p in my_open.items()]),
            width="stretch", hide_index=True,
            column_config={
                "Einsatz":  st.column_config.NumberColumn(format="$%.0f"),
                "Entry":    st.column_config.NumberColumn(format="$%.4f"),
                "Menge":    st.column_config.NumberColumn(format="%.6f"),
                "HL-Hebel": st.column_config.NumberColumn(format="%.0fx"),
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
    ws_ok = bool(track.get("ws_subscribed"))
    c1, c2, c3, c4 = st.columns(4)
    c1.markdown(_kpi("📡 WebSocket", "abonniert" if ws_ok else "nicht abonniert",
                     _fmt_age(now - float(track.get("ws_sub_at") or now)) if ws_ok else "—",
                     "#00e6a7" if ws_ok else "#ff5c5c"), unsafe_allow_html=True)
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

    if not st.button("📊 Historie & Kennzahlen laden", key=f"deep_{wallet}"):
        st.caption("Lädt Fills und Win-Rate je Zeitfenster — ein API-Call.")
        return

    with st.spinner("Lade Hyperliquid-Daten …"):
        try:
            focus = adapter.get_trader_focus(wallet)
        except Exception as e:
            st.error(f"Daten konnten nicht geladen werden: {e}")
            return
    if focus is None:
        st.error("Ungültige Wallet-Adresse.")
        return

    cols = st.columns(4)
    for col, name in zip(cols, ("day", "week", "month", "allTime")):
        metrics = focus["metrics_by_window"].get(name)
        with col:
            if metrics is None:
                st.markdown(_kpi(f"📅 {_WINDOW_LABEL[name]}", "—",
                                 "keine geschlossenen Trades", "#64748b"),
                            unsafe_allow_html=True)
                continue
            color = ("#00e6a7" if metrics.win_rate >= 0.55
                     else "#ffb400" if metrics.win_rate >= 0.45 else "#ff5c5c")
            st.markdown(_kpi(f"📅 {_WINDOW_LABEL[name]}", f"{metrics.win_rate:.0%}",
                             f"{metrics.trades} Trades · Ø {_fmt_hold(metrics.avg_hold_sec)}",
                             color), unsafe_allow_html=True)

    st.markdown("<br>**Letzte Fills des Traders**", unsafe_allow_html=True)
    fills = focus.get("recent_fills", [])
    if not fills:
        st.caption("Keine Fills verfügbar.")
        return
    now = time.time()
    st.dataframe(
        pd.DataFrame([{
            "Zeitpunkt":  _dt.datetime.fromtimestamp(
                float(f.get("time", 0)) / 1000.0
            ).strftime("%d.%m.%Y %H:%M:%S"),
            "Alter":      _fmt_age(now - float(f.get("time", 0)) / 1000.0),
            "Coin":       f.get("coin", ""),
            "Seite":      "BUY" if f.get("side") == "B" else "SELL",
            "Aktion":     f.get("dir", ""),
            "Preis":      float(f.get("px", "0") or "0"),
            "Menge":      float(f.get("sz", "0") or "0"),
            "Wert USD":   float(f.get("px", "0") or "0")
                          * float(f.get("sz", "0") or "0"),
            "Closed PnL": float(f.get("closedPnl", "0") or "0"),
        } for f in fills]),
        width="stretch", hide_index=True,
        column_config={
            "Preis":      st.column_config.NumberColumn(format="$%.4f"),
            "Menge":      st.column_config.NumberColumn(format="%.4f"),
            "Wert USD":   st.column_config.NumberColumn(format="$%.2f"),
            "Closed PnL": st.column_config.NumberColumn(format="$%+.2f"),
        },
    )


def _tab_verify(wallet: str) -> None:
    st.caption(
        "Hyperliquid zeigt selbst wenig Details. Diese Explorer liefern PnL-Kurven "
        "und Charts pro Coin. Ändert ein Anbieter seinen URL-Pfad, kommt ein 404 — "
        "dann die Adresse unten manuell dort suchen."
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
    from src.adapters.hyperliquid_copytrader import LEADERBOARD_WINDOWS, hl_explorer_url

    st.markdown('<div class="section-header">🔍 Passenden Trader finden</div>',
                unsafe_allow_html=True)
    st.caption(
        "Alle Kennzahlen beziehen sich auf **das gewählte Zeitfenster**, nicht auf "
        "Lifetime. Der Scan ist wegen des HL-Rate-Limits auf ~2s pro Kandidat gedrosselt."
    )

    with st.form("scanner_form"):
        c1, c2, c3, c4 = st.columns(4)
        window = c1.selectbox("Zeitfenster", LEADERBOARD_WINDOWS, index=0,
                              format_func=lambda w: _WINDOW_LABEL.get(w, w))
        min_win_rate = c2.slider("Min. Win-Rate %", 0, 100, 55) / 100
        min_trades = c3.number_input("Min. Trades", min_value=0, value=20, step=5)
        max_hold_min = c4.number_input("Max. Ø Hold (min)", min_value=0, value=60, step=5)

        c5, c6 = st.columns(2)
        min_account = c5.number_input("Min. Account-Wert ($)", min_value=0,
                                      value=10000, step=5000)
        limit = c6.slider("Kandidaten prüfen", 5, 60, 20)
        submitted = st.form_submit_button("🔎 Suchen", type="primary", width="stretch")

    if submitted:
        bar = st.progress(0.0)
        note = st.empty()

        def _progress(done: int, total: int, wallet: str) -> None:
            bar.progress(done / total)
            note.caption(f"Prüfe {done}/{total}: {_short(wallet)} …")

        try:
            results = adapter.list_leaderboard(
                window=window, limit=limit,
                min_trades=min_trades, min_win_rate=min_win_rate,
                max_avg_hold_sec=max_hold_min * 60 if max_hold_min else None,
                min_account_value=min_account,
                progress_cb=_progress,
            )
        except Exception as e:
            bar.empty()
            note.empty()
            st.error(f"Scan fehlgeschlagen: {e}")
            return
        bar.empty()
        note.empty()
        st.session_state["scan_results"] = results
        st.session_state["scan_window"] = window
        st.session_state["scan_ts"] = _dt.datetime.now().strftime("%H:%M:%S")

    results = list(st.session_state.get("scan_results", []))
    if not results:
        if submitted:
            st.info("Kein Trader erfüllt diese Filter. Senke die Schwellwerte oder "
                    "wähle ein breiteres Zeitfenster.")
        return

    sort_by = st.selectbox(
        "Sortieren nach",
        ["PnL (Fenster)", "Win-Rate", "Volumen (Liquidität)", "Trades", "Account-Wert"],
        key="scan_sort",
    )
    sort_key = {
        "PnL (Fenster)": "window_pnl", "Win-Rate": "win_rate",
        "Volumen (Liquidität)": "window_volume", "Trades": "trades",
        "Account-Wert": "account_value",
    }[sort_by]
    results.sort(key=lambda r: r.get(sort_key, 0), reverse=True)

    win_label = _WINDOW_LABEL.get(st.session_state.get("scan_window", "day"), "?")
    st.markdown(
        f'<div class="badge-info">{len(results)} Treffer · Fenster: {win_label} · '
        f'gescannt {st.session_state.get("scan_ts", "")}</div>',
        unsafe_allow_html=True,
    )

    chosen = {t["wallet"] for t in trader_store.list_traders() if t["is_copied"]}
    st.dataframe(
        pd.DataFrame([{
            "Status":        "🟢 kopiert" if r["wallet"] in chosen else "—",
            "Trader":        _short(r["wallet"]),
            "Account":       r["account_value"],
            "PnL (Fenster)": r["window_pnl"],
            "Volumen":       r["window_volume"],
            "Trades":        r["trades"],
            "Win-Rate":      r["win_rate"],
            "Ø Hold":        r["avg_hold_sec"] / 60,
            "Aktiv vor":     r["last_active_age"] / 3600,
            "Explorer":      hl_explorer_url(r["wallet"]),
        } for r in results]),
        width="stretch", hide_index=True,
        column_config={
            "Account":       st.column_config.NumberColumn(format="$%.0f"),
            "PnL (Fenster)": st.column_config.NumberColumn(format="$%+.0f"),
            "Volumen":       st.column_config.NumberColumn(format="$%.0f"),
            "Trades":        st.column_config.NumberColumn(format="%d"),
            "Win-Rate":      st.column_config.ProgressColumn(
                format="%.0f%%", min_value=0.0, max_value=1.0),
            "Ø Hold":        st.column_config.NumberColumn(format="%.1f min"),
            "Aktiv vor":     st.column_config.NumberColumn(format="%.1f h"),
            "Explorer":      st.column_config.LinkColumn("🔗 Prüfen", display_text="Öffnen"),
        },
    )

    st.markdown('<div class="section-header">➕ Trader übernehmen</div>',
                unsafe_allow_html=True)
    with st.form("adopt_form"):
        a1, a2, a3 = st.columns([3, 1, 1])
        choice = a1.selectbox("Trader", options=[r["wallet"] for r in results],
                              format_func=_short, label_visibility="collapsed")
        amount = a2.number_input("Betrag $", min_value=1.0, value=_default_size(),
                                 step=5.0, label_visibility="collapsed")
        adopt = a3.form_submit_button("🚀 Kopieren", type="primary", width="stretch")
    if adopt:
        adapter.set_copy_size(choice, amount)
        adapter.activate_wallet(choice)
        adapter.set_focus_wallet(choice)
        st.success(
            f"{_short(choice)} wird jetzt mit ${amount:,.0f} pro Trade kopiert. "
            f"Der Bot übernimmt ihn innerhalb weniger Sekunden — den Nachweis "
            f"siehst du unter **👥 Meine Trader → 📡 Tracking**."
        )
    st.caption(
        "Übernehmen setzt Betrag, aktiviert Copy-Trading und pinnt den Trader an. "
        "Alles davon lässt sich pro Trader nachträglich ändern."
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
            f"WebSocket {'verbunden' if bot.get('ws_connected') else 'getrennt'} · "
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
      <div class="detail-item"><div class="detail-label">Min. Signalgröße (HL)</div><div class="detail-value">${settings['min_copy_size_usd']:,.0f}</div></div>
      <div class="detail-item"><div class="detail-label">Auto-Discovery</div><div class="detail-value">{"AN" if settings['auto_discover'] else "AUS"}</div></div>
      <div class="detail-item"><div class="detail-label">Max. Trader</div><div class="detail-value">{settings['max_tracked_traders']}</div></div>
      <div class="detail-item"><div class="detail-label">Rescan-Intervall</div><div class="detail-value">{settings['rescan_hours']:.1f}h</div></div>
      <div class="detail-item"><div class="detail-label">Min. Trades</div><div class="detail-value">{settings['min_trades']}</div></div>
      <div class="detail-item"><div class="detail-label">Min. Win-Rate</div><div class="detail-value">{settings['min_win_rate']:.0%}</div></div>
      <div class="detail-item"><div class="detail-label">Min. Account-Wert</div><div class="detail-value">${settings['min_account_value']:,.0f}</div></div>
      <div class="detail-item"><div class="detail-label">Max. Ø Haltezeit</div><div class="detail-value">{settings['max_avg_hold_min']:.0f} min</div></div>
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
