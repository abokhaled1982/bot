"""
Binance Trading Terminal — entry point.
All tab logic lives in dashboard/tabs/*.py
All data fetching lives in dashboard/db.py
"""
import streamlit as st

from dashboard.config import DASHBOARD_CSS
from dashboard.tabs import render_live_market

render_live_market()
