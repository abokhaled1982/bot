"""
Binance Trading Terminal — entry point.
All tab logic lives in dashboard/tabs/*.py
All data fetching lives in dashboard/db.py
"""
import os
import streamlit as st
import requests

from dashboard.config import (
    DASHBOARD_CSS, STOP_LOSS_PCT, TRAILING_STOP_PCT,
    TRAILING_ACTIVATE, TP1_PCT, TP2_PCT, TP3_PCT, MAX_HOLD_HOURS,
)
from dashboard.components import fmt_usd
from dashboard.tabs import (
    render_positions, render_history,
    render_trade, render_analytics, render_logs,
    render_live_market,
)

# ── Page config ───────────────────────────────────────────────────────────────
st.set_page_config(
    page_title="Binance Trading Terminal",
    layout="wide",
    page_icon="📈",
    initial_sidebar_state="expanded",
)
st.markdown(DASHBOARD_CSS, unsafe_allow_html=True)

# ── USDT Balance (from .env or Binance API later) ─────────────────────────────
POSITION_SIZE_USDT = float(os.getenv("BINANCE_POSITION_SIZE_USDT", "10.0"))
MAX_POSITIONS      = int(os.getenv("BINANCE_MAX_POSITIONS", "10"))
DRY_RUN            = os.getenv("DRY_RUN", "True").lower() == "true"

# Fetch BTC price for market context
@st.cache_data(ttl=30)
def _get_btc_price():
    try:
        r = requests.get(
            "https://api.binance.com/api/v3/ticker/24hr?symbol=BTCUSDT",
            timeout=5,
        )
        d = r.json()
        return float(d["lastPrice"]), float(d["priceChangePercent"])
    except Exception:
        return 0.0, 0.0

btc_price, btc_change = _get_btc_price()

# ── Sidebar ───────────────────────────────────────────────────────────────────
with st.sidebar:
    # Logo / Title
    st.markdown(
        '<div style="text-align:center;padding:10px 0 4px">'
        '<span style="font-size:1.2rem;font-weight:800;color:#f1f5f9;letter-spacing:1px">BINANCE</span>'
        '<span style="font-size:1.2rem;font-weight:800;color:#00e6a7;letter-spacing:1px"> TERMINAL</span>'
        '<div style="color:#475569;font-size:0.68rem;letter-spacing:2px;margin-top:2px">CRYPTO TRADING BOT</div>'
        '</div>',
        unsafe_allow_html=True,
    )

    from datetime import datetime
    st.caption(datetime.now().strftime("%Y-%m-%d  %H:%M:%S"))

    st.markdown("---")

    # ── Bot status ────────────────────────────────────────────────────────────
    bot_stopped = os.path.exists("STOP_BOT")
    mode_badge  = (
        '<span style="background:#1e3a2e;color:#00e6a7;border:1px solid #00e6a733;'
        'padding:1px 7px;border-radius:4px;font-size:0.7rem;font-weight:700">PAPER</span>'
        if DRY_RUN else
        '<span style="background:#3a1e1e;color:#ff5c5c;border:1px solid #ff5c5c33;'
        'padding:1px 7px;border-radius:4px;font-size:0.7rem;font-weight:700">LIVE</span>'
    )

    if bot_stopped:
        st.markdown(
            f'<span class="status-dot status-stopped"></span>'
            f'<span style="color:#ff5c5c;font-weight:700;font-size:0.85rem">Bot Offline</span> {mode_badge}',
            unsafe_allow_html=True,
        )
        if st.button("▶ Start Bot", use_container_width=True, type="primary"):
            os.remove("STOP_BOT")
            st.rerun()
    else:
        st.markdown(
            f'<span class="status-dot status-running"></span>'
            f'<span style="color:#00e6a7;font-weight:700;font-size:0.85rem">Bot Running</span> {mode_badge}',
            unsafe_allow_html=True,
        )
        if st.button("⏹ Stop Bot", use_container_width=True):
            open("STOP_BOT", "w").write("STOP")
            st.rerun()

    st.markdown("---")

    # ── Account summary ───────────────────────────────────────────────────────
    btc_col = "#00e6a7" if btc_change >= 0 else "#ff5c5c"
    st.markdown(
        f'<div style="padding:8px 0">'
        f'<div style="color:#475569;font-size:0.68rem;font-weight:700;text-transform:uppercase;letter-spacing:0.5px">₿ Bitcoin</div>'
        f'<div style="color:#f1f5f9;font-size:1.1rem;font-weight:800;margin-top:3px">${btc_price:,.0f}</div>'
        f'<div style="color:{btc_col};font-size:0.8rem;font-weight:600">{btc_change:+.2f}% (24h)</div>'
        f'</div>',
        unsafe_allow_html=True,
    )

    st.markdown(
        f'<div style="padding:6px 0">'
        f'<div style="color:#475569;font-size:0.68rem;font-weight:700;text-transform:uppercase;letter-spacing:0.5px">💵 Trade Size</div>'
        f'<div style="color:#f1f5f9;font-size:1.1rem;font-weight:800;margin-top:3px">${POSITION_SIZE_USDT:.2f} USDT</div>'
        f'<div style="color:#64748b;font-size:0.78rem">max {MAX_POSITIONS} positions</div>'
        f'</div>',
        unsafe_allow_html=True,
    )

    st.markdown("---")

    # ── Strategy Config ───────────────────────────────────────────────────────
    with st.expander("⚙️ Strategy Config", expanded=False):
        st.markdown(
            f'<div style="font-size:0.78rem;line-height:2;color:#94a3b8">'
            f'🛑 Stop-Loss: <span style="color:#ff5c5c;font-weight:600">-{STOP_LOSS_PCT*100:.0f}%</span><br>'
            f'📉 Trailing: <span style="color:#a78bfa;font-weight:600">-{TRAILING_STOP_PCT*100:.0f}%</span>'
            f' (at +{TRAILING_ACTIVATE*100:.0f}%)<br>'
            f'💚 TP1: <span style="color:#00e6a7;font-weight:600">+{TP1_PCT*100:.0f}%</span> · '
            f'TP2: <span style="color:#00e6a7;font-weight:600">+{TP2_PCT*100:.0f}%</span> · '
            f'TP3: <span style="color:#00e6a7;font-weight:600">+{TP3_PCT*100:.0f}%</span><br>'
            f'⏱ Max Hold: {MAX_HOLD_HOURS}h<br>'
            f'💵 Size: ${POSITION_SIZE_USDT} USDT'
            f'</div>',
            unsafe_allow_html=True,
        )

    st.markdown("")
    if st.button("🔄 Refresh", use_container_width=True):
        st.cache_data.clear()
        st.rerun()

    st.markdown(
        '<a href="https://www.binance.com/en/trade" target="_blank" '
        'style="color:#3b8bff;font-size:0.78rem;text-decoration:none">🔗 Binance Exchange</a>',
        unsafe_allow_html=True,
    )

# ── Tabs ──────────────────────────────────────────────────────────────────────
tab_live, tab_pos, tab_hist, tab_analytics, tab_trade, tab_logs = st.tabs([
    "📡 Live Market",
    "📌 Positions",
    "📋 History",
    "📊 Analytics",
    "🔄 Trade",
    "📝 Logs",
])

with tab_live:      render_live_market()
with tab_pos:       render_positions()
with tab_hist:      render_history()
with tab_analytics: render_analytics()
with tab_trade:     render_trade()
with tab_logs:      render_logs()
