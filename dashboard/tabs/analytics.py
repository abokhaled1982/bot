import streamlit as st
import pandas as pd
from dashboard.db import db_query

def render():
    st.markdown('<div class="section-header">📊 Performance Analytics</div>', unsafe_allow_html=True)
    df = db_query("SELECT timestamp, action, combined_score, quantity, price FROM trades")
    
    if df.empty:
        st.info("Not enough data to run analytics.")
        return
        
    st.line_chart(df.set_index("timestamp")["combined_score"], height=300)
    st.bar_chart(df.groupby("action").size(), height=300)
