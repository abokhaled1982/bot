import streamlit as st
import pandas as pd
from dashboard.db import db_query

def render():
    st.markdown('<div class="section-header">📝 System Logs</div>', unsafe_allow_html=True)
    df = db_query("SELECT timestamp, level, message FROM bot_logs ORDER BY id DESC LIMIT 100")
    
    if df.empty:
        st.info("No logs found.")
        return
        
    st.dataframe(df, hide_index=True, use_container_width=True)
