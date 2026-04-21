import streamlit as st
from dashboard.db import db_query, get_recent_events
from dashboard.components import fmt_usd, kpi_card


_LOG_BADGE = {
    "ERROR":   '<span style="background:rgba(255,92,92,0.12);color:#ff5c5c;border:1px solid rgba(255,92,92,0.2);padding:3px 10px;border-radius:6px;font-size:0.82rem;font-weight:700">ERR</span>',
    "WARNING": '<span style="background:rgba(255,180,0,0.12);color:#ffb400;border:1px solid rgba(255,180,0,0.2);padding:3px 10px;border-radius:6px;font-size:0.82rem;font-weight:700">WRN</span>',
    "SUCCESS": '<span style="background:rgba(0,230,167,0.12);color:#00e6a7;border:1px solid rgba(0,230,167,0.2);padding:3px 10px;border-radius:6px;font-size:0.82rem;font-weight:700">OK</span>',
    "INFO":    '<span style="background:rgba(59,139,255,0.12);color:#7cb4ff;border:1px solid rgba(59,139,255,0.2);padding:3px 10px;border-radius:6px;font-size:0.82rem;font-weight:700">INF</span>',
}
_CSS_MAP = {"ERROR": "log-error", "WARNING": "log-warning", "SUCCESS": "log-success", "INFO": "log-info"}

_EVENT_ICONS = {
    "BUY_SUCCESS": "🟢", "BUY_SIMULATED": "🟡", "BUY_FAILED": "❌",
    "SELL_SUCCESS": "🔴", "SELL_SIMULATED": "🟠", "SELL_FAILED": "⚠️",
    "SELL_TP1": "💚", "SELL_TP2": "💰", "SELL_TP3": "🚀",
    "SELL_STOP_LOSS": "🛑", "SELL_TRAILING_STOP": "📉", "SELL_TIME_EXIT": "⏰",
    "SELL_MANUAL": "🖱️", "BOT_START": "▶️", "BOT_STOP": "⏹️",
    "POSITION_ADDED": "📌", "POSITION_CLOSED": "✅",
    "BUY": "🟢", "SELL": "🔴", "REJECT": "⛔",
}


def render():
    # ── Tab selector: Events vs Bot Logs ──────────────────────────────────────
    view = st.radio(
        "View", ["⚡ Live Events", "📝 Bot Logs"],
        horizontal=True, key="logs_view", label_visibility="collapsed",
    )

    if view == "⚡ Live Events":
        _render_events()
    else:
        _render_bot_logs()


def _render_events():
    st.markdown('<div class="section-header">⚡ Live Event Feed</div>', unsafe_allow_html=True)

    # Filters
    fc1, fc2, fc3 = st.columns([1.5, 1, 0.5])
    with fc1: ev_search = st.text_input("🔍 Filter", placeholder="Symbol, type, or message...", key="ev_search")
    with fc2: ev_type   = st.selectbox("Type", [
        "All", "BUY", "SELL", "REJECT", "TP", "STOP_LOSS", "TRAILING", "TIME_EXIT",
        "FAILED", "ERROR", "BOT_START", "BOT_STOP",
    ], key="ev_type")
    with fc3: ev_limit  = st.selectbox("Rows", [25, 50, 100, 200], index=1, key="ev_limit")

    @st.fragment(run_every="8s")
    def _events_feed():
        df_events = get_recent_events(limit=ev_limit)

        if df_events.empty:
            st.info("No events recorded yet.")
            return

        # Quick stats
        sc1, sc2, sc3, sc4 = st.columns(4)
        buys  = len(df_events[df_events["event_type"].str.contains("BUY", na=False)])
        sells = len(df_events[df_events["event_type"].str.contains("SELL", na=False)])
        rejects = len(df_events[df_events["event_type"].str.contains("REJECT", na=False)])
        errors  = len(df_events[df_events["event_type"].str.contains("FAIL|ERROR", na=False)])
        sc1.markdown(kpi_card("Buys", f'<span class="profit">{buys}</span>', ""), unsafe_allow_html=True)
        sc2.markdown(kpi_card("Sells", f'<span style="color:#ffb400">{sells}</span>', ""), unsafe_allow_html=True)
        sc3.markdown(kpi_card("Rejects", str(rejects), ""), unsafe_allow_html=True)
        sc4.markdown(kpi_card("Errors", f'<span class="loss">{errors}</span>', ""), unsafe_allow_html=True)

        st.markdown("")

        for _, ev in df_events.iterrows():
            et   = str(ev.get("event_type", ""))
            sym  = str(ev.get("symbol", "") or "?")
            icon = _EVENT_ICONS.get(et, "🔵")
            ts   = str(ev.get("timestamp", "") or "")[:16]
            msg  = str(ev.get("message", "") or "")[:80]
            addr = str(ev.get("address", "") or "")

            # Apply filters
            if ev_search:
                search_lower = ev_search.lower()
                if (search_lower not in sym.lower()
                    and search_lower not in et.lower()
                    and search_lower not in msg.lower()):
                    continue
            if ev_type != "All" and ev_type.upper() not in et.upper():
                continue

            # P/L badge
            pnl_html = ""
            pnl_usd = ev.get("pnl_usd")
            pnl_pct = ev.get("pnl_pct")
            try:
                if pnl_usd is not None and not (isinstance(pnl_usd, float) and (pnl_usd != pnl_usd)):
                    pnl_val = float(pnl_usd)
                    clr = "#00e6a7" if pnl_val >= 0 else "#ff5c5c"
                    sg  = "+" if pnl_val >= 0 else ""
                    pp = ""
                    if pnl_pct is not None and not (isinstance(pnl_pct, float) and (pnl_pct != pnl_pct)):
                        pp = f" ({sg}{float(pnl_pct)*100:.1f}%)"
                    pnl_html = f'<span style="color:{clr};font-weight:700;font-size:0.82rem">{sg}{pnl_val:.4f}${pp}</span>'
            except (ValueError, TypeError):
                pass

            # TX link
            tx_html = ""
            tx = str(ev.get("tx_signature", "") or "")
            if tx and len(tx) > 10:
                tx_html = f'<a href="https://solscan.io/tx/{tx}" target="_blank" style="color:#3b8bff;font-size:0.72rem;text-decoration:none">🔗 tx</a>'

            # Token link
            tok_html = ""
            if addr and len(addr) > 10:
                tok_html = f'<a href="https://dexscreener.com/solana/{addr}" target="_blank" style="color:#3b8bff;font-size:0.82rem;text-decoration:none">📊</a>'

            st.markdown(
                f'<div class="event-row">'
                f'<span class="event-icon">{icon}</span>'
                f'<span class="event-time">{ts}</span>'
                f'<span class="event-sym">{sym} {tok_html}</span>'
                f'<span class="event-type">{et}</span>'
                f'{pnl_html} {tx_html}'
                f'<span class="event-msg">{msg}</span>'
                f'</div>',
                unsafe_allow_html=True,
            )

    _events_feed()


