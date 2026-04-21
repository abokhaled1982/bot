import sqlite3
from datetime import datetime

def init_db():
    conn = sqlite3.connect('memecoin_bot.db')
    c = conn.cursor()
    # Create trades table
    c.execute('''CREATE TABLE IF NOT EXISTS trades
                 (id INTEGER PRIMARY KEY AUTOINCREMENT,
                  token_address TEXT,
                  symbol TEXT,
                  entry_price REAL,
                  position_size REAL,
                  score REAL,
                  decision TEXT,
                  rejection_reason TEXT,
                  ai_reasoning TEXT,
                  funnel_stage TEXT,
                  timestamp DATETIME,
                  gates_passed TEXT,
                  pair_created_at INTEGER,
                  tx_signature TEXT,
                  tx_status TEXT,
                  buy_amount_usd REAL,
                  sell_amount_usd REAL,
                  source TEXT,
                  dex_url TEXT,
                  market_cap REAL,
                  fdv REAL,
                  liquidity_usd REAL,
                  volume_h1 REAL,
                  volume_h24 REAL,
                  volume_spike REAL,
                  change_5m REAL,
                  change_1h REAL,
                  change_24h REAL,
                  vol_mcap_ratio REAL,
                  buys_h1 INTEGER,
                  sells_h1 INTEGER,
                  buys_h24 INTEGER,
                  sells_h24 INTEGER,
                  mint_authority TEXT,
                  rugcheck_score INTEGER,
                  rugcheck_lp_locked REAL,
                  rugcheck_dangers TEXT,
                  rugcheck_warnings TEXT,
                  top_10_holder_pct REAL,
                  holder_count INTEGER,
                  liquidity_locked INTEGER,
                  raydium_vol_24h REAL,
                  raydium_tvl REAL,
                  raydium_burn_pct REAL,
                  confidence TEXT,
                  hype_score INTEGER,
                  risk_flags TEXT,
                  fusion_hype REAL,
                  fusion_liq_lock REAL,
                  fusion_vol_spike REAL,
                  fusion_wallet REAL,
                  fusion_buy_sell REAL,
                  fusion_vol_mcap REAL,
                  fusion_risk REAL,
                  fusion_btc REAL,
                  fusion_override TEXT,
                  token_age_hours REAL)''')
    # Create logs table
    c.execute('''CREATE TABLE IF NOT EXISTS bot_logs
                 (id INTEGER PRIMARY KEY AUTOINCREMENT,
                  level TEXT,
                  message TEXT,
                  timestamp DATETIME)''')
    conn.commit()
    conn.close()
    print("Database initialized successfully.")

if __name__ == "__main__":
    init_db()
