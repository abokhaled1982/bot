"""
All database queries and external API data fetching for the dashboard.
Single source of truth — no DB calls in tab files.
"""
import sqlite3
import json
import os
import time
import requests
import streamlit as st
import pandas as pd
from datetime import datetime

from dashboard.config import DB_PATH, WALLET_ADDRESS


# ── DB ────────────────────────────────────────────────────────────────────────

def db_query(sql: str, params: tuple = ()) -> pd.DataFrame:
    try:
        conn = sqlite3.connect(DB_PATH)
        df   = pd.read_sql_query(sql, conn, params=params)
        conn.close()
        return df
    except Exception as e:
        print(f"DB_QUERY ERROR: {e} | SQL: {sql}")
        return pd.DataFrame()


# ── Price / Market Data ───────────────────────────────────────────────────────

@st.cache_data(ttl=5)
def get_live_price(symbol: str) -> float:
    """Fetch current token price from Binance."""
    try:
        r = requests.get(
            f"https://api.binance.com/api/v3/ticker/price?symbol={symbol}", timeout=5
        )
        if r.status_code == 200:
            return float(r.json().get("price", 0))
    except Exception:
        pass
    return 0.0


@st.cache_data(ttl=15)
def get_token_full_info(symbol: str) -> dict:
    """Full market snapshot for a token from Binance 24h ticker."""
    try:
        r = requests.get(
            f"https://api.binance.com/api/v3/ticker/24hr?symbol={symbol}", timeout=5
        )
        if r.status_code == 200:
            data = r.json()
            return {
                "symbol":     data.get("symbol", "?"),
                "price":      float(data.get("lastPrice", 0)),
                "change_24h": float(data.get("priceChangePercent", 0)),
                "volume_24h": float(data.get("quoteVolume", 0)), # USDT volume
                "high_24h":   float(data.get("highPrice", 0)),
                "low_24h":    float(data.get("lowPrice", 0)),
            }
    except Exception:
        pass
    return {}


@st.cache_data(ttl=60)
def get_sol_price_and_change() -> tuple[float, float]:
    """Returns (sol_price_usd, sol_24h_change_pct). Falls back to (0, 0)."""
    # Primary: CoinGecko
    try:
        r = requests.get(
            "https://api.coingecko.com/api/v3/simple/price"
            "?ids=solana&vs_currencies=usd&include_24hr_change=true",
            timeout=5,
        )
        d = r.json()["solana"]
        return float(d["usd"]), float(d.get("usd_24h_change", 0))
    except Exception:
        pass
    # Fallback: DexScreener SOL/USDC pair
    try:
        r = requests.get(
            "https://api.dexscreener.com/latest/dex/tokens/So11111111111111111111111111111111111111112",
            timeout=5,
        )
        pairs = r.json().get("pairs", [])
        if pairs:
            price = float(pairs[0].get("priceUsd") or 0)
            change = float((pairs[0].get("priceChange") or {}).get("h24", 0))
            return price, change
    except Exception:
        pass
    return 0.0, 0.0


@st.cache_data(ttl=60)
def get_btc_price_and_change() -> tuple[float, float]:
    """Returns (btc_price_usd, btc_24h_change_pct). Falls back to (0, 0)."""
    try:
        r = requests.get(
            "https://api.coingecko.com/api/v3/simple/price"
            "?ids=bitcoin&vs_currencies=usd&include_24hr_change=true",
            timeout=5,
        )
        d = r.json()["bitcoin"]
        return float(d["usd"]), float(d.get("usd_24h_change", 0))
    except Exception:
        pass
    return 0.0, 0.0


# ── Wallet ────────────────────────────────────────────────────────────────────

@st.cache_data(ttl=30)
def get_wallet_sol_balance(wallet: str) -> float:
    # Dummy for backward compatibility
    return 0.0

@st.cache_data(ttl=15)
def get_wallet_tokens(wallet: str) -> list:
    # Dummy for backward compatibility
    return []

def load_positions() -> dict:
    if os.path.exists("positions.json"):
        try:
            with open("positions.json", encoding="utf-8") as f:
                return json.load(f)
        except Exception as e:
            print(f"LOAD POSITIONS ERROR: {e}")
    return {}

@st.cache_data(ttl=15)
def get_reconciled_positions(wallet: str) -> dict:
    """
    Load bot positions directly from positions.json.
    Since we trade on Binance, the bot's positions.json is the source of truth.
    """
    return load_positions()


def load_watchlist() -> dict:
    if os.path.exists("watchlist.json"):
        try:
            with open("watchlist.json") as f:
                return json.load(f)
        except Exception:
            pass
    return {}


@st.cache_data(ttl=8)
def get_recent_events(limit: int = 25) -> pd.DataFrame:
    """
    Query bot_events table for the live event feed.
    Falls back to trades table if bot_events is empty/missing.
    """
    df = db_query(
        "SELECT event_type, symbol, address, tx_signature, "
        "buy_amount_usd, sell_amount_usd, price_usd, pnl_usd, pnl_pct, "
        "stage, message, timestamp "
        "FROM bot_events ORDER BY id DESC LIMIT ?",
        (limit,),
    )
    if not df.empty:
        return df

    # Fallback: synthesise events from trades table
    df_t = db_query(
        "SELECT symbol, token_address, decision, entry_price, buy_amount_usd, "
        "sell_amount_usd, rejection_reason, funnel_stage, timestamp "
        "FROM trades ORDER BY id DESC LIMIT ?",
        (limit,),
    )
    if df_t.empty:
        return pd.DataFrame()

    rows = []
    for _, r in df_t.iterrows():
        dec = str(r.get("decision", ""))
        rows.append({
            "event_type":    dec if dec in ("BUY", "SELL") else "REJECT",
            "symbol":        r.get("symbol", "?"),
            "address":       r.get("token_address", ""),
            "tx_signature":  None,
            "buy_amount_usd":  r.get("buy_amount_usd"),
            "sell_amount_usd": r.get("sell_amount_usd"),
            "price_usd":     r.get("entry_price", 0),
            "pnl_usd":       None,
            "pnl_pct":       None,
            "stage":         r.get("funnel_stage", ""),
            "message":       str(r.get("rejection_reason", ""))[:80],
            "timestamp":     r.get("timestamp", ""),
        })
    return pd.DataFrame(rows)