def _render_bot_logs():
    st.markdown('<div class="section-header">📝 Bot Logs</div>', unsafe_allow_html=True)

    c1, c2, c3 = st.columns([1, 1, 2])
    with c1: level  = st.selectbox("Level", ["ALL", "ERROR", "WARNING", "SUCCESS", "INFO"], key="log_level")
    with c2: limit  = st.selectbox("Entries", [30, 50, 100, 200, 500], index=1, key="log_limit")
    with c3: search = st.text_input("🔍 Search", placeholder="Filter by keyword...", key="log_search")

    where, params = [], []
    if level != "ALL":
        where.append("level = ?")
        params.append(level)
    if search:
        where.append("message LIKE ?")
        params.append(f"%{search}%")

    where_sql = ("WHERE " + " AND ".join(where)) if where else ""
    df = db_query(
        f"SELECT level, message, timestamp FROM bot_logs {where_sql} ORDER BY timestamp DESC LIMIT ?",
        tuple(params + [limit]),
    )

    if df.empty:
        st.info("No logs found.")
        return

    # Stats row
    sc1, sc2, sc3, sc4 = st.columns(4)
    sc1.markdown(kpi_card("Shown", str(len(df)), "entries"), unsafe_allow_html=True)
    err_count = int((df["level"] == "ERROR").sum())
    wrn_count = int((df["level"] == "WARNING").sum())
    ok_count  = int((df["level"] == "SUCCESS").sum())
    err_html = f'<span class="loss">{err_count}</span>' if err_count > 0 else "0"
    sc2.markdown(kpi_card("Errors", err_html, ""), unsafe_allow_html=True)
    wrn_html = f'<span style="color:#ffb400">{wrn_count}</span>' if wrn_count > 0 else "0"
    sc3.markdown(kpi_card("Warnings", wrn_html, ""), unsafe_allow_html=True)
    sc4.markdown(kpi_card("Success", f'<span class="profit">{ok_count}</span>', ""), unsafe_allow_html=True)

    st.markdown("")

    # Log terminal — white background with readable text
    _LOG_TEXT_COLOR = {
        "ERROR":   "#dc2626",
        "WARNING": "#b45309",
        "SUCCESS": "#15803d",
        "INFO":    "#334155",
    }
    st.markdown(
        '<div style="background:#f8fafc;border:1px solid #e2e8f0;border-radius:10px;'
        'padding:16px 18px;max-height:620px;overflow-y:auto;'
        'font-family:JetBrains Mono,monospace;font-size:0.8rem">',
        unsafe_allow_html=True,
    )
    for _, row in df.iterrows():
        lvl   = row["level"]
        msg   = str(row["message"]).replace("<", "&lt;").replace(">", "&gt;")
        ts    = str(row["timestamp"])[-8:]
        badge = _LOG_BADGE.get(lvl, "")
        txt_color = _LOG_TEXT_COLOR.get(lvl, "#334155")
        bg_color = {
            "ERROR":   "rgba(220,38,38,0.06)",
            "WARNING": "rgba(180,83,9,0.06)",
            "SUCCESS": "rgba(21,128,61,0.04)",
            "INFO":    "transparent",
        }.get(lvl, "transparent")
        st.markdown(
            f'<div style="margin-bottom:3px;line-height:1.6;padding:4px 8px;'
            f'border-bottom:1px solid #e2e8f0;border-radius:4px;background:{bg_color}">'
            f'<span style="color:#5eead4;margin-right:10px">{ts}</span>'
            f'{badge} <span style="color:{txt_color};font-weight:600;margin-left:8px">{msg}</span>'
            f'</div>',
            unsafe_allow_html=True,
        )
    st.markdown('</div>', unsafe_allow_html=True)
