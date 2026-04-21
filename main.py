"""Memecoin Trading Bot — entry point."""
import asyncio
import os
import sqlite3
from datetime import datetime

from dotenv import load_dotenv
from loguru import logger

load_dotenv()

DB_PATH = "memecoin_bot.db"


def _init_db() -> None:
    conn = sqlite3.connect(DB_PATH)
    c    = conn.cursor()
    c.execute("""CREATE TABLE IF NOT EXISTS trades (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        token_address TEXT, symbol TEXT, entry_price REAL,
        position_size REAL, score REAL, decision TEXT,
        rejection_reason TEXT, ai_reasoning TEXT,
        funnel_stage TEXT, gates_passed TEXT,
        pair_created_at INTEGER, tx_signature TEXT,
        tx_status TEXT, buy_amount_usd REAL,
        sell_amount_usd REAL, timestamp DATETIME
    )""")
    c.execute("""CREATE TABLE IF NOT EXISTS bot_logs (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        level TEXT, message TEXT, timestamp DATETIME
    )""")
    c.execute("""CREATE TABLE IF NOT EXISTS bot_events (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        event_type TEXT, symbol TEXT, address TEXT,
        tx_signature TEXT, buy_amount_usd REAL,
        sell_amount_usd REAL, price_usd REAL,
        pnl_usd REAL, pnl_pct REAL,
        stage TEXT, message TEXT, timestamp TEXT
    )""")
    conn.commit()
    conn.close()


def _log_to_db(msg) -> None:
    try:
        conn = sqlite3.connect(DB_PATH)
        conn.execute(
            "INSERT INTO bot_logs (level, message, timestamp) VALUES (?,?,?)",
            (msg.record["level"].name, msg.record["message"], datetime.now()),
        )
        conn.commit()
        conn.close()
    except Exception:
        pass


async def _main() -> None:
    _init_db()
    logger.add(_log_to_db)
    logger.add(
        "logs/bot_{time:YYYY-MM-DD}.log",
        rotation="00:00", retention="14 days",
        level="INFO", enqueue=True,
    )

    from src.bot.orderflow_pipeline import main_loop
    await main_loop()


if __name__ == "__main__":
    asyncio.run(_main())
