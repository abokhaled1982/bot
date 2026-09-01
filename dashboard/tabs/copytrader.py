"""
dashboard/tabs/copytrader.py — Hyperliquid Copy-Trader Dashboard (4-Tab Layout)

Tabs:
  🏠 Uebersicht — System-Health, Portfolio-KPIs, Signal-Feed
  🔍 Scanner    — Leaderboard-Suche mit fenster-korrekten Metriken
  🎯 Focus      — Deep-Dive auf 1 Trader (Metriken je Zeitfenster, Fills, Positionen)
  ⚙️ Setup     — Getrackte Trader, manuelle Wallets, Signal-Log, Runtime-Settings

Der Focus-Tab liest st.session_state["focus_wallet"] — gesetzt vom Scanner-
"In Focus setzen"-Button. Alle Zahlen im Scanner beziehen sich auf DAS
gewaehlte Zeitfenster (nicht mehr Lifetime).
"""
from __future__ import annotations

import asyncio
import datetime as _dt
import json
import os
import threading

import pandas as pd
import streamlit as st


# ── Helpers ──────────────────────────────────────────────────────────────────

def _load_paper_positions() -> dict:
    try:
        with open("positions.json", encoding="utf-8") as file:
            return json.load(file)
    except (OSError, json.JSONDecodeError):
        return {}


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


_WINDOW_LABEL = {"day": "1 Tag", "week": "1 Woche", "month": "1 Monat", "allTime": "Gesamt"}


@st.cache_resource
def _get_adapter():
    """Return (or create) the singleton HyperliquidCopyTrader."""
    from src.adapters.hyperliquid_copytrader import HyperliquidCopyTrader
    adapter = HyperliquidCopyTrader()

    def _run():
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        loop.run_until_complete(adapter.start())

    t = threading.Thread(target=_run, daemon=True)
    t.start()
    return adapter


def _toggle_wallet(adapter, wallet: str, active: bool) -> None:
    if active:
        adapter.activate_wallet(wallet)
    else:
        adapter.deactivate_wallet(wallet)


def _set_focus(adapter, wallet: str) -> None:
    """Persist the focus wallet via the adapter and mirror into session state."""
    wallet = wallet.strip()
    adapter.set_focus_wallet(wallet)
    st.session_state["focus_wallet"] = wallet


def _clear_focus(adapter) -> None:
    adapter.clear_focus_wallet()
    st.session_state["focus_wallet"] = ""


def _verification_links(wallet: str) -> list[tuple[str, str]]:
    """Return (label, url) pairs for external verification of a HL wallet.

    Ordered by trust: HL native first (authoritative), community explorers after.
    URLs are external and may change — clicking is the reliable check.
    """
    return [
        ("Hyperliquid Explorer", f"https://app.hyperliquid.xyz/explorer/address/{wallet}"),
        ("Hyperdash",             f"https://hyperdash.info/trader/{wallet}"),
        ("Purrsec",               f"https://purrsec.com/address/{wallet}"),
        ("HypurrScan",            f"https://hypurrscan.io/address/{wallet}"),
        ("ASXN",                  f"https://asxn.xyz/user/{wallet}"),
    ]


# ── Entry ────────────────────────────────────────────────────────────────────

def render():
    adapter = _get_adapter()

    # First-load: seed session state from the persistent focus wallet so the
    # Focus tab keeps working across page reloads and bot restarts.
    if "focus_wallet" not in st.session_state:
        st.session_state["focus_wallet"] = adapter.get_focus_wallet() or ""

    st.markdown('<h2 style="margin-bottom:0;">🔄 Hyperliquid Copy-Trader</h2>', unsafe_allow_html=True)
    st.caption("Live-Signale von Top-Tradern auf Hyperliquid · Ausführung auf Binance")

    tab_overview, tab_scanner, tab_focus, tab_setup = st.tabs(
        ["🏠 Übersicht", "🔍 Scanner", "🎯 Focus", "⚙️ Setup"]
    )
    with tab_overview:
        _render_overview(adapter)
    with tab_scanner:
        _render_scanner(adapter)
    with tab_focus:
        _render_focus(adapter)
    with tab_setup:
        _render_setup(adapter)


# ── 🏠 Übersicht ─────────────────────────────────────────────────────────────

