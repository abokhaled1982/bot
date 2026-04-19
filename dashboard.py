"""
Memecoin Bot Dashboard — entry point.
All tab logic lives in dashboard/tabs/*.py
All data fetching lives in dashboard/db.py
"""
import os
import streamlit as st

from dashboard.config import (
    DASHBOARD_CSS, WALLET_ADDRESS, STOP_LOSS_PCT, TRAILING_STOP_PCT,
    TRAILING_ACTIVATE, TP1_PCT, TP2_PCT, TP3_PCT, MAX_HOLD_HOURS, POSITION_SIZE_USD,
)
from dashboard.db import (
    get_reconciled_positions, get_wallet_sol_balance,
    get_sol_price_and_change, get_btc_price_and_change,
)
from dashboard.components import fmt_usd, fmt_pct
from dashboard.tabs import (
    render_positions, render_history,
    render_trade, render_analytics, render_logs,
)

# ── Page config ───────────────────────────────────────────────────────────────
st.set_page_config(
    page_title="Memecoin Terminal",
    layout="wide",
    page_icon="logo.png" if os.path.exists("logo.png") else None,
    initial_sidebar_state="expanded",
)
st.markdown(DASHBOARD_CSS, unsafe_allow_html=True)

# ── Load shared data ─────────────────────────────────────────────────────────
positions              = get_reconciled_positions(WALLET_ADDRESS)
sol_bal                = get_wallet_sol_balance(WALLET_ADDRESS)
sol_price, sol_change  = get_sol_price_and_change()
sol_usd                = sol_bal * sol_price

# ── Sidebar (minimal — no duplication with tabs) ──────────────────────────────
with st.sidebar:
    st.markdown(
        '<div style="text-align:center;padding:8px 0 4px">'
        '<span style="font-size:1.1rem;font-weight:800;color:#f1f5f9;letter-spacing:1px">MEMECOIN</span>'
        '<span style="font-size:1.1rem;font-weight:800;color:#00e6a7;letter-spacing:1px"> TERMINAL</span>'
        '</div>',
        unsafe_allow_html=True,
    )
    from datetime import datetime
    st.caption(datetime.now().strftime("%Y-%m-%d  %H:%M:%S"))

    st.markdown("---")

    # Bot status
    bot_stopped = os.path.exists("STOP_BOT")
    if bot_stopped:
        st.markdown(
            '<span class="status-dot status-stopped"></span>'
            '<span style="color:#ff5c5c;font-weight:700;font-size:0.85rem">Bot Offline</span>',
            unsafe_allow_html=True,
        )
        if st.button("▶ Start Bot", use_container_width=True, type="primary"):
            os.remove("STOP_BOT")
            st.rerun()
    else:
        st.markdown(
            '<span class="status-dot status-running"></span>'
            '<span style="color:#00e6a7;font-weight:700;font-size:0.85rem">Bot Running</span>',
            unsafe_allow_html=True,
        )
        if st.button("⏹ Stop Bot", use_container_width=True):
            open("STOP_BOT", "w").write("STOP")
            st.rerun()

    st.markdown("---")

    # Wallet summary
    st.markdown(
        f'<div style="padding:10px 0">'
        f'<div style="color:#475569;font-size:0.68rem;font-weight:700;text-transform:uppercase;letter-spacing:0.5px">💰 Wallet</div>'
        f'<div style="color:#f1f5f9;font-size:1.1rem;font-weight:800;margin-top:4px">{sol_bal:.4f} SOL</div>'
        f'<div style="color:#64748b;font-size:0.8rem">{fmt_usd(sol_usd)}</div>'
        f'</div>',
        unsafe_allow_html=True,
    )
    st.markdown(
        f'<div style="padding:6px 0">'
        f'<div style="color:#475569;font-size:0.68rem;font-weight:700;text-transform:uppercase;letter-spacing:0.5px">📊 Positions</div>'
        f'<div style="color:#f1f5f9;font-size:1.1rem;font-weight:800;margin-top:4px">{len(positions)}</div>'
        f'</div>',
        unsafe_allow_html=True,
    )

    st.markdown("---")

    with st.expander("⚙️ Strategy Config", expanded=False):
        st.markdown(
            f'<div style="font-size:0.78rem;line-height:2;color:#94a3b8">'
            f'🛑 Stop-Loss: <span style="color:#ff5c5c;font-weight:600">-{int(STOP_LOSS_PCT*100)}%</span><br>'
            f'📉 Trailing: <span style="color:#a78bfa;font-weight:600">-{int(TRAILING_STOP_PCT*100)}%</span>'
            f' (at +{int(TRAILING_ACTIVATE*100)}%)<br>'
            f'💚 TP1: <span style="color:#00e6a7;font-weight:600">+{int(TP1_PCT*100)}%</span> · '
            f'TP2: <span style="color:#00e6a7;font-weight:600">+{int(TP2_PCT*100)}%</span> · '
            f'TP3: <span style="color:#00e6a7;font-weight:600">+{int(TP3_PCT*100)}%</span><br>'
            f'⏱ Max Hold: {MAX_HOLD_HOURS}h<br>'
            f'💵 Size: {fmt_usd(POSITION_SIZE_USD)}'
            f'</div>',
            unsafe_allow_html=True,
        )

    st.markdown("")
    if st.button("🔄 Refresh", use_container_width=True):
        st.cache_data.clear()
        st.rerun()

    st.markdown(
        f'<a href="https://solscan.io/account/{WALLET_ADDRESS}" target="_blank" '
        f'style="color:#3b8bff;font-size:0.78rem;text-decoration:none">🔗 Solscan Wallet</a>',
        unsafe_allow_html=True,
    )

# ── Tabs ──────────────────────────────────────────────────────────────────────
tab_pos, tab_hist, tab_analytics, tab_trade, tab_logs = st.tabs([
    "📌 Positions",
    "📋 History",
    "📊 Analytics",
    "🔄 Trade",
    "📝 Logs",
])

with tab_pos:       render_positions()
with tab_hist:      render_history()
with tab_analytics: render_analytics()
with tab_trade:     render_trade()
with tab_logs:      render_logs()
