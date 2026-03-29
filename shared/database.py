import sqlite3
import os
from loguru import logger

DB_PATH = "memecoin_bot.db"

def init_db():
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    cursor.executescript('''
        CREATE TABLE IF NOT EXISTS signals (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            token_symbol TEXT NOT NULL,
            fusion_score REAL NOT NULL,
            decision TEXT NOT NULL,
            breakdown TEXT NOT NULL,
            timestamp DATETIME DEFAULT CURRENT_TIMESTAMP
        );
        CREATE TABLE IF NOT EXISTS trades (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            token_address TEXT NOT NULL,
            amount_usd REAL NOT NULL,
            entry_price REAL,
            exit_price REAL,
            pnl REAL,
            dry_run BOOLEAN DEFAULT 1,
            timestamp DATETIME DEFAULT CURRENT_TIMESTAMP
        );
        CREATE TABLE IF NOT EXISTS news_items (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            source TEXT NOT NULL,
            content TEXT NOT NULL,
            token_symbol TEXT NOT NULL,
            sentiment_weight REAL,
            timestamp DATETIME DEFAULT CURRENT_TIMESTAMP
        );
    ''')
    conn.commit()
    conn.close()
    logger.info("Database (SQLite) initialized: tables signals, trades, news_items verified.")

if __name__ == "__main__":
    init_db()