def _render_overview(adapter):

    @st.fragment(run_every="3s")
    def _live():
        status = adapter.status()
        positions = adapter.get_all_positions()
        signals = adapter.get_signals()

        all_pos = [p for pos in positions.values() for p in pos.values()]
        total_positions = len(all_pos)
        total_exposure = sum(p["value_usd"] for p in all_pos)
        total_upnl = sum(p["unrealized_pnl"] for p in all_pos)

        # ── System-Health-Banner ────────────────────────────────────────
        api_health = status.get("api_health", "ok")
        if status["discovering"]:
            _banner("🔍 Trader-Suche läuft",
                    "Das HL-Leaderboard wird gescannt (30–90s), pro Kandidat 1 API-Call.",
                    "amber")
        elif api_health == "rate_limited":
            _banner("⚠️ Hyperliquid rate-limited",
                    "Die API wartet auf Reset. Werte werden aus dem lokalen Cache angezeigt.",
                    "amber")
        elif api_health == "unreachable":
            _banner("🔴 Hyperliquid unerreichbar",
                    "Netzwerkfehler zu api.hyperliquid.xyz. Wiederhole automatisch.",
                    "red")

        # ── KPI-Karten ─────────────────────────────────────────────────
        if status["discovering"]:
            dot_color, dot_label = "#ffb400", "SUCHT TRADER…"
        elif api_health == "rate_limited":
            dot_color, dot_label = "#ffb400", "RATE-LIMITED"
        elif api_health == "unreachable":
            dot_color, dot_label = "#ff5c5c", "UNREACHABLE"
        elif status["connected"]:
            dot_color, dot_label = "#00e6a7", "LIVE"
        else:
            dot_color, dot_label = "#ff5c5c", "CONNECTING"

        ws_color = "#00e6a7" if status["ws_connected"] else "#64748b"
        pnl_color = "#00e6a7" if total_upnl >= 0 else "#ff5c5c"
        next_scan = status.get("next_scan_in")
        next_label = f"in {next_scan/3600:.1f}h" if next_scan and next_scan > 0 else "läuft..."

        c1, c2, c3, c4, c5 = st.columns(5)
        with c1:
            st.markdown(f"""
            <div class="kpi-card">
              <div class="label">🔄 Status</div>
              <div class="value" style="color:{dot_color};font-size:1.05rem">● {dot_label}</div>
              <div class="sub" style="color:{ws_color}">WS: {"connected" if status["ws_connected"] else "reconnecting"}</div>
            </div>""", unsafe_allow_html=True)
        with c2:
            st.markdown(f"""
            <div class="kpi-card">
              <div class="label">👥 Getrackte Trader</div>
              <div class="value">{status['tracked_traders']}</div>
              <div class="sub">{len(adapter.get_active_wallets())} manuell aktiv</div>
            </div>""", unsafe_allow_html=True)
        with c3:
            st.markdown(f"""
            <div class="kpi-card">
              <div class="label">📦 Offene Positionen</div>
              <div class="value">{total_positions}</div>
              <div class="sub">${total_exposure:,.0f} Exposure</div>
            </div>""", unsafe_allow_html=True)
        with c4:
            st.markdown(f"""
            <div class="kpi-card">
              <div class="label">💰 Unrealisiert PnL</div>
              <div class="value" style="color:{pnl_color}">${total_upnl:+,.0f}</div>
              <div class="sub">über alle getrackten Trader</div>
            </div>""", unsafe_allow_html=True)
        with c5:
            st.markdown(f"""
            <div class="kpi-card">
              <div class="label">🔍 Auto-Discovery</div>
              <div class="value" style="font-size:1.05rem">{"AN" if status['auto_discover'] else "AUS"}</div>
              <div class="sub">Rescan {next_label}</div>
            </div>""", unsafe_allow_html=True)

        st.markdown("<br>", unsafe_allow_html=True)

        # ── Positionen + Signal-Feed ───────────────────────────────────
        col_pos, col_sig = st.columns([3, 2])
        with col_pos:
            st.markdown('<div class="section-header">📦 Alle offenen Positionen</div>', unsafe_allow_html=True)
            rows = []
            for wallet, pos in positions.items():
                for coin, p in pos.items():
                    rows.append({
                        "Trader": _short(wallet), "Coin": coin,
                        "Seite": "LONG" if p["size"] > 0 else "SHORT",
                        "Wert": p["value_usd"], "Entry": p["entry_px"],
                        "Hebel": p["leverage"], "PnL%": p["pnl_pct"],
                        "PnL$": p["unrealized_pnl"],
                    })
            if rows:
                df = pd.DataFrame(rows).sort_values("Wert", ascending=False)
                st.dataframe(
                    df, width="stretch", hide_index=True, height=340,
                    column_config={
                        "Wert": st.column_config.NumberColumn(format="$%.0f"),
                        "Entry": st.column_config.NumberColumn(format="$%.4f"),
                        "Hebel": st.column_config.NumberColumn(format="%.0fx"),
                        "PnL%": st.column_config.NumberColumn(format="%+.2f%%"),
                        "PnL$": st.column_config.NumberColumn(format="$%+.0f"),
                    },
                )
            else:
                st.info("⏳ Noch keine offenen Positionen bei getrackten Tradern.")

        with col_sig:
            st.markdown('<div class="section-header">📡 Letzte Signale</div>', unsafe_allow_html=True)
            if not signals:
                st.info("Noch keine frischen Signale.")
            else:
                icon_map = {
                    "COPY_OPEN_LONG": "📈", "COPY_OPEN_SHORT": "📉", "COPY_CLOSE_LONG": "🔴",
                    "COPY_INCREASE": "⬆️", "COPY_DECREASE": "⬇️",
                }
                html = '<div class="log-terminal" style="max-height:360px">'
                for s in signals[:10]:
                    html += (
                        '<div class="event-row">'
                        f'<span class="event-icon">{icon_map.get(s.signal, "•")}</span>'
                        f'<span class="event-time">vor {s.age_sec:.0f}s</span>'
                        f'<span class="event-sym">{s.symbol}</span>'
                        f'<span class="event-type">{s.signal}</span>'
                        f'<span class="event-msg">{s.trader_short} · ${s.size_usd:,.0f}</span>'
                        '</div>'
                    )
                html += "</div>"
                st.markdown(html, unsafe_allow_html=True)

        our_positions = _load_paper_positions()
        if our_positions:
            with st.expander("💼 Unsere Binance-Positionen (Paper)", expanded=False):
                st.json(our_positions)

    _live()


