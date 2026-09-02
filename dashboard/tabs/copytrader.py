"""
dashboard/tabs/copytrader.py — Hyperliquid Copy-Trader Dashboard

Layout (3 Tabs, keine Redundanz):
  🏠 Übersicht    — Kontostand, Modus, wie viele Trader kopiert werden, Live-Feed
  👥 Meine Trader — eine aufklappbare Zeile pro Trader; darin Sub-Tabs:
                    Meine Trades · Trader-Details · Betrag & Copy · Verifikation
  🔍 Scanner      — Trader finden, Betrag wählen, direkt übernehmen
  ⚙️ Setup        — Laufzeit-Einstellungen, .env-Anzeige

Der Focus-Trader ist kein eigener Tab mehr, sondern die oben angepinnte Zeile
in "Meine Trader" — persistiert über adapter.set_focus_wallet().
"""
from __future__ import annotations

import asyncio
import datetime as _dt
import json
import os
import threading
import time

import pandas as pd
import streamlit as st

HISTORY_PATH = os.getenv("COPY_HISTORY_FILE", "copy_history.json")
CLOSE_REQUEST_PATH = os.getenv("COPY_CLOSE_REQUEST_FILE", "close_requests.json")

_WINDOW_LABEL = {"day": "1 Tag", "week": "1 Woche", "month": "1 Monat", "allTime": "Gesamt"}

_SIGNAL_ICON = {
    "COPY_OPEN_LONG": "📈", "COPY_OPEN_SHORT": "📉", "COPY_CLOSE_LONG": "🔴",
    "COPY_INCREASE": "⬆️", "COPY_DECREASE": "⬇️",
}


# ── Daten-Helpers ────────────────────────────────────────────────────────────

def _load_json(path: str, fallback):
    try:
        with open(path, encoding="utf-8") as file:
            return json.load(file)
    except (OSError, json.JSONDecodeError):
        return fallback


def _copy_positions() -> dict:
    """Own open Binance positions opened by the copy-trader (ignores legacy rows)."""
    raw = _load_json("positions.json", {})
    return {s: p for s, p in raw.items() if p.get("source") == "COPY"}


def _positions_of(wallet: str, short: str) -> dict:
    """Own open Binance positions attributed to one HL trader."""
    return {
        sym: p for sym, p in _copy_positions().items()
        if p.get("hl_trader_full") == wallet
        or (not p.get("hl_trader_full") and p.get("hl_trader") == short)
    }


def _request_close(symbols: list[str], reason: str) -> None:
    """Ask the bot process to close positions — the dashboard has no executor."""
    try:
        with open(CLOSE_REQUEST_PATH, "w", encoding="utf-8") as f:
            json.dump({"symbols": symbols, "reason": reason,
                       "requested_at": time.time()}, f, indent=2)
    except OSError as e:
        st.error(f"Verkaufs-Anfrage konnte nicht geschrieben werden: {e}")


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


@st.cache_data(ttl=30, show_spinner=False)
def _binance_balance() -> tuple[float, str]:
    """(balance, source). Live balance only when keys are set and DRY_RUN is off."""
    if _dry_run():
        return 0.0, "paper"
    try:
        from src.execution.binance_executor import get_account_balance
        return float(get_account_balance("USDT")), "live"
    except Exception as e:  # keys missing, network down, ...
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


# ── Adapter ──────────────────────────────────────────────────────────────────

@st.cache_resource
def _get_adapter():
    from src.adapters.hyperliquid_copytrader import HyperliquidCopyTrader
    adapter = HyperliquidCopyTrader()

    def _run():
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        loop.run_until_complete(adapter.start())

    threading.Thread(target=_run, daemon=True).start()
    return adapter


# ── Entry ────────────────────────────────────────────────────────────────────

def render():
    adapter = _get_adapter()

    st.markdown('<h2 style="margin-bottom:0;">🔄 Hyperliquid Copy-Trader</h2>',
                unsafe_allow_html=True)
    mode = "📝 PAPER (DRY-RUN)" if _dry_run() else "🔴 LIVE"
    st.caption(f"Signale von Hyperliquid-Tradern · Ausführung auf Binance Spot · Modus: {mode}")

    tab_home, tab_traders, tab_scan, tab_setup = st.tabs(
        ["🏠 Übersicht", "👥 Meine Trader", "🔍 Scanner", "⚙️ Setup"]
    )
    with tab_home:
        _render_overview(adapter)
    with tab_traders:
        _render_my_traders(adapter)
    with tab_scan:
        _render_scanner(adapter)
    with tab_setup:
        _render_setup(adapter)


