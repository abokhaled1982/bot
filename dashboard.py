import streamlit as st
import pandas as pd
import sqlite3
import requests

st.set_page_config(page_title="ProTrading Control Center", layout="wide")

st.title("🛡️ ProTrading Control Center")

def get_db_connection():
    return sqlite3.connect('memecoin_bot.db')

def get_data(query, params=()):
    conn = get_db_connection()
    df = pd.read_sql_query(query, conn, params=params)
    conn.close()
    return df

# Professional DataGrid-like UI
st.subheader("📋 Trade Intelligence Ledger")
# Get unique tokens by taking the latest status of each address
query = """
SELECT token_address, symbol, decision, rejection_reason, ai_reasoning, funnel_stage, MAX(timestamp) as last_seen
FROM trades 
GROUP BY token_address 
ORDER BY last_seen DESC
"""
df = get_data(query)

# Interactive Filtering
st.sidebar.header("Filter & Analysis")
search = st.sidebar.text_input("🔍 Search Symbol or Address")
if search:
    df = df[df['symbol'].str.contains(search, case=False, na=False) | 
            df['token_address'].str.contains(search, case=False, na=False)]

# Professional Table View
st.data_editor(
    df,
    column_config={
        "token_address": st.column_config.TextColumn("Address", width="medium"),
        "symbol": st.column_config.TextColumn("Symbol", width="small"),
        "decision": st.column_config.TextColumn("Decision", width="small"),
        "ai_reasoning": st.column_config.TextColumn("Intelligence Summary", width="large"),
    },
    hide_index=True,
    use_container_width=True
)

st.divider()

# Controls
col1, col2 = st.columns(2)
with col1:
    st.subheader("🎯 Manual Override")
    target_addr = st.text_input("Token Address")
    target_sym = st.text_input("Symbol")
    if st.button("Trigger Analysis"):
        resp = requests.post("http://localhost:8000/api/trade/manual", json={"token_address": target_addr, "symbol": target_sym})
        st.success("Triggered!")

with col2:
    st.subheader("⚠️ System Controls")
    if st.button("🛑 EMERGENCY STOP"):
        with open("STOP_BOT", "w") as f: f.write("STOP")
        st.error("Stop signal sent!")

st.subheader("📜 System Logs")
logs = get_data("SELECT level, message, timestamp FROM bot_logs ORDER BY timestamp DESC LIMIT 10")
st.table(logs)
