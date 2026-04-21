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
    except Exception:
        return pd.DataFrame()


# ── Price / Market Data ───────────────────────────────────────────────────────

@st.cache_data(ttl=20)
def get_live_price(address: str) -> float:
    """Fetch current token price from DexScreener. Falls back to 0.0."""
    try:
        r = requests.get(
            f"https://api.dexscreener.com/latest/dex/tokens/{address}", timeout=5
        )
        if r.status_code == 200:
            pairs = r.json().get("pairs", [])
            if pairs:
                return float(pairs[0].get("priceUsd") or 0)
    except Exception:
        pass
    return 0.0


@st.cache_data(ttl=30)
def get_token_full_info(address: str) -> dict:
    """Full market snapshot for a token from DexScreener."""
    try:
        r = requests.get(
            f"https://api.dexscreener.com/latest/dex/tokens/{address}", timeout=5
        )
        if r.status_code == 200:
            pairs = r.json().get("pairs", [])
            if pairs:
                p    = pairs[0]
                vol  = p.get("volume", {})
                pc   = p.get("priceChange", {})
                txns = p.get("txns", {})
                return {
                    "symbol":     p.get("baseToken", {}).get("symbol", "?"),
                    "price":      float(p.get("priceUsd") or 0),
                    "change_5m":  float(pc.get("m5",  0) or 0),
                    "change_1h":  float(pc.get("h1",  0) or 0),
                    "change_6h":  float(pc.get("h6",  0) or 0),
                    "change_24h": float(pc.get("h24", 0) or 0),
                    "volume_1h":  float(vol.get("h1",  0) or 0),
                    "volume_24h": float(vol.get("h24", 0) or 0),
                    "liquidity":  float(p.get("liquidity", {}).get("usd", 0) or 0),
                    "market_cap": float(p.get("marketCap", 0) or 0),
                    "buys_1h":    int(txns.get("h1",  {}).get("buys",  0) or 0),
                    "sells_1h":   int(txns.get("h1",  {}).get("sells", 0) or 0),
                    "buys_24h":   int(txns.get("h24", {}).get("buys",  0) or 0),
                    "sells_24h":  int(txns.get("h24", {}).get("sells", 0) or 0),
                    "pair_created_at": p.get("pairCreatedAt", 0),
                    "dex_url":    p.get("url", ""),
                    "dex_id":     p.get("dexId", ""),
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
    from src.utils.rpc import rpc_call
    try:
        result = rpc_call("getBalance", [wallet])
        if result is not None:
            return result["value"] / 1e9
    except Exception:
        pass
    return 0.0


@st.cache_data(ttl=15)
def get_wallet_tokens(wallet: str) -> list:
    """Return all SPL tokens (legacy + Token-2022) with balance > 0, enriched with symbol."""
    from src.utils.rpc import rpc_call
    TOKEN_PROGRAMS = [
        "TokenkegQfeZyiNwAJbNbGKPFXCWuBvf9Ss623VQ5DA",
        "TokenzQdBNbLqP5VEhdkAS6EPFLC1PHnBqCXEpPxuEb",
    ]
    tokens    = []
    seen_mints = set()
    for prog in TOKEN_PROGRAMS:
        try:
            result = rpc_call("getTokenAccountsByOwner", [
                wallet, {"programId": prog}, {"encoding": "jsonParsed"},
            ])
            if not result:
                continue
            for acc in result.get("value", []):
                info   = acc["account"]["data"]["parsed"]["info"]
                mint   = info["mint"]
                if mint in seen_mints:
                    continue
                amount = float(info["tokenAmount"]["uiAmount"] or 0)
                if amount > 0:
                    seen_mints.add(mint)
                    tokens.append({"mint": mint, "amount": amount, "symbol": mint[:8]})
        except Exception:
            continue

    # Enrich symbols from DexScreener in batch
    if tokens:
        try:
            mints_str = ",".join(t["mint"] for t in tokens[:30])
            r = requests.get(
                f"https://api.dexscreener.com/latest/dex/tokens/{mints_str}", timeout=5,
            )
            if r.status_code == 200:
                sym_map = {}
                for pair in r.json().get("pairs") or []:
                    base = pair.get("baseToken", {})
                    if base.get("address") and base.get("symbol"):
                        sym_map[base["address"]] = base["symbol"]
                for t in tokens:
                    if t["mint"] in sym_map:
                        t["symbol"] = sym_map[t["mint"]]
        except Exception:
            pass
    return tokens


def load_positions() -> dict:
    if os.path.exists("positions.json"):
        try:
            with open("positions.json") as f:
                return json.load(f)
        except Exception:
            pass
    return {}


@st.cache_data(ttl=15)
def get_reconciled_positions(wallet: str) -> dict:
    """
    Merge positions.json (bot-tracked) with real wallet tokens.
    Each position is enriched with wallet_confirmed, sell_failed, etc.
    """
    bot_positions = load_positions()

    try:
        real_tokens = get_wallet_tokens(wallet)
        real_mints  = {t["mint"] for t in real_tokens if t["amount"] > 0}
    except Exception:
        real_tokens = []
        real_mints  = set()

    for addr in list(bot_positions.keys()):
        bot_positions[addr]["wallet_confirmed"] = addr in real_mints

    for t in real_tokens:
        mint = t["mint"]
        if mint not in bot_positions and t["amount"] > 0:
            try:
                df_buy = db_query(
                    "SELECT entry_price, buy_amount_usd, timestamp FROM trades "
                    "WHERE token_address=? AND decision LIKE '%BUY%' ORDER BY id DESC LIMIT 1",
                    (mint,)
                )
                df_sell = db_query(
                    "SELECT timestamp FROM trades "
                    "WHERE token_address=? AND decision LIKE '%SELL%' ORDER BY id DESC LIMIT 1",
                    (mint,)
                )
                entry_price = float(df_buy.iloc[0]["entry_price"]) if not df_buy.empty else 0.0
                buy_ts_raw  = df_buy.iloc[0]["timestamp"] if not df_buy.empty else None
                created_at  = datetime.strptime(str(buy_ts_raw)[:19], "%Y-%m-%d %H:%M:%S").timestamp() if buy_ts_raw else 0
                sell_failed = False
                if not df_sell.empty and not df_buy.empty:
                    if str(df_sell.iloc[0]["timestamp"])[:19] > str(df_buy.iloc[0]["timestamp"])[:19]:
                        sell_failed = True
                manually_held = df_buy.empty and df_sell.empty
            except Exception:
                entry_price   = 0.0
                created_at    = 0
                sell_failed   = False
                manually_held = True

            bot_positions[mint] = {
                "symbol":           t.get("symbol", mint[:8]),
                "entry_price":      entry_price,
                "created_at":       created_at,
                "remaining_pct":    1.0,
                "tp1_hit":          False,
                "tp2_hit":          False,
                "tp3_hit":          False,
                "highest_price":    entry_price,
                "trailing_active":  False,
                "wallet_confirmed": True,
                "external":         True,
                "sell_failed":      sell_failed,
                "manually_held":    manually_held,
                "wallet_amount":    t["amount"],
            }

    return bot_positions


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