# ── 🏠 Übersicht ─────────────────────────────────────────────────────────────

def _render_overview(adapter):

    @st.fragment(run_every="3s")
    def _live():
        status = adapter.status()
        copied = adapter.get_active_wallets()
        my_open = _copy_positions()
        history = _load_json(HISTORY_PATH, [])
        history = history if isinstance(history, list) else []

        # ── Health ──────────────────────────────────────────────────────
        health = status.get("api_health", "ok")
        if status["discovering"]:
            _banner("🔍 Trader-Suche läuft",
                    "Das HL-Leaderboard wird gescannt. Dauert 30–90s.", "amber")
        elif health == "rate_limited":
            _banner("⚠️ Hyperliquid rate-limited",
                    "API wartet auf Reset — Werte kommen aus dem Cache.", "amber")
        elif health == "unreachable":
            _banner("🔴 Hyperliquid unerreichbar",
                    "Netzwerkfehler zu api.hyperliquid.xyz. Automatischer Retry läuft.", "red")

        # ── Zeile 1: Geld & Modus ───────────────────────────────────────
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
                         f"{status['tracked_traders']} beobachtet gesamt"),
                    unsafe_allow_html=True)

        st.markdown("<br>", unsafe_allow_html=True)

        # ── Zeile 2: Verbindung ─────────────────────────────────────────
        if status["discovering"]:
            dot, dot_label = "#ffb400", "SUCHT TRADER"
        elif health == "rate_limited":
            dot, dot_label = "#ffb400", "RATE-LIMITED"
        elif health == "unreachable":
            dot, dot_label = "#ff5c5c", "UNREACHABLE"
        elif status["connected"]:
            dot, dot_label = "#00e6a7", "LIVE"
        else:
            dot, dot_label = "#ff5c5c", "VERBINDET"

        next_scan = status.get("next_scan_in")
        next_label = f"Rescan in {next_scan/3600:.1f}h" if next_scan and next_scan > 0 else "Rescan läuft"
        ws = "WebSocket verbunden" if status["ws_connected"] else "WebSocket reconnect"

        d1, d2, d3 = st.columns(3)
        d1.markdown(_kpi("🔌 Hyperliquid", f"● {dot_label}", ws, dot), unsafe_allow_html=True)
        d2.markdown(_kpi("🔍 Auto-Discovery",
                         "AN" if status["auto_discover"] else "AUS", next_label),
                    unsafe_allow_html=True)
        d3.markdown(_kpi("🎯 Angepinnt",
                         _short(adapter.get_focus_wallet() or "") or "keiner",
                         "bleibt über Neustart erhalten"), unsafe_allow_html=True)

        st.markdown("<br>", unsafe_allow_html=True)

        # ── Live-Feed + eigene offene Trades ────────────────────────────
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
            st.markdown('<div class="section-header">📡 Live-Aktivität</div>',
                        unsafe_allow_html=True)
            signals = adapter.get_signals()
            if not signals:
                st.info(
                    "Noch keine Signale in diesem Dashboard-Prozess. Trades und "
                    "PnL oben kommen aus den Dateien des Bots und sind aktuell — "
                    "der Signal-Feed hier baut sich erst nach dem Verbindungsaufbau auf."
                )
            else:
                html = '<div class="log-terminal" style="max-height:320px">'
                for s in signals[:12]:
                    html += (
                        '<div class="event-row">'
                        f'<span class="event-icon">{_SIGNAL_ICON.get(s.signal, "•")}</span>'
                        f'<span class="event-time">vor {s.age_sec:.0f}s</span>'
                        f'<span class="event-sym">{s.symbol}</span>'
                        f'<span class="event-msg">{s.trader_short} · ${s.size_usd:,.0f}</span>'
                        '</div>'
                    )
                st.markdown(html + "</div>", unsafe_allow_html=True)

    _live()


# ── 👥 Meine Trader ──────────────────────────────────────────────────────────