# ── 🔍 Scanner ───────────────────────────────────────────────────────────────

def _render_scanner(adapter):
    from src.adapters.hyperliquid_copytrader import LEADERBOARD_WINDOWS, hl_explorer_url

    st.markdown('<div class="section-header">🔍 Trader-Scanner</div>', unsafe_allow_html=True)
    st.caption(
        "Alle Metriken (PnL, Trades, WinRate, Ø Hold) beziehen sich auf **das gewählte "
        "Zeitfenster** — nicht mehr auf Lifetime. Die Suche ist auf ~2s pro Kandidat "
        "gedrosselt (HL-Rate-Limit)."
    )

    with st.form("scanner_form"):
        c1, c2, c3, c4 = st.columns(4)
        with c1:
            window = st.selectbox(
                "Zeitfenster", LEADERBOARD_WINDOWS, index=0,
                format_func=lambda w: _WINDOW_LABEL.get(w, w),
            )
        with c2:
            min_win_rate = st.slider("Min. Win-Rate %", 0, 100, 55) / 100
        with c3:
            min_trades = st.number_input("Min. Trades im Fenster", min_value=0, value=20, step=5)
        with c4:
            max_hold_min = st.number_input("Max. Ø Hold (min)", min_value=0, value=60, step=5)

        c5, c6, c7 = st.columns(3)
        with c5:
            min_account = st.number_input("Min. Account-Wert ($)", min_value=0, value=10000, step=5000)
        with c6:
            limit = st.slider("Kandidaten-Anzahl", 5, 60, 20)
        with c7:
            sort_by = st.selectbox(
                "Sortieren nach",
                ["PnL (Fenster)", "Win-Rate", "Volumen (Liquidität)", "Trades", "Account-Wert"],
            )

        submitted = st.form_submit_button("🔎 Suchen", type="primary", width="stretch")

    if submitted:
        progress_bar = st.progress(0.0)
        status_text = st.empty()

        def _on_progress(done: int, total: int, wallet: str) -> None:
            progress_bar.progress(done / total)
            status_text.caption(f"Prüfe Kandidat {done}/{total}: {_short(wallet)} …")

        try:
            results = adapter.list_leaderboard(
                window=window, limit=limit,
                min_trades=min_trades, min_win_rate=min_win_rate,
                max_avg_hold_sec=max_hold_min * 60 if max_hold_min else None,
                min_account_value=min_account,
                progress_cb=_on_progress,
            )
        except Exception as e:
            progress_bar.empty()
            status_text.empty()
            st.error(f"Scan fehlgeschlagen: {e}")
            return

        progress_bar.empty()
        status_text.empty()
        st.session_state["search_results"] = results
        st.session_state["search_window"] = window
        st.session_state["search_ts"] = _dt.datetime.now().strftime("%H:%M:%S")

    results = list(st.session_state.get("search_results", []))
    if not results:
        if submitted:
            st.info("Keine Trader gefunden, die die Filter erfüllen. Prüfe die Schwellwerte oder wähle ein breiteres Fenster.")
        return

    sort_key = {
        "PnL (Fenster)": "window_pnl", "Win-Rate": "win_rate",
        "Volumen (Liquidität)": "window_volume", "Trades": "trades",
        "Account-Wert": "account_value",
    }.get(sort_by, "window_pnl")
    results.sort(key=lambda r: r.get(sort_key, 0), reverse=True)

    active = adapter.get_active_wallets()
    ts = st.session_state.get("search_ts", "")
    win_label = _WINDOW_LABEL.get(st.session_state.get("search_window", "day"), "?")
    st.markdown(
        f'<div class="badge-info">{len(results)} Trader · Fenster: {win_label} · gescannt {ts}</div>',
        unsafe_allow_html=True,
    )
    st.markdown("<br>", unsafe_allow_html=True)

    display_df = pd.DataFrame([{
        "Copy":          r["wallet"] in active,
        "Trader":        _short(r["wallet"]),
        "Account":       r["account_value"],
        "PnL (Fenster)": r["window_pnl"],
        "Volumen":       r["window_volume"],
        "Trades":        r["trades"],
        "Win-Rate":      r["win_rate"],
        "Ø Hold":        r["avg_hold_sec"] / 60,
        "Aktiv vor":     r["last_active_age"] / 3600,
        "Explorer":      hl_explorer_url(r["wallet"]),
    } for r in results])

    edited = st.data_editor(
        display_df, width="stretch", hide_index=True, key="scanner_editor",
        disabled=[c for c in display_df.columns if c != "Copy"],
        column_config={
            "Copy": st.column_config.CheckboxColumn(
                "Copy", help="Sofort aktivieren / deaktivieren", width="small"
            ),
            "Account":       st.column_config.NumberColumn(format="$%.0f"),
            "PnL (Fenster)": st.column_config.NumberColumn(
                format="$%+.0f", help=f"PnL im Fenster: {win_label}"
            ),
            "Volumen":       st.column_config.NumberColumn(format="$%.0f"),
            "Trades":        st.column_config.NumberColumn(
                format="%d", help=f"Geschlossene Trades im Fenster: {win_label}"
            ),
            "Win-Rate":      st.column_config.ProgressColumn(
                format="%.0f%%", min_value=0.0, max_value=1.0,
                help=f"Win-Rate im Fenster: {win_label}",
            ),
            "Ø Hold":        st.column_config.NumberColumn(
                format="%.1f min", help="Durchschnittliche Haltezeit pro Trade",
            ),
            "Aktiv vor":     st.column_config.NumberColumn(
                format="%.1f h", help="Zeit seit letztem Fill",
            ),
            "Explorer":      st.column_config.LinkColumn(
                "🔗 Verifizieren", display_text="Öffnen",
            ),
        },
    )

    # Sync Copy-Toggle back to the adapter
    changed = False
    wallets = [r["wallet"] for r in results]
    for i, wallet in enumerate(wallets):
        now_active = bool(edited.iloc[i]["Copy"])
        if now_active != (wallet in active):
            _toggle_wallet(adapter, wallet, now_active)
            changed = True
    if changed:
        st.rerun()

    # Focus-Auswahl
    st.markdown("<br>", unsafe_allow_html=True)
    st.markdown('<div class="section-header">🎯 Trader in Focus setzen</div>', unsafe_allow_html=True)
    fc1, fc2 = st.columns([4, 1])
    with fc1:
        focus_choice = st.selectbox(
            "Wähle einen Trader für den Deep-Dive im Focus-Tab",
            options=wallets, format_func=_short, key="focus_select",
        )
    with fc2:
        st.markdown("<br>", unsafe_allow_html=True)
        if st.button("→ In Focus", type="primary", width="stretch"):
            _set_focus(adapter, focus_choice)
            st.success(
                f"{_short(focus_choice)} steht jetzt im Focus — wird live überwacht "
                f"und bleibt über Reload/Neustart erhalten. Wechsle zum 🎯 Focus-Tab."
            )


