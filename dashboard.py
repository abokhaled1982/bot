"""
Binance Copy-Trader Dashboard — entry point.
All tab logic lives in dashboard/tabs/*.py
"""
import streamlit as st
from dotenv import load_dotenv

# Muss vor den Dashboard-Imports laufen: die Tab-Module lesen DRY_RUN,
# BOT_DB_PATH und die Standardbeträge beim Import aus der Umgebung. Ohne das
# arbeitet das Dashboard mit anderen Werten als der Bot-Prozess.
load_dotenv()

from dashboard.config import DASHBOARD_CSS  # noqa: E402
from dashboard.tabs import render_copytrader  # noqa: E402

st.set_page_config(
	page_title="Binance Copy-Trader",
	layout="wide",
	page_icon="🔄",
	initial_sidebar_state="collapsed",
)
st.markdown(DASHBOARD_CSS, unsafe_allow_html=True)

render_copytrader()
