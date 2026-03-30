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
                  timestamp DATETIME)''')
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
