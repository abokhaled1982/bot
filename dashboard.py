"""
Hyperliquid Copy-Trader Dashboard — entry point.
All tab logic lives in dashboard/tabs/*.py
"""
import streamlit as st

from dashboard.config import DASHBOARD_CSS
from dashboard.tabs import render_copytrader

st.set_page_config(
	page_title="Hyperliquid Copy-Trader",
	layout="wide",
	page_icon="🔄",
	initial_sidebar_state="collapsed",
)
st.markdown(DASHBOARD_CSS, unsafe_allow_html=True)

render_copytrader()