# ── 🎯 Focus ─────────────────────────────────────────────────────────────────

def _render_focus(adapter):
    from src.adapters.hyperliquid_copytrader import hl_explorer_url

    wallet = st.session_state.get("focus_wallet", "").strip()

    st.markdown('<div class="section-header">🎯 Trader im Focus</div>', unsafe_allow_html=True)
    with st.form("focus_input_form"):
        c1, c2 = st.columns([4, 1])
        typed = c1.text_input(
            "Wallet-Adresse für Deep-Dive", value=wallet,
            placeholder="0x…", label_visibility="collapsed",
        )
        submitted = c2.form_submit_button("Anzeigen", type="primary", width="stretch")
    if submitted and typed.strip():
        _set_focus(adapter, typed.strip())
        wallet = typed.strip()

    if not wallet:
        st.info(
            "🎯 Kein Trader im Focus. Wähle einen im **🔍 Scanner** aus und klicke "
            "auf **In Focus**, oder gib hier direkt eine Wallet-Adresse ein."
        )
        return

    with st.spinner(f"Lade Deep-Dive für {_short(wallet)} …"):
        try:
            focus = adapter.get_trader_focus(wallet)
        except Exception as e:
            st.error(f"Focus-Daten konnten nicht geladen werden: {e}")
            return

    if focus is None:
        st.error("Ungültige Wallet-Adresse.")
        return

    # ── Header ──────────────────────────────────────────────────────────
    active = adapter.get_active_wallets()
    is_copy = wallet in active
    is_focus_persisted = adapter.get_focus_wallet() == wallet
    hc1, hc2, hc3 = st.columns([2, 2, 1])
    with hc1:
        pin_label = "📌 dauerhaft im Focus" if is_focus_persisted else "◻ nur Anzeige"
        st.markdown(f"""
        <div class="kpi-card" style="text-align:left">
          <div class="label">👤 Trader</div>
          <div class="value" style="font-family:'JetBrains Mono',monospace;font-size:0.95rem">{_short(wallet)}</div>
          <div class="sub">{pin_label} · <a href="{hl_explorer_url(wallet)}" target="_blank" style="color:#7cb4ff">HL Explorer</a></div>
        </div>""", unsafe_allow_html=True)
    with hc2:
        copy_badge = "🟢 Copy AN" if is_copy else "⚪️ Copy AUS"
        track_badge = "🛰️ live überwacht" if focus['is_tracked'] else "on-demand"
        st.markdown(f"""
        <div class="kpi-card" style="text-align:left">
          <div class="label">💼 Account-Wert</div>
          <div class="value">${focus['account_value']:,.0f}</div>
          <div class="sub">{focus['total_fills']} Fills · {track_badge} · {copy_badge}</div>
        </div>""", unsafe_allow_html=True)
    with hc3:
        st.markdown("<br>", unsafe_allow_html=True)
        if st.button("✕ Focus lösen", key=f"clear_focus_{wallet}", width="stretch"):
            _clear_focus(adapter)
            st.rerun()

    # ── Copy-Setup: Betrag + Toggle ─────────────────────────────────────
    st.markdown("<br>", unsafe_allow_html=True)
    st.markdown('<div class="section-header">💰 Copy-Setup</div>', unsafe_allow_html=True)

    dry_run = os.getenv("DRY_RUN", "True").lower() == "true"
    settings = adapter.get_settings()
    default_size = 10.0
    try:
        default_size = float(os.getenv("BINANCE_POSITION_SIZE_USDT", "10"))
    except ValueError:
        pass
    current_override = adapter.get_copy_size(wallet)
    current_size = current_override if current_override else default_size

    mode_badge = (
        '<span class="badge-warn">📝 DRY-RUN (Paper)</span>' if dry_run
        else '<span class="badge-loss">🔴 LIVE (echtes Geld)</span>'
    )
    st.markdown(
        f'{mode_badge} · Global-Default: <b>${default_size:.0f}</b> · '
        f'Min. Copy-Signalgröße auf HL-Seite: <b>${settings["min_copy_size_usd"]:,.0f}</b>',
        unsafe_allow_html=True,
    )

    with st.form(f"copy_setup_{wallet}"):
        sc1, sc2, sc3 = st.columns([2, 1, 1])
        with sc1:
            new_size = st.number_input(
                "Betrag pro Trade (USDT)", min_value=1.0, max_value=10000.0,
                value=float(current_size), step=5.0,
                help="Wird beim Öffnen jedes kopierten Trades verwendet. "
                     "Überschreibt BINANCE_POSITION_SIZE_USDT nur für DIESEN Trader.",
            )
        with sc2:
            enable = st.checkbox("Copy einschalten", value=is_copy)
        with sc3:
            submitted_copy = st.form_submit_button("💾 Übernehmen", type="primary", width="stretch")

    if submitted_copy:
        adapter.set_copy_size(wallet, new_size)
        if enable and not is_copy:
            _toggle_wallet(adapter, wallet, True)
        elif not enable and is_copy:
            _toggle_wallet(adapter, wallet, False)
        st.success(
            f"Copy-Setup gespeichert: ${new_size:.0f} pro Trade · "
            f"{'AN' if enable else 'AUS'} · {'DRY-RUN' if dry_run else 'LIVE'}"
        )
        st.rerun()

    if current_override:
        rc1, rc2 = st.columns([4, 1])
        rc1.caption(f"Aktueller Override für diesen Trader: **${current_override:.0f}** — statt Global-Default ${default_size:.0f}")
        if rc2.button("🔄 Auf Default zurücksetzen", key=f"reset_size_{wallet}"):
            adapter.clear_copy_size(wallet)
            st.rerun()

    # ── Sub-Tabs: Meine Trades / Trader-Aktivität / Verifikation ────────
    st.markdown("<br>", unsafe_allow_html=True)
    sub_mine, sub_trader, sub_verify = st.tabs(
        ["🧑 Meine Trades", "🎯 Trader-Aktivität", "🔗 Verifikation"]
    )

    with sub_mine:
        _render_focus_mine(wallet, dry_run)

    with sub_trader:
        _render_focus_trader(focus)

    with sub_verify:
        _render_focus_verify(wallet)


