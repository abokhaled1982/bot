"""
Binance Trading Terminal — entry point.
All tab logic lives in dashboard/tabs/*.py
All data fetching lives in dashboard/db.py
"""
import streamlit as st

from dashboard.config import DASHBOARD_CSS
from dashboard.tabs import render_live_market

st.set_page_config(
	page_title="Binance Orderflow Simulator",
	layout="wide",
	page_icon="B",
	initial_sidebar_state="collapsed",
)
st.markdown(DASHBOARD_CSS, unsafe_allow_html=True)

render_live_market()