def _render_my_traders(adapter):
    focus = adapter.get_focus_wallet() or ""
    copied = set(adapter.get_active_wallets())

    # Nur bewusst gewählte Trader. Auto-entdeckte gehören in den Scanner, sonst
    # stünden hier plötzlich 12 fremde Zeilen.
    chosen = copied | ({focus} if focus else set())
    ordered = sorted(chosen, key=lambda w: (w != focus, w not in copied, w))

    st.markdown('<div class="section-header">👥 Meine Trader</div>',
                unsafe_allow_html=True)
    st.caption(
        "Hier stehen ausschließlich Trader, die du selbst übernommen hast. "
        "**Kopiert** = der Bot handelt ihn auf Binance. **Angepinnt** = wird "
        "überwacht und bleibt nach einem Neustart erhalten."
    )

    with st.form("add_trader_form", clear_on_submit=True):
        a1, a2, a3 = st.columns([3, 1, 1])
        new_wallet = a1.text_input("Wallet hinzufügen", label_visibility="collapsed",
                                   placeholder="0x1234…abcd")
        new_size = a2.number_input("Betrag $", min_value=1.0, value=_default_size(),
                                   step=5.0, label_visibility="collapsed")
        added = a3.form_submit_button("➕ Kopieren", type="primary", width="stretch")
    if added:
        w = new_wallet.strip()
        if not w.startswith("0x") or len(w) < 10:
            st.error("Ungültige Wallet-Adresse.")
        else:
            adapter.set_copy_size(w, new_size)
            adapter.activate_wallet(w)
            st.rerun()

    if not ordered:
        st.info(
            "Noch kein Trader übernommen. Gehe zum **🔍 Scanner**, finde einen "
            "passenden Trader und übernimm ihn mit deinem Wunschbetrag."
        )
        return

    _render_bulk_actions(adapter, ordered, copied, focus)

    stats = adapter.get_trader_stats()
    hl_positions = adapter.get_all_positions()

    for wallet in ordered:
        short = _short(wallet)
        is_copied = wallet in copied
        is_focus = wallet == focus
        override = adapter.get_copy_size(wallet)
        size = override or _default_size()
        my_open = _positions_of(wallet, short)
        history = _history_of(wallet, short)
        realized = sum(float(h.get("pnl_usdt", 0)) for h in history)
        exposure = sum(float(p.get("size_usdt", 0)) for p in my_open.values())
        trader_upnl = sum(p["unrealized_pnl"] for p in hl_positions.get(wallet, {}).values())
        s = stats.get(wallet)

        with st.container(border=True):
            _trader_header(short, is_copied, is_focus, size, bool(override),
                           realized, len(history), len(my_open), exposure,
                           trader_upnl, s)
            _trader_controls(adapter, wallet, short, size, is_copied, is_focus)

            with st.expander("▾ Details ansehen", expanded=False):
                t_mine, t_detail, t_verify = st.tabs(
                    ["💼 Meine Trades", "🎯 Trader-Details", "🔗 Verifikation"]
                )
                with t_mine:
                    _tab_my_trades(my_open, history, realized)
                with t_detail:
                    _tab_trader_detail(adapter, wallet, hl_positions.get(wallet, {}))
                with t_verify:
                    _tab_verify(wallet)


def _render_bulk_actions(adapter, ordered: list[str], copied: set[str], focus: str) -> None:
    open_syms = sorted({
        sym for w in ordered for sym in _positions_of(w, _short(w))
    })

    st.markdown(
        f'<div class="badge-info">{len(ordered)} Trader · {len(copied)} kopiert · '
        f'{len(open_syms)} offene Position(en)</div>',
        unsafe_allow_html=True,
    )
    with st.expander("⛔ Alles stoppen / entfernen", expanded=False):
        st.caption(
            "**Stoppen** beendet nur neue Copy-Orders. **Entfernen** löscht "
            "zusätzlich die gespeicherten Beträge und die Anpinnung. "
            "**Verkaufen** schließt die offenen Positionen dieser Trader — der "
            "Bot führt das aus, sobald er die Anfrage sieht (max. 2s)."
        )
        confirm = st.checkbox("Ja, ausführen", key="bulk_confirm")

        b1, b2 = st.columns(2)
        if b1.button(f"⛔ Alles stoppen, verkaufen & entfernen ({len(open_syms)} Position(en))",
                     key="bulk_nuke", type="primary",
                     disabled=not confirm, width="stretch"):
            if open_syms:
                _request_close(open_syms, "MANUAL_STOP_ALL")
            for w in ordered:
                adapter.clear_copy_size(w)
                adapter.deactivate_wallet(w)
            adapter.clear_focus_wallet()
            st.success(
                f"{len(ordered)} Trader entfernt. "
                + (f"Verkauf für {', '.join(open_syms)} angefordert."
                   if open_syms else "Es waren keine Positionen offen.")
            )
            st.rerun()

        if b2.button(f"⏸️ Nur Copy stoppen ({len(copied)})", key="bulk_stop",
                     disabled=not confirm or not copied, width="stretch"):
            for w in copied:
                adapter.deactivate_wallet(w)
            st.rerun()

        if open_syms:
            st.caption("Betroffene Positionen: " + ", ".join(open_syms))
        if not _dry_run():
            st.warning(
                "🔴 Im LIVE-Modus werden Positionen von ihrer OCO-Order verwaltet. "
                "Der Bot kann sie nicht automatisch schließen — bitte direkt auf "
                "Binance verkaufen."
            )

