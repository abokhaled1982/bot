"""
dashboard/tabs/copytrader.py — Hyperliquid Copy-Trader Dashboard

Volles Kontrollzentrum fuer den Copy-Trading-Bot:
  • Übersicht  — Live-KPIs, Gesamt-Exposure/PnL, alle offenen Positionen, Signal-Feed
  • Trader-Suche — HL-Leaderboard filtern/sortieren, mit einem Klick copy-traden,
    Explorer-Link fuer manuelle Verifikation
  • Meine Trader — getrackte Wallets verwalten, manuell hinzufuegen/entfernen
  • Signale — voller Signal-Log mit Filtern
  • Einstellungen — Polling-Intervall & Min. Copy-Groesse live anpassen
"""
import asyncio
import json
import threading

import pandas as pd
import streamlit as st


def _load_paper_positions() -> dict:
    try:
        with open("positions.json", encoding="utf-8") as file:
            return json.load(file)
    except (OSError, json.JSONDecodeError):
        return {}


def _short(wallet: str) -> str:
    return f"{wallet[:6]}...{wallet[-4:]}" if len(wallet) > 12 else wallet


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


def render():
    adapter = _get_adapter()

    st.markdown('<h2 style="margin-bottom:0;">🔄 Hyperliquid Copy-Trader</h2>', unsafe_allow_html=True)
    st.caption("Live-Signale von Top-Tradern auf Hyperliquid · Ausführung auf Binance")

    tab_overview, tab_search, tab_traders, tab_signals, tab_settings = st.tabs(
        ["📊 Übersicht", "🔍 Trader-Suche", "👥 Meine Trader", "📡 Signale", "⚙️ Einstellungen"]
    )

    with tab_overview:
        _render_overview(adapter)
    with tab_search:
        _render_search(adapter)
    with tab_traders:
        _render_my_traders(adapter)
    with tab_signals:
        _render_signals(adapter)
    with tab_settings:
        _render_settings(adapter)


# ── 📊 Übersicht ──────────────────────────────────────────────────────────────

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

        if status["discovering"]:
            st.markdown(
                '<div class="insight-box">'
                '<div class="title">🔍 Trader-Suche läuft</div>'
                '<div class="text">Das Hyperliquid-Leaderboard wird gescannt und pro Kandidat einzeln '
                'auf Win-Rate/Trades/Haltezeit geprüft. Die öffentliche API ist ratenlimitiert — '
                'das kann beim ersten Start bzw. nach einem Rescan 30–90s dauern.</div>'
                '</div>', unsafe_allow_html=True,
            )

        c1, c2, c3, c4, c5 = st.columns(5)
        if status["discovering"]:
            dot_color, dot_label = "#ffb400", "SUCHT TRADER…"
        elif status["connected"]:
            dot_color, dot_label = "#00e6a7", "LIVE"
        else:
            dot_color, dot_label = "#ff5c5c", "CONNECTING"
        ws_color = "#00e6a7" if status["ws_connected"] else "#64748b"
        pnl_color = "#00e6a7" if total_upnl >= 0 else "#ff5c5c"
        next_scan = status.get("next_scan_in")
        next_label = f"in {next_scan/3600:.1f}h" if next_scan and next_scan > 0 else "läuft..."

        with c1:
            st.markdown(f"""
            <div class="kpi-card">
              <div class="label">🔄 Status</div>
              <div class="value" style="color:{dot_color};font-size:1.1rem">● {dot_label}</div>
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
              <div class="value" style="font-size:1.1rem">{"AN" if status['auto_discover'] else "AUS"}</div>
              <div class="sub">Rescan {next_label}</div>
            </div>""", unsafe_allow_html=True)

        st.markdown("<br>", unsafe_allow_html=True)
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
                st.info("⏳ Keine offenen Positionen bei getrackten Tradern.")

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
            st.markdown('<div class="section-header">💼 Unsere Binance-Positionen</div>', unsafe_allow_html=True)
            st.json(our_positions)

    _live()


# ── 🔍 Trader-Suche ───────────────────────────────────────────────────────────

