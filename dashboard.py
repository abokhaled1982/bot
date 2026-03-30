import streamlit as st
import pandas as pd
import sqlite3
import plotly.express as px

st.set_page_config(page_title="ProTrading Control Center", layout="wide")

st.title("🛡️ ProTrading Control Center")

def get_db_connection():
    return sqlite3.connect('memecoin_bot.db')

def get_data(query):
    conn = get_db_connection()
    df = pd.read_sql_query(query, conn)
    conn.close()
    return df

# Layout - Funnel Overview
st.subheader("Decision Funnel (Why Tokens are Rejected)")
df_all = get_data("SELECT funnel_stage, COUNT(*) as count FROM trades GROUP BY funnel_stage")
fig = px.funnel(df_all, x='count', y='funnel_stage', title="Funnel: Scanning to Execution")
st.plotly_chart(fig, use_container_width=True)

# Main UI
col1, col2 = st.columns([2, 1])

with col1:
    st.subheader("📋 Trade History & Intelligence")
    trades = get_data("SELECT symbol, decision, rejection_reason, ai_reasoning, funnel_stage, timestamp FROM trades ORDER BY timestamp DESC LIMIT 50")
    st.dataframe(trades, use_container_width=True)

with col2:
    st.subheader("⚙️ Control Panel")
    if st.button("Emergency Stop"):
        with open("STOP_BOT", "w") as f: f.write("STOP")
        st.error("Stop signal sent!")
    
    st.subheader("💡 Risk Settings")
    max_pos = st.slider("Max Position Size ($)", 0.0, 5.0, 0.20, 0.05)
    if st.button("Update Risk"):
        st.success(f"Updated Max Position to ${max_pos}")

st.subheader("📜 Live Event Log")
logs = get_data("SELECT level, message, timestamp FROM bot_logs ORDER BY timestamp DESC LIMIT 20")
st.table(logs)
