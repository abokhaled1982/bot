"""
Unit tests for src/execution/events.py

Tests:
 - Table creation (init)
 - emit() writes correct data
 - emit() never raises on bad DB path
 - All standard event types are accepted
"""
import os
import sqlite3
import tempfile
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

import pytest
from src.execution import events


@pytest.fixture()
def db_path(tmp_path):
    return str(tmp_path / "test_events.db")


def _read_events(db_path: str) -> list[dict]:
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    rows = conn.execute("SELECT * FROM bot_events ORDER BY id").fetchall()
    conn.close()
    return [dict(r) for r in rows]


class TestEventsInit:
    def test_creates_table(self, db_path):
        events.init(db_path)
        conn = sqlite3.connect(db_path)
        tables = [r[0] for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        ).fetchall()]
        conn.close()
        assert "bot_events" in tables

    def test_idempotent(self, db_path):
        """Calling init twice should not raise."""
        events.init(db_path)
        events.init(db_path)

    def test_schema_columns(self, db_path):
        events.init(db_path)
        conn = sqlite3.connect(db_path)
        cols = {r[1] for r in conn.execute("PRAGMA table_info(bot_events)").fetchall()}
        conn.close()
        expected = {
            "id", "event_type", "symbol", "address", "tx_signature",
            "buy_amount_usd", "sell_amount_usd", "price_usd",
            "pnl_usd", "pnl_pct", "stage", "message", "timestamp",
        }
        assert expected == cols


class TestEventsEmit:
    def test_emit_buy_success(self, db_path):
        events.init(db_path)
        events.emit(
            "BUY_SUCCESS", "BONK", "someaddress123",
            tx_signature="txabc", buy_amount_usd=0.20,
            price_usd=0.000001, stage="BUY_EXEC",
            message="✅ BUY confirmed",
            db_path=db_path,
        )
        rows = _read_events(db_path)
        assert len(rows) == 1
        r = rows[0]
        assert r["event_type"]    == "BUY_SUCCESS"
        assert r["symbol"]        == "BONK"
        assert r["address"]       == "someaddress123"
        assert r["tx_signature"]  == "txabc"
        assert r["buy_amount_usd"] == pytest.approx(0.20)
        assert r["price_usd"]     == pytest.approx(0.000001)

    def test_emit_sell_with_pnl(self, db_path):
        events.init(db_path)
        events.emit(
            "SELL_TP1", "WIF", "wifaddr",
            sell_amount_usd=0.30, pnl_usd=0.10, pnl_pct=0.50,
            stage="TP1", message="TP1 hit",
            db_path=db_path,
        )
        rows = _read_events(db_path)
        r = rows[0]
        assert r["event_type"]     == "SELL_TP1"
        assert r["pnl_usd"]        == pytest.approx(0.10)
        assert r["pnl_pct"]        == pytest.approx(0.50)
        assert r["sell_amount_usd"] == pytest.approx(0.30)

    def test_emit_does_not_raise_on_bad_db(self):
        """emit() should swallow errors silently."""
        events.emit(
            "BOT_START", None, None,
            message="started",
            db_path="/nonexistent/path/db.sqlite3",
        )

    def test_emit_multiple_events(self, db_path):
        events.init(db_path)
        for et in ["BUY_SUCCESS", "SELL_TP1", "SELL_STOP_LOSS", "POSITION_CLOSED"]:
            events.emit(et, "SYM", "addr", db_path=db_path)
        rows = _read_events(db_path)
        assert len(rows) == 4
        assert [r["event_type"] for r in rows] == [
            "BUY_SUCCESS", "SELL_TP1", "SELL_STOP_LOSS", "POSITION_CLOSED"
        ]

    def test_emit_defaults_nulls(self, db_path):
        """Optional fields default to None without error."""
        events.init(db_path)
        events.emit("BOT_START", db_path=db_path)
        rows = _read_events(db_path)
        assert len(rows) == 1
        r = rows[0]
        assert r["symbol"]        is None
        assert r["address"]       is None
        assert r["tx_signature"]  is None
        assert r["buy_amount_usd"] is None
        assert r["pnl_usd"]        is None

    def test_emit_timestamp_set(self, db_path):
        events.init(db_path)
        events.emit("BOT_START", db_path=db_path)
        rows = _read_events(db_path)
        assert rows[0]["timestamp"] is not None
        assert len(rows[0]["timestamp"]) > 10   # ISO string

    @pytest.mark.parametrize("event_type", [
        "BUY_SUCCESS", "BUY_SIMULATED", "BUY_FAILED",
        "SELL_SUCCESS", "SELL_SIMULATED", "SELL_FAILED",
        "SELL_TP1", "SELL_TP2", "SELL_TP3",
        "SELL_STOP_LOSS", "SELL_TRAILING_STOP", "SELL_TIME_EXIT", "SELL_MANUAL",
        "BOT_START", "BOT_STOP", "POSITION_ADDED", "POSITION_CLOSED",
    ])
    def test_all_event_types_accepted(self, db_path, event_type):
        events.init(db_path)
        events.emit(event_type, "SYM", "addr", db_path=db_path)
        rows = _read_events(db_path)
        assert rows[-1]["event_type"] == event_type
