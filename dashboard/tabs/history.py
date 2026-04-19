import streamlit as st
from dashboard.db import db_query

def render():
    st.markdown('<div class="section-header">📋 Trade History</div>', unsafe_allow_html=True)
    df = db_query(
        "SELECT id, timestamp, ticker, action, combined_score, ta_score, sent_score, "
        "velocity_score, llm_conviction, quantity, price, status, reason, mention_count, gates_passed "
        "FROM trades ORDER BY id DESC")
    if df.empty:
        st.info("No trades found in database.")
        return
    st.dataframe(df, hide_index=True, use_container_width=True,
        column_config={
            "id":"ID","timestamp":"Time","ticker":"Ticker","action":"Action",
            "combined_score":st.column_config.NumberColumn("Fusion",format="%.3f"),
            "ta_score":st.column_config.NumberColumn("TA",format="%.2f"),
            "sent_score":st.column_config.NumberColumn("Sent",format="%.2f"),
            "velocity_score":st.column_config.NumberColumn("Vel",format="%.3f"),
            "llm_conviction":st.column_config.NumberColumn("Conv",format="%.2f"),
            "quantity":"Qty",
            "price":st.column_config.NumberColumn("Price",format="$%.2f"),
            "status":"Status","reason":"Reason","mention_count":"Mentions",
            "gates_passed":"Gates"})