def _render_search(adapter):
    from src.adapters.hyperliquid_copytrader import LEADERBOARD_WINDOWS, hl_explorer_url

    st.markdown('<div class="section-header">🔍 Trader-Suche & Filter</div>', unsafe_allow_html=True)

    with st.form("trader_search"):
        c1, c2, c3, c4 = st.columns(4)
        with c1:
            window = st.selectbox(
                "Zeitfenster", LEADERBOARD_WINDOWS, index=0,
                format_func=lambda w: {"day": "1 Tag", "week": "1 Woche", "month": "1 Monat", "allTime": "Gesamt"}[w],
            )
        with c2:
            min_win_rate = st.slider("Min. Win-Rate %", 0, 100, 55) / 100
        with c3:
            min_trades = st.number_input("Min. Trades", min_value=0, value=50, step=10)
        with c4:
            max_hold_min = st.number_input("Max. Ø Haltezeit (min)", min_value=0, value=30, step=5)

        c5, c6, c7 = st.columns(3)
        with c5:
            min_account_value = st.number_input("Min. Account-Wert ($)", min_value=0, value=0, step=1000)
        with c6:
            limit = st.slider("Anzahl Kandidaten (Scan)", 5, 60, 15)
        with c7:
            sort_by = st.selectbox(
                "Sortieren nach",
                ["PnL (Fenster)", "Win-Rate", "Volumen (Liquidität)", "Trades", "Account-Wert"],
            )

        submitted = st.form_submit_button("🔎 Suchen", type="primary", width="stretch")

    st.caption(
        "⚠️ Die Hyperliquid-API ist ratenlimitiert (~2s pro Kandidat) und wird auch vom "
        "laufenden Copy-Trader im Hintergrund genutzt — die Suche kann daher je nach "
        "Kandidatenzahl 30s bis über eine Minute dauern."
    )

    if submitted:
        progress_bar = st.progress(0.0)
        status_text = st.empty()

        def _on_progress(done: int, total: int, wallet: str) -> None:
            progress_bar.progress(done / total)
            status_text.caption(f"Prüfe Kandidat {done}/{total}: {_short(wallet)}...")

        results = adapter.list_leaderboard(
            window=window, limit=limit,
            min_trades=min_trades, min_win_rate=min_win_rate,
            max_avg_hold_sec=max_hold_min * 60 if max_hold_min else None,
            min_account_value=min_account_value,
            progress_cb=_on_progress,
        )
        progress_bar.empty()
        status_text.empty()
        st.session_state["search_results"] = results
        st.session_state["search_window"] = window

    results = list(st.session_state.get("search_results", []))
    if results:
        sort_key = {
            "PnL (Fenster)": "window_pnl", "Win-Rate": "win_rate",
            "Volumen (Liquidität)": "window_volume", "Trades": "trades",
            "Account-Wert": "account_value",
        }.get(sort_by, "window_pnl")
        results.sort(key=lambda r: r.get(sort_key, 0), reverse=True)

        active = adapter.get_active_wallets()
        st.caption(
            f"{len(results)} Trader gefunden ({st.session_state.get('search_window', 'day')}-Fenster) "
            f"· sortiert nach {sort_by}"
        )

        wallets = [r["wallet"] for r in results]
        display_df = pd.DataFrame([{
            "Aktiv": r["wallet"] in active,
            "Trader": _short(r["wallet"]),
            "Account-Wert": r["account_value"],
            "PnL (Fenster)": r["window_pnl"],
            "Volumen (Liquidität)": r["window_volume"],
            "Win-Rate": r["win_rate"],
            "Trades": r["trades"],
            "Ø Hold (min)": r["avg_hold_sec"] / 60,
            "Explorer": hl_explorer_url(r["wallet"]),
        } for r in results])

        edited = st.data_editor(
            display_df, width="stretch", hide_index=True, key="search_results_editor",
            disabled=[c for c in display_df.columns if c != "Aktiv"],
            column_config={
                "Aktiv": st.column_config.CheckboxColumn("Copy-Trade", help="Sofort aktivieren/deaktivieren"),
                "Account-Wert": st.column_config.NumberColumn(format="$%.0f"),
                "PnL (Fenster)": st.column_config.NumberColumn(format="$%.0f"),
                "Volumen (Liquidität)": st.column_config.NumberColumn(format="$%.0f"),
                "Win-Rate": st.column_config.ProgressColumn(format="%.0f%%", min_value=0.0, max_value=1.0),
                "Ø Hold (min)": st.column_config.NumberColumn(format="%.1f min"),
                "Explorer": st.column_config.LinkColumn("🔗 Verifizieren", display_text="Öffnen"),
            },
        )

        changed = False
        for i, wallet in enumerate(wallets):
            now_active = bool(edited.iloc[i]["Aktiv"])
            if now_active != (wallet in active):
                _toggle_wallet(adapter, wallet, now_active)
                changed = True
        if changed:
            st.rerun()
    elif submitted:
        st.info("Keine Trader gefunden, die die Filter erfüllen.")


# ── 👥 Meine Trader ───────────────────────────────────────────────────────────