def _render_focus_mine(wallet: str, dry_run: bool) -> None:
    """My Binance positions that were opened by copying THIS trader."""
    all_positions = _load_paper_positions()
    # Match by full wallet (new positions) with fallback to trader_short (legacy)
    trader_short = _short(wallet)
    mine = {
        sym: p for sym, p in all_positions.items()
        if p.get("hl_trader_full") == wallet
        or (not p.get("hl_trader_full") and p.get("hl_trader") == trader_short)
    }

    mode_label = "Paper (DRY-RUN)" if dry_run else "LIVE"
    st.caption(f"Deine offenen Binance-Positionen aus Copies dieses Traders · Modus: **{mode_label}**")

    if not mine:
        st.info(
            "Noch keine offenen Copy-Positionen von diesem Trader. Sobald er ein "
            "neues Long öffnet und der Binance-Markt-Check ok ist, erscheint sie "
            "hier automatisch."
        )
        return

    now = _dt.datetime.now().timestamp()
    rows = []
    total_exposure = 0.0
    for sym, p in mine.items():
        opened = float(p.get("opened_at", 0))
        size = float(p.get("size_usdt", 0))
        total_exposure += size
        rows.append({
            "Symbol":    sym,
            "Größe":     size,
            "Entry":     float(p.get("entry_price", 0)),
            "Qty":       float(p.get("qty", 0)),
            "Alter":     _fmt_age(now - opened) if opened else "?",
            "HL-Coin":   p.get("hl_coin", ""),
            "HL-Hebel":  float(p.get("hl_leverage", 0)),
            "OrderID":   str(p.get("order_id", ""))[:12],
        })
    st.dataframe(
        pd.DataFrame(rows), width="stretch", hide_index=True,
        column_config={
            "Größe":    st.column_config.NumberColumn(format="$%.0f"),
            "Entry":    st.column_config.NumberColumn(format="$%.4f"),
            "Qty":      st.column_config.NumberColumn(format="%.6f"),
            "HL-Hebel": st.column_config.NumberColumn(format="%.0fx"),
        },
    )
    st.markdown(
        f'<div class="badge-info">Positionen: {len(mine)} · Exposure: ${total_exposure:,.0f}</div>',
        unsafe_allow_html=True,
    )


