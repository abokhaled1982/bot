"""Stock Bot Dashboard — entry point."""
import os
import streamlit as st
from dashboard.config import DASHBOARD_CSS
from dashboard.db import get_reconciled_positions, get_wallet_balance, get_wallet_free_cash
from dashboard.components import fmt_usd
from dashboard.tabs import (
    render_discovery, render_positions, render_history,
    render_trade, render_analytics, render_logs,
)

st.set_page_config(page_title="AlphaEngine Terminal", layout="wide", initial_sidebar_state="expanded")
st.markdown(DASHBOARD_CSS, unsafe_allow_html=True)

positions = get_reconciled_positions()
total_bal = get_wallet_balance()
free_cash = get_wallet_free_cash()

with st.sidebar:
    st.markdown(
        '<div style="text-align:center;padding:8px 0 4px">'
        '<span style="font-size:1.1rem;font-weight:800;color:#f1f5f9;letter-spacing:1px">ALPHA</span>'
        '<span style="font-size:1.1rem;font-weight:800;color:#00e6a7;letter-spacing:1px"> ENGINE</span>'
        '</div>', unsafe_allow_html=True)
    from datetime import datetime
    st.caption(datetime.now().strftime("%Y-%m-%d  %H:%M:%S"))
    st.markdown("---")
    bot_stopped = os.path.exists("STOP_BOT")
    if bot_stopped:
        st.markdown('<span class="status-dot status-stopped"></span>'
            '<span style="color:#ff5c5c;font-weight:700;font-size:0.85rem">Bot Offline</span>', unsafe_allow_html=True)
        if st.button("▶ Start Bot", use_container_width=True, type="primary"):
            os.remove("STOP_BOT"); st.rerun()
    else:
        st.markdown('<span class="status-dot status-running"></span>'
            '<span style="color:#00e6a7;font-weight:700;font-size:0.85rem">Bot Running</span>', unsafe_allow_html=True)
        if st.button("⏹ Stop Bot", use_container_width=True):
            open("STOP_BOT","w").write("STOP"); st.rerun()
    st.markdown("---")
    st.markdown(
        f'<div style="padding:10px 0">'
        f'<div style="color:#475569;font-size:0.68rem;font-weight:700;text-transform:uppercase;letter-spacing:0.5px">💰 Portfolio</div>'
        f'<div style="color:#f1f5f9;font-size:1.1rem;font-weight:800;margin-top:4px">{fmt_usd(total_bal)}</div>'
        f'<div style="color:#64748b;font-size:0.8rem">Free: {fmt_usd(free_cash)}</div>'
        f'</div>', unsafe_allow_html=True)
    st.markdown(
        f'<div style="padding:6px 0">'
        f'<div style="color:#475569;font-size:0.68rem;font-weight:700;text-transform:uppercase;letter-spacing:0.5px">📊 Positions</div>'
        f'<div style="color:#f1f5f9;font-size:1.1rem;font-weight:800;margin-top:4px">{len(positions)}</div>'
        f'</div>', unsafe_allow_html=True)
    st.markdown("---")
    if st.button("🔄 Refresh", use_container_width=True):
        st.cache_data.clear(); st.rerun()

tab_disc, tab_pos, tab_hist, tab_analytics, tab_trade, tab_logs = st.tabs([
    "🔍 Discovery", "📌 Positions", "📋 History", "📊 Analytics", "🔄 Trade", "📝 Logs"
])
with tab_disc:      render_discovery()
with tab_pos:       render_positions()
with tab_hist:      render_history()
with tab_analytics: render_analytics()
with tab_trade:     render_trade()
with tab_logs:      render_logs()
