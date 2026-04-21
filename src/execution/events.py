"""
src/execution/events.py — Structured trade events.

Emits events to the `bot_events` table so the dashboard has a clean
event feed without parsing unstructured bot_logs.

Event types:
  BUY_SUCCESS, BUY_SIMULATED, BUY_FAILED
  SELL_SUCCESS, SELL_SIMULATED, SELL_FAILED
  SELL_TP1, SELL_TP2, SELL_TP3
  SELL_STOP_LOSS, SELL_TRAILING_STOP, SELL_TIME_EXIT, SELL_MANUAL
  BOT_START, BOT_STOP, POSITION_ADDED, POSITION_CLOSED
"""
import sqlite3
from datetime import datetime
from typing import Optional
from loguru import logger

_DB_PATH = "memecoin_bot.db"

_SCHEMA = """
CREATE TABLE IF NOT EXISTS bot_events (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    event_type      TEXT    NOT NULL,
    symbol          TEXT,
    address         TEXT,
    tx_signature    TEXT,
    buy_amount_usd  REAL,
    sell_amount_usd REAL,
    price_usd       REAL,
    pnl_usd         REAL,
    pnl_pct         REAL,
    stage           TEXT,
    message         TEXT,
    timestamp       TEXT    NOT NULL
)
"""


def init(db_path: str = _DB_PATH) -> None:
    """Create bot_events table if it doesn't exist."""
    conn = sqlite3.connect(db_path)
    conn.execute(_SCHEMA)
    conn.commit()
    conn.close()


def emit(
    event_type:      str,
    symbol:          Optional[str]   = None,
    address:         Optional[str]   = None,
    *,
    tx_signature:    Optional[str]   = None,
    buy_amount_usd:  Optional[float] = None,
    sell_amount_usd: Optional[float] = None,
    price_usd:       Optional[float] = None,
    pnl_usd:         Optional[float] = None,
    pnl_pct:         Optional[float] = None,
    stage:           Optional[str]   = None,
    message:         Optional[str]   = None,
    db_path:         str             = _DB_PATH,
) -> None:
    """Write a structured event to bot_events. Never raises."""
    try:
        conn = sqlite3.connect(db_path)
        conn.execute(
            """INSERT INTO bot_events
               (event_type, symbol, address, tx_signature,
                buy_amount_usd, sell_amount_usd, price_usd,
                pnl_usd, pnl_pct, stage, message, timestamp)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?)""",
            (
                event_type, symbol, address, tx_signature,
                buy_amount_usd, sell_amount_usd, price_usd,
                pnl_usd, pnl_pct, stage, message,
                datetime.now().isoformat(),
            ),
        )
        conn.commit()
        conn.close()
    except Exception as e:
        logger.warning(f"[EVENTS] emit() failed: {e}")