def _render_focus_trader(focus: dict) -> None:
    """The trader's own state on Hyperliquid — metrics, positions, fills."""
    st.markdown('<div class="section-header">📊 Metriken je Zeitfenster</div>', unsafe_allow_html=True)
    st.caption("Aus `userFills` berechnet — was Hyperliquid für dieses Fenster kennt.")

    mcols = st.columns(4)
    for col, w_name in zip(mcols, ("day", "week", "month", "allTime")):
        m = focus["metrics_by_window"].get(w_name)
        label = _WINDOW_LABEL[w_name]
        with col:
            if m is None:
                st.markdown(f"""
                <div class="kpi-card">
                  <div class="label">📅 {label}</div>
                  <div class="value" style="color:#64748b;font-size:1rem">—</div>
                  <div class="sub">keine geschlossenen Trades</div>
                </div>""", unsafe_allow_html=True)
                continue
            wr_color = "#00e6a7" if m.win_rate >= 0.55 else "#ffb400" if m.win_rate >= 0.45 else "#ff5c5c"
            st.markdown(f"""
            <div class="kpi-card">
              <div class="label">📅 {label}</div>
              <div class="value" style="color:{wr_color}">{m.win_rate:.0%}</div>
              <div class="sub">{m.trades} Trades · Ø Hold {_fmt_hold(m.avg_hold_sec)}</div>
            </div>""", unsafe_allow_html=True)

    st.markdown("<br>", unsafe_allow_html=True)
    st.markdown('<div class="section-header">📦 Trader-Live-Positionen</div>', unsafe_allow_html=True)
    positions = focus.get("positions", {})
    if not positions:
        st.info("Keine offenen Positionen bei diesem Trader.")
    else:
        rows = [{
            "Coin": coin,
            "Seite": "LONG" if p["size"] > 0 else "SHORT",
            "Wert": p["value_usd"], "Entry": p["entry_px"],
            "Hebel": p["leverage"], "PnL%": p["pnl_pct"],
            "PnL$": p["unrealized_pnl"],
        } for coin, p in positions.items()]
        st.dataframe(
            pd.DataFrame(rows), width="stretch", hide_index=True,
            column_config={
                "Wert":  st.column_config.NumberColumn(format="$%.0f"),
                "Entry": st.column_config.NumberColumn(format="$%.4f"),
                "Hebel": st.column_config.NumberColumn(format="%.0fx"),
                "PnL%":  st.column_config.NumberColumn(format="%+.2f%%"),
                "PnL$":  st.column_config.NumberColumn(format="$%+.0f"),
            },
        )

    st.markdown("<br>", unsafe_allow_html=True)
    st.markdown('<div class="section-header">📜 Letzte Fills</div>', unsafe_allow_html=True)
    fills = focus.get("recent_fills", [])
    if not fills:
        st.info("Keine Fills verfügbar.")
        return
    rows = []
    now = _dt.datetime.now().timestamp()
    for f in fills:
        ts = float(f.get("time", 0)) / 1000.0
        age = now - ts if ts else 0
        rows.append({
            "Alter":      _fmt_age(age),
            "Coin":       f.get("coin", ""),
            "Richtung":   f.get("dir", ""),
            "Preis":      float(f.get("px", "0") or "0"),
            "Größe":      float(f.get("sz", "0") or "0"),
            "Closed PnL": float(f.get("closedPnl", "0") or "0"),
        })
    st.dataframe(
        pd.DataFrame(rows), width="stretch", hide_index=True,
        column_config={
            "Preis":      st.column_config.NumberColumn(format="$%.4f"),
            "Größe":      st.column_config.NumberColumn(format="%.4f"),
            "Closed PnL": st.column_config.NumberColumn(format="$%+.2f"),
        },
    )