def _render_my_traders(adapter):
    from src.adapters.hyperliquid_copytrader import MANUAL_WALLETS, hl_explorer_url

    st.markdown('<div class="section-header">➕ Trader manuell hinzufügen</div>', unsafe_allow_html=True)
    with st.form("add_wallet_form", clear_on_submit=True):
        c1, c2 = st.columns([4, 1])
        wallet_input = c1.text_input(
            "Wallet-Adresse", label_visibility="collapsed", placeholder="0x1234...abcd",
        )
        add_submitted = c2.form_submit_button("Hinzufügen", type="primary", width="stretch")

    if add_submitted:
        w = wallet_input.strip()
        if not w.startswith("0x") or len(w) < 10:
            st.error("Ungültige Wallet-Adresse.")
        else:
            adapter.activate_wallet(w)
            st.success(f"{_short(w)} wird jetzt getrackt.")
            st.rerun()

    st.markdown("<br>", unsafe_allow_html=True)

    @st.fragment(run_every="5s")
    def _live():
        wallets = adapter.get_wallets()
        stats = adapter.get_trader_stats()
        positions = adapter.get_all_positions()
        active = adapter.get_active_wallets()

        st.markdown('<div class="section-header">👥 Getrackte Trader</div>', unsafe_allow_html=True)
        if not wallets:
            st.info("⏳ Noch keine Trader aktiv. Nutze die Trader-Suche oder warte auf Auto-Discovery.")
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
                "Trader": _short(w), "Quelle": quelle,
                "Account-Wert": s.total_pnl if s else None,
                "Fills": s.total_trades if s else None,
                "Positionen": len(pos),
                "Exposure": sum(p["value_usd"] for p in pos.values()),
                "PnL": sum(p["unrealized_pnl"] for p in pos.values()),
                "Coins": ", ".join(pos.keys()) or "-",
                "Explorer": hl_explorer_url(w),
            })
        df = pd.DataFrame(rows)
        st.dataframe(
            df, width="stretch", hide_index=True,
            column_config={
                "Account-Wert": st.column_config.NumberColumn(format="$%.0f"),
                "Exposure": st.column_config.NumberColumn(format="$%.0f"),
                "PnL": st.column_config.NumberColumn(format="$%+.0f"),
                "Explorer": st.column_config.LinkColumn("🔗 Verifizieren", display_text="Öffnen"),
            },
        )

        st.markdown("<br>", unsafe_allow_html=True)
        removable = [w for w in wallets if w not in MANUAL_WALLETS]
        if removable:
            st.markdown('<div class="section-header">🗑️ Trader entfernen</div>', unsafe_allow_html=True)
            cols = st.columns(3)
            for i, w in enumerate(removable):
                with cols[i % 3]:
                    if st.button(f"✕ {_short(w)}", key=f"rm_{w}", width="stretch"):
                        adapter.deactivate_wallet(w)
                        st.rerun()
        else:
            st.caption("Alle getrackten Trader stammen aus HL_TRADER_WALLETS (.env) und können hier nicht entfernt werden.")

    _live()


# ── 📡 Signale ────────────────────────────────────────────────────────────────

def _render_signals(adapter):
    st.markdown('<div class="section-header">📡 Signale & Filter</div>', unsafe_allow_html=True)

    c1, c2, c3 = st.columns(3)
    with c1:
        symbol_filter = st.text_input("Symbol filtern (z. B. BTC)", "").strip().upper()
    with c2:
        type_filter = st.multiselect(
            "Signal-Typ",
            ["COPY_OPEN_LONG", "COPY_OPEN_SHORT", "COPY_CLOSE_LONG", "COPY_INCREASE", "COPY_DECREASE"],
        )
    with c3:
        limit = st.slider("Max. Anzahl", 10, 200, 50)

    @st.fragment(run_every="3s")
    def _live():
        signals = adapter.get_signals()
        if symbol_filter:
            signals = [s for s in signals if symbol_filter in s.symbol]
        if type_filter:
            signals = [s for s in signals if s.signal in type_filter]
        signals = signals[:limit]

        if not signals:
            st.info("Keine Signale gefunden, die die Filter erfüllen.")
            return

        df = pd.DataFrame([{
            "Alter": s.age_sec, "Trader": s.trader_short, "Signal": s.signal,
            "Symbol": s.symbol, "Größe": s.size_usd, "Entry": s.entry_price,
            "Hebel": s.leverage, "PnL%": s.pnl_pct,
        } for s in signals])
        st.dataframe(
            df, width="stretch", hide_index=True,
            column_config={
                "Alter": st.column_config.NumberColumn("Alter (s)", format="%.0f s"),
                "Größe": st.column_config.NumberColumn(format="$%.0f"),
                "Entry": st.column_config.NumberColumn(format="$%.4f"),
                "Hebel": st.column_config.NumberColumn(format="%.0fx"),
                "PnL%": st.column_config.NumberColumn(format="%+.2f%%"),
            },
        )

    _live()


# ── ⚙️ Einstellungen ──────────────────────────────────────────────────────────

def _render_settings(adapter):
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

    st.markdown("<br>", unsafe_allow_html=True)
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
    </div>
    """, unsafe_allow_html=True)
    st.caption("Diese Werte werden über Umgebungsvariablen (.env) gesetzt und hier nur angezeigt.")

