"""
Database queries and API fetching for the stock bot dashboard.
"""
import sqlite3, base64, os
import pandas as pd
import streamlit as st
import requests
from dashboard.config import DB_PATH, T212_KEY, T212_MODE

def db_query(sql: str, params: tuple = ()) -> pd.DataFrame:
    try:
        conn = sqlite3.connect(DB_PATH)
        df = pd.read_sql_query(sql, conn, params=params)
        conn.close()
        return df
    except Exception:
        return pd.DataFrame()

# ── T212 Client ──────────────────────────────────────────────
class DashboardT212Client:
    URLS = {"demo":"https://demo.trading212.com/api/v0","live":"https://live.trading212.com/api/v0"}
    def __init__(self):
        self.base = self.URLS.get(T212_MODE, self.URLS["demo"])
        auth = "Basic "+base64.b64encode(f"{T212_KEY}:".encode()).decode() if T212_KEY else ""
        self._h = {"Authorization": auth, "Content-Type": "application/json"}
    def get_cash(self):
        try:
            r=requests.get(self.base+"/equity/account/cash",headers=self._h,timeout=5)
            if r.status_code==200: return r.json()
        except: pass
        return {}
    def get_portfolio(self):
        try:
            r=requests.get(self.base+"/equity/portfolio",headers=self._h,timeout=5)
            if r.status_code==200:
                d=r.json(); return d if isinstance(d,list) else d.get("items",[])
        except: pass
        return []

@st.cache_data(ttl=30)
def get_wallet_balance() -> float:
    c=DashboardT212Client(); cash=c.get_cash()
    return float(cash.get("total",cash.get("freeForInvest",0)))

@st.cache_data(ttl=30)
def get_wallet_free_cash() -> float:
    c=DashboardT212Client(); cash=c.get_cash()
    return float(cash.get("free",cash.get("freeForInvest",0)))

@st.cache_data(ttl=30)
def get_reconciled_positions() -> dict:
    c=DashboardT212Client(); portfolio=c.get_portfolio()
    positions={}
    for p in portfolio:
        ticker=p.get("ticker","").split("_")[0]
        positions[ticker]={
            "symbol":ticker,"entry_price":float(p.get("averagePrice",0)),
            "current_price":float(p.get("currentPrice",0)),
            "quantity":float(p.get("quantity",0)),
            "pnl":float(p.get("ppl",0)),"wallet_confirmed":True}
    return positions

# ── Discovery Data ───────────────────────────────────────────
@st.cache_data(ttl=10)
def get_news_signals(limit:int=100) -> pd.DataFrame:
    return db_query(
        "SELECT ticker, sentiment, urgency, headline, source, extracted_at "
        "FROM news_signals ORDER BY id DESC LIMIT ?", (limit,))

@st.cache_data(ttl=10)
def get_candidates(limit:int=50) -> pd.DataFrame:
    return db_query(
        "SELECT ticker, mention_count, velocity_score, avg_sentiment, ta_score, "
        "fusion_score, llm_conviction, decision, gates_passed, rejection_reason, cycle, timestamp "
        "FROM candidates ORDER BY id DESC LIMIT ?", (limit,))

@st.cache_data(ttl=10)
def get_velocity_data() -> pd.DataFrame:
    return db_query(
        "SELECT ticker, COUNT(*) as mentions, AVG(sentiment) as avg_sent, "
        "MAX(urgency) as max_urgency, MIN(extracted_at) as first_seen "
        "FROM news_signals WHERE extracted_at >= datetime('now', '-2 hours') "
        "GROUP BY ticker ORDER BY mentions DESC LIMIT 20")

@st.cache_data(ttl=10)
def get_signal_funnel() -> dict:
    headlines=db_query("SELECT COUNT(*) as c FROM news_signals")
    candidates_df=db_query("SELECT COUNT(*) as c FROM candidates")
    passed=db_query("SELECT COUNT(*) as c FROM candidates WHERE decision != 'HOLD'")
    traded=db_query("SELECT COUNT(*) as c FROM trades WHERE status='EXECUTED'")
    return {
        "Headlines Scanned": int(headlines.iloc[0]["c"]) if not headlines.empty else 0,
        "Tickers Extracted": int(candidates_df.iloc[0]["c"]) if not candidates_df.empty else 0,
        "Signals Passed": int(passed.iloc[0]["c"]) if not passed.empty else 0,
        "Trades Executed": int(traded.iloc[0]["c"]) if not traded.empty else 0,
    }

@st.cache_data(ttl=15)
def get_recent_events(limit:int=25) -> pd.DataFrame:
    return db_query(
        "SELECT action as event_type, ticker as symbol, t212_ticker as address, "
        "quantity as buy_amount_usd, price as price_usd, velocity_score, "
        "funnel_stage as stage, reason as message, timestamp "
        "FROM trades ORDER BY id DESC LIMIT ?", (limit,))