def _render_focus_verify(wallet: str) -> None:
    st.caption(
        "Hyperliquid selbst zeigt wenig Details. Diese Community-Explorer liefern "
        "PnL-Kurven, Charts pro Coin und mehr — Links öffnen in einem neuen Tab. "
        "Wenn ein Link 404 gibt, hat der Anbieter den URL-Pfad geändert; die "
        "Wallet-Adresse unten steht dann zum Kopieren bereit."
    )
    links = _verification_links(wallet)
    link_html = " · ".join(
        f'<a href="{url}" target="_blank" style="color:#7cb4ff;text-decoration:none">'
        f'<span class="badge-info" style="margin-right:6px">{label} ↗</span></a>'
        for label, url in links
    )
    st.markdown(f'<div style="margin:12px 0">{link_html}</div>', unsafe_allow_html=True)
    st.code(wallet, language=None)


# ── ⚙️ Setup ────────────────────────────────────────────────────────────────

def _render_setup(adapter):
    from src.adapters.hyperliquid_copytrader import MANUAL_WALLETS, hl_explorer_url

    # ── Getrackte Trader ─────────────────────────────────────────────────
    st.markdown('<div class="section-header">👥 Getrackte Trader</div>', unsafe_allow_html=True)

    with st.form("add_wallet_form", clear_on_submit=True):
        c1, c2 = st.columns([4, 1])
        wallet_input = c1.text_input(
            "Wallet manuell hinzufügen", label_visibility="collapsed",
            placeholder="0x1234…abcd",
        )
        add_submitted = c2.form_submit_button("➕ Hinzufügen", type="primary", width="stretch")
    if add_submitted:
        w = wallet_input.strip()
        if not w.startswith("0x") or len(w) < 10:
            st.error("Ungültige Wallet-Adresse.")
        else:
            adapter.activate_wallet(w)
            st.success(f"{_short(w)} wird jetzt getrackt.")
            st.rerun()

    @st.fragment(run_every="5s")
    def _traders_live():
        wallets = adapter.get_wallets()
        stats = adapter.get_trader_stats()
        positions = adapter.get_all_positions()
        active = adapter.get_active_wallets()

        if not wallets:
            st.info("⏳ Noch keine Trader aktiv. Nutze den Scanner oder warte auf Auto-Discovery.")
            return

        rows = []
        for w in wallets:
            s = stats.get(w)
            pos = positions.get(w, {})
            if w in MANUAL_WALLETS:
                quelle = "env"
            elif w in active:
                quelle = "manuell"
            else:
                quelle = "auto"
            rows.append({
                "Trader":       _short(w),
                "Quelle":       quelle,
                "Account-Wert": s.total_pnl if s else None,
                "Fills":        s.total_trades if s else None,
                "Positionen":   len(pos),
                "Exposure":     sum(p["value_usd"] for p in pos.values()),
                "PnL":          sum(p["unrealized_pnl"] for p in pos.values()),
                "Coins":        ", ".join(pos.keys()) or "-",
                "Explorer":     hl_explorer_url(w),
            })
        st.dataframe(
            pd.DataFrame(rows), width="stretch", hide_index=True,
            column_config={
                "Account-Wert": st.column_config.NumberColumn(format="$%.0f"),
                "Exposure":     st.column_config.NumberColumn(format="$%.0f"),
                "PnL":          st.column_config.NumberColumn(format="$%+.0f"),
                "Explorer":     st.column_config.LinkColumn("🔗 Verifizieren", display_text="Öffnen"),
            },
        )

        # Entfernen (nur nicht-env)
        removable = [w for w in wallets if w not in MANUAL_WALLETS]
        if removable:
            st.markdown("<br>", unsafe_allow_html=True)
            st.markdown('<div class="section-header">🗑️ Trader entfernen</div>', unsafe_allow_html=True)
            cols = st.columns(3)
            for i, w in enumerate(removable):
                with cols[i % 3]:
                    if st.button(f"✕ {_short(w)}", key=f"rm_{w}", width="stretch"):
                        adapter.deactivate_wallet(w)
                        st.rerun()
        else:
            st.caption(
                "Alle getrackten Trader stammen aus HL_TRADER_WALLETS (.env) und "
                "können nur dort entfernt werden."
            )
    _traders_live()

    # ── Signal-Log ────────────────────────────────────────────────────────
    st.markdown("<br>", unsafe_allow_html=True)
    st.markdown('<div class="section-header">📡 Signal-Log</div>', unsafe_allow_html=True)
    sc1, sc2, sc3 = st.columns(3)
    with sc1:
        symbol_filter = st.text_input("Symbol filtern (z. B. BTC)", "").strip().upper()
    with sc2:
        type_filter = st.multiselect(
            "Signal-Typ",
            ["COPY_OPEN_LONG", "COPY_OPEN_SHORT", "COPY_CLOSE_LONG", "COPY_INCREASE", "COPY_DECREASE"],
        )
    with sc3:
        limit = st.slider("Max. Anzahl", 10, 200, 50)

    @st.fragment(run_every="3s")
    def _signals_live():
        signals = adapter.get_signals()
        if symbol_filter:
            signals = [s for s in signals if symbol_filter in s.symbol]
        if type_filter:
            signals = [s for s in signals if s.signal in type_filter]
        signals = signals[:limit]
        if not signals:
            st.info("Keine Signale gefunden.")
            return
        df = pd.DataFrame([{
            "Alter": s.age_sec, "Trader": s.trader_short, "Signal": s.signal,
            "Symbol": s.symbol, "Größe": s.size_usd, "Entry": s.entry_price,
            "Hebel": s.leverage, "PnL%": s.pnl_pct,
        } for s in signals])
        st.dataframe(
            df, width="stretch", hide_index=True,
            column_config={
                "Alter":  st.column_config.NumberColumn("Alter (s)", format="%.0f s"),
                "Größe":  st.column_config.NumberColumn(format="$%.0f"),
                "Entry":  st.column_config.NumberColumn(format="$%.4f"),
                "Hebel":  st.column_config.NumberColumn(format="%.0fx"),
                "PnL%":   st.column_config.NumberColumn(format="%+.2f%%"),
            },
        )
    _signals_live()

    # ── Runtime-Settings + Env-Anzeige ────────────────────────────────────
    st.markdown("<br>", unsafe_allow_html=True)
    st.markdown('<div class="section-header">⚙️ Laufzeit-Einstellungen</div>', unsafe_allow_html=True)
    settings = adapter.get_settings()
    with st.form("settings_form"):
        c1, c2 = st.columns(2)
        with c1:
            poll_interval = st.number_input(
                "Polling-Intervall (Sekunden)", min_value=1.0, max_value=120.0,
                value=float(settings["poll_interval"]), step=1.0,
                help="Wie oft die Positionen der getrackten Trader abgefragt werden.",
            )
        with c2:
            min_copy_size = st.number_input(
                "Min. Copy-Größe ($)", min_value=0.0,
                value=float(settings["min_copy_size_usd"]), step=100.0,
                help="Neue Positionen unter diesem Wert lösen kein Copy-Signal aus.",
            )
        saved = st.form_submit_button("💾 Speichern", type="primary")
    if saved:
        adapter.set_poll_interval(poll_interval)
        adapter.set_min_copy_size(min_copy_size)
        st.success("Einstellungen gespeichert.")

    st.markdown('<div class="section-header">🔒 Auto-Discovery (.env, nur lesend)</div>', unsafe_allow_html=True)
    st.markdown(f"""
    <div class="detail-grid">
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
    st.caption(
        "Metrik-Cache: Win-Rate/Trades/Haltezeit pro (Wallet, Fenster) werden für "
        "diese Dauer wiederverwendet. Restliche Werte kommen aus .env."
    )


# ── UI-Helpers ───────────────────────────────────────────────────────────────

def _banner(title: str, text: str, kind: str) -> None:
    color = {"amber": "#ffb400", "red": "#ff5c5c", "blue": "#3b8bff"}.get(kind, "#3b8bff")
    st.markdown(
        f'<div class="insight-box" style="border-left-color:{color}">'
        f'  <div class="title" style="color:{color}">{title}</div>'
        f'  <div class="text">{text}</div>'
        '</div>',
        unsafe_allow_html=True,
    )
