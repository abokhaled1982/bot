import sqlite3
import os
from datetime import datetime

DB_NAME = 'stock_bot.db'

def init_db():
    conn = sqlite3.connect(DB_NAME)
    c = conn.cursor()
    
    # Trades table — full audit trail
    c.execute('''CREATE TABLE IF NOT EXISTS trades
                 (id INTEGER PRIMARY KEY AUTOINCREMENT,
                  ticker TEXT,
                  t212_ticker TEXT,
                  action TEXT,
                  combined_score REAL,
                  ta_score REAL,
                  sent_score REAL,
                  velocity_score REAL,
                  momentum REAL,
                  llm_conviction REAL,
                  quantity REAL,
                  price REAL,
                  status TEXT,
                  detail TEXT,
                  reason TEXT,
                  timestamp DATETIME,
                  gates_passed TEXT,
                  funnel_stage TEXT,
                  ai_reasoning TEXT,
                  confidence TEXT,
                  trade_id TEXT,
                  mention_count INTEGER,
                  headlines_used TEXT)''')
                  
    # News signals — every ticker extracted from headlines by LLM
    c.execute('''CREATE TABLE IF NOT EXISTS news_signals
                 (id INTEGER PRIMARY KEY AUTOINCREMENT,
                  ticker TEXT,
                  sentiment REAL,
                  urgency INTEGER,
                  headline TEXT,
                  source TEXT,
                  extracted_at DATETIME,
                  batch_id TEXT)''')
                  
    # Candidates — discovery history (what the engine found and evaluated)
    c.execute('''CREATE TABLE IF NOT EXISTS candidates
                 (id INTEGER PRIMARY KEY AUTOINCREMENT,
                  ticker TEXT,
                  mention_count INTEGER,
                  velocity_score REAL,
                  avg_sentiment REAL,
                  ta_score REAL,
                  fusion_score REAL,
                  llm_conviction REAL,
                  decision TEXT,
                  gates_passed TEXT,
                  rejection_reason TEXT,
                  cycle INTEGER,
                  timestamp DATETIME)''')

    # Bot logs
    c.execute('''CREATE TABLE IF NOT EXISTS bot_logs
                 (id INTEGER PRIMARY KEY AUTOINCREMENT,
                  level TEXT,
                  message TEXT,
                  timestamp DATETIME)''')
    
    # Indexes for fast dashboard queries
    c.execute('CREATE INDEX IF NOT EXISTS idx_signals_ticker ON news_signals(ticker)')
    c.execute('CREATE INDEX IF NOT EXISTS idx_signals_time ON news_signals(extracted_at)')
    c.execute('CREATE INDEX IF NOT EXISTS idx_candidates_time ON candidates(timestamp)')
    c.execute('CREATE INDEX IF NOT EXISTS idx_trades_time ON trades(timestamp)')
    
    conn.commit()
    conn.close()
    print(f"Database {DB_NAME} initialized successfully.")

if __name__ == "__main__":
    init_db()