def _trader_header(short: str, is_copied: bool, is_focus: bool, size: float,
                   has_override: bool, realized: float, closed: int,
                   open_count: int, exposure: float, trader_upnl: float, stats) -> None:
    pin = '<span class="badge-info">📌 angepinnt</span> ' if is_focus else ""
    state = ('<span class="badge-win">🟢 KOPIERT</span>' if is_copied
             else '<span class="badge-info">👁 BEOBACHTET</span>')
    amount = (f"${size:,.0f} / Trade" if has_override
              else f"${size:,.0f} / Trade (Standard)")
    pnl_color = "#00e6a7" if realized >= 0 else "#ff5c5c"
    account = f"${stats.total_pnl:,.0f}" if stats else "—"

    st.markdown(
        f'<div style="display:flex;align-items:center;gap:10px;flex-wrap:wrap;'
        f'margin-bottom:6px">'
        f'<span style="font-family:JetBrains Mono,monospace;font-size:1.05rem;'
        f'font-weight:600">{short}</span>{pin}{state}</div>',
        unsafe_allow_html=True,
    )
    h1, h2, h3, h4, h5 = st.columns(5)
    h1.markdown(_kpi("💵 Betrag", amount.split(" /")[0], amount.split("/ ")[1]),
                unsafe_allow_html=True)
    h2.markdown(_kpi("💰 Mein PnL", f"${realized:+,.2f}",
                     f"{closed} geschlossen", pnl_color), unsafe_allow_html=True)
    h3.markdown(_kpi("📦 Offen", str(open_count), f"${exposure:,.0f} eingesetzt"),
                unsafe_allow_html=True)
    h4.markdown(_kpi("🎯 Trader uPnL", f"${trader_upnl:+,.0f}", "auf Hyperliquid",
                     "#00e6a7" if trader_upnl >= 0 else "#ff5c5c"),
                unsafe_allow_html=True)
    h5.markdown(_kpi("🏦 Account", account, "Trader-Guthaben"), unsafe_allow_html=True)


def _trader_controls(adapter, wallet: str, short: str, size: float,
                     is_copied: bool, is_focus: bool) -> None:
    c1, c2, c3, c4, c5 = st.columns([2, 1.4, 1, 1, 1])
    amount = c1.number_input(
        "Betrag pro Trade (USDT)", min_value=1.0, max_value=100000.0,
        value=float(size), step=5.0, key=f"amt_{wallet}",
        help="Gilt nur für diesen Trader und überschreibt "
             "BINANCE_POSITION_SIZE_USDT aus der .env.",
    )
    copy_on = c2.checkbox("Copy aktiv", value=is_copied, key=f"copy_{wallet}")

    c3.markdown("<div style='height:28px'></div>", unsafe_allow_html=True)
    if c3.button("💾 Speichern", key=f"save_{wallet}", type="primary", width="stretch"):
        adapter.set_copy_size(wallet, amount)
        if copy_on and not is_copied:
            adapter.activate_wallet(wallet)
        elif not copy_on and is_copied:
            adapter.deactivate_wallet(wallet)
        st.rerun()

    c4.markdown("<div style='height:28px'></div>", unsafe_allow_html=True)
    pin_label = "📌 Lösen" if is_focus else "📌 Anpinnen"
    if c4.button(pin_label, key=f"pin_{wallet}", width="stretch"):
        if is_focus:
            adapter.clear_focus_wallet()
        else:
            adapter.set_focus_wallet(wallet)
        st.rerun()

    c5.markdown("<div style='height:28px'></div>", unsafe_allow_html=True)
    if c5.button("🗑️ Entfernen", key=f"del_{wallet}", width="stretch"):
        open_syms = list(_positions_of(wallet, short))
        if open_syms:
            _request_close(open_syms, "MANUAL_REMOVE_TRADER")
        adapter.clear_copy_size(wallet)
        adapter.deactivate_wallet(wallet)
        if is_focus:
            adapter.clear_focus_wallet()
        st.rerun()

    mode = "📝 DRY-RUN — Orders werden simuliert" if _dry_run() else "🔴 LIVE — echtes Geld"
    st.caption(
        f"{mode} · Konsolen-Log: `[HL-{'COPY' if is_copied else 'WATCH'}]` für {short} · "
        f"Entfernen verkauft offene Positionen dieses Traders"
    )



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


def _tab_trader_detail(adapter, wallet: str, live_positions: dict) -> None:
    if live_positions:
        st.markdown("**Aktuelle Positionen des Traders (live)**")
        st.dataframe(
            pd.DataFrame([{
                "Coin":  coin,
                "Seite": "LONG" if p["size"] > 0 else "SHORT",
                "Wert":  p["value_usd"], "Entry": p["entry_px"],
                "Hebel": p["leverage"], "PnL %": p["pnl_pct"],
                "PnL $": p["unrealized_pnl"],
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
        m = focus["metrics_by_window"].get(name)
        with col:
            if m is None:
                st.markdown(_kpi(f"📅 {_WINDOW_LABEL[name]}", "—",
                                 "keine geschlossenen Trades", "#64748b"),
                            unsafe_allow_html=True)
                continue
            color = ("#00e6a7" if m.win_rate >= 0.55
                     else "#ffb400" if m.win_rate >= 0.45 else "#ff5c5c")
            st.markdown(_kpi(f"📅 {_WINDOW_LABEL[name]}", f"{m.win_rate:.0%}",
                             f"{m.trades} Trades · Ø {_fmt_hold(m.avg_hold_sec)}", color),
                        unsafe_allow_html=True)

    st.markdown("<br>**Letzte Fills des Traders**", unsafe_allow_html=True)
    fills = focus.get("recent_fills", [])
    if not fills:
        st.caption("Keine Fills verfügbar.")
        return
    now = _dt.datetime.now().timestamp()
    st.dataframe(
        pd.DataFrame([{
            "Alter":      _fmt_age(now - float(f.get("time", 0)) / 1000.0),
            "Coin":       f.get("coin", ""),
            "Richtung":   f.get("dir", ""),
            "Preis":      float(f.get("px", "0") or "0"),
            "Größe":      float(f.get("sz", "0") or "0"),
            "Closed PnL": float(f.get("closedPnl", "0") or "0"),
        } for f in fills]),
        width="stretch", hide_index=True,
        column_config={
            "Preis":      st.column_config.NumberColumn(format="$%.4f"),
            "Größe":      st.column_config.NumberColumn(format="%.4f"),
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
            bar.empty(); note.empty()
            st.error(f"Scan fehlgeschlagen: {e}")
            return
        bar.empty(); note.empty()
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

    copied = set(adapter.get_active_wallets())
    st.dataframe(
        pd.DataFrame([{
            "Status":        "🟢 kopiert" if r["wallet"] in copied else "—",
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

    # ── Übernehmen: Trader + Betrag in einem Schritt ────────────────────
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
            f"{_short(choice)} wird jetzt mit ${amount:,.0f} pro Trade kopiert "
            f"und ist im Tab **👥 Meine Trader** oben angepinnt."
        )
    st.caption(
        "Übernehmen setzt Betrag, aktiviert Copy-Trading und pinnt den Trader an. "
        "Alles davon lässt sich pro Trader nachträglich ändern."
    )


# ── ⚙️ Setup ─────────────────────────────────────────────────────────────────

def _render_setup(adapter):
    settings = adapter.get_settings()

    st.markdown('<div class="section-header">⚙️ Laufzeit-Einstellungen</div>',
                unsafe_allow_html=True)
    with st.form("settings_form"):
        c1, c2 = st.columns(2)
        poll_interval = c1.number_input(
            "Polling-Intervall (s)", min_value=1.0, max_value=120.0,
            value=float(settings["poll_interval"]), step=1.0,
            help="Wie oft die Positionen der Trader abgefragt werden.",
        )
        min_copy_size = c2.number_input(
            "Min. Signalgröße auf HL ($)", min_value=0.0,
            value=float(settings["min_copy_size_usd"]), step=100.0,
            help="Positionen des Traders unter diesem Wert lösen kein Signal aus. "
                 "Das ist NICHT dein Einsatz — den setzt du pro Trader.",
        )
        if st.form_submit_button("💾 Speichern", type="primary"):
            adapter.set_poll_interval(poll_interval)
            adapter.set_min_copy_size(min_copy_size)
            st.success("Gespeichert.")

    st.markdown('<div class="section-header">🔒 Aus der .env (nur lesend)</div>',
                unsafe_allow_html=True)
    st.markdown(f"""
    <div class="detail-grid">
      <div class="detail-item"><div class="detail-label">Modus</div><div class="detail-value">{"DRY-RUN" if _dry_run() else "LIVE"}</div></div>
      <div class="detail-item"><div class="detail-label">Standard-Betrag</div><div class="detail-value">${_default_size():,.0f}</div></div>
      <div class="detail-item"><div class="detail-label">Auto-Discovery</div><div class="detail-value">{"AN" if settings['auto_discover'] else "AUS"}</div></div>
      <div class="detail-item"><div class="detail-label">Max. Trader</div><div class="detail-value">{settings['max_tracked_traders']}</div></div>
      <div class="detail-item"><div class="detail-label">Rescan-Intervall</div><div class="detail-value">{settings['rescan_hours']:.1f}h</div></div>
      <div class="detail-item"><div class="detail-label">Min. Trades</div><div class="detail-value">{settings['min_trades']}</div></div>
      <div class="detail-item"><div class="detail-label">Min. Win-Rate</div><div class="detail-value">{settings['min_win_rate']:.0%}</div></div>
      <div class="detail-item"><div class="detail-label">Min. Account-Wert</div><div class="detail-value">${settings['min_account_value']:,.0f}</div></div>
      <div class="detail-item"><div class="detail-label">Max. Ø Haltezeit</div><div class="detail-value">{settings['max_avg_hold_min']:.0f} min</div></div>
      <div class="detail-item"><div class="detail-label">Signal-TTL</div><div class="detail-value">{settings['signal_ttl']:.0f}s</div></div>
      <div class="detail-item"><div class="detail-label">Metrik-Cache</div><div class="detail-value">{settings['metrics_cache_ttl_min']:.0f} min</div></div>
    </div>
    """, unsafe_allow_html=True)

    st.markdown('<div class="section-header">📡 Signal-Log</div>', unsafe_allow_html=True)
    f1, f2 = st.columns(2)
    symbol_filter = f1.text_input("Symbol filtern", "").strip().upper()
    max_rows = f2.slider("Max. Zeilen", 10, 200, 50)

    @st.fragment(run_every="3s")
    def _signals_live():
        signals = adapter.get_signals()
        if symbol_filter:
            signals = [s for s in signals if symbol_filter in s.symbol]
        signals = signals[:max_rows]
        if not signals:
            st.caption("Keine Signale.")
            return
        st.dataframe(
            pd.DataFrame([{
                "Alter": s.age_sec, "Trader": s.trader_short, "Signal": s.signal,
                "Symbol": s.symbol, "Größe": s.size_usd, "Entry": s.entry_price,
                "Hebel": s.leverage, "PnL %": s.pnl_pct,
            } for s in signals]),
            width="stretch", hide_index=True,
            column_config={
                "Alter": st.column_config.NumberColumn("Alter (s)", format="%.0f s"),
                "Größe": st.column_config.NumberColumn(format="$%.0f"),
                "Entry": st.column_config.NumberColumn(format="$%.4f"),
                "Hebel": st.column_config.NumberColumn(format="%.0fx"),
                "PnL %": st.column_config.NumberColumn(format="%+.2f%%"),
            },
        )
    _signals_live()
