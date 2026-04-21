"""
Unit tests for src/bot/pipeline.py helpers.

Tests:
  - get_btc_change() with mocked HTTP
  - _save_watchlist() writes correct JSON
  - evaluate_token() gate progression (mocked adapters)
  - _retry_watchlist() expiry logic
"""
import asyncio
import json
import os
import sys
import time
import tempfile
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))


# ── get_btc_change ────────────────────────────────────────────────────────────

class TestGetBtcChange:
    @pytest.mark.asyncio
    async def test_parses_24h_change(self):
        import aiohttp
        from src.bot.pipeline import get_btc_change

        mock_cm = AsyncMock()
        mock_cm.__aenter__.return_value.status = 200
        mock_cm.__aenter__.return_value.json = AsyncMock(
            return_value={"bitcoin": {"usd": 70000, "usd_24h_change": 4.8}}
        )

        with patch("aiohttp.ClientSession") as mock_session_cls:
            mock_sess = MagicMock()
            mock_sess.__aenter__ = AsyncMock(return_value=mock_sess)
            mock_sess.__aexit__  = AsyncMock(return_value=False)
            mock_sess.get.return_value = mock_cm
            mock_session_cls.return_value = mock_sess

            result = await get_btc_change()

        assert result == pytest.approx(4.8 / 24, rel=1e-4)

    @pytest.mark.asyncio
    async def test_returns_0_on_network_error(self):
        from src.bot.pipeline import get_btc_change
        with patch("aiohttp.ClientSession", side_effect=Exception("down")):
            result = await get_btc_change()
        assert result == 0.0


# ── _save_watchlist ───────────────────────────────────────────────────────────

class TestSaveWatchlist:
    def test_writes_valid_json(self, tmp_path, monkeypatch):
        import src.bot.pipeline as pipe
        monkeypatch.chdir(tmp_path)

        pipe.MIGRATION_WATCHLIST = {
            "addr1": {
                "token":    {"symbol": "AAA", "source": "pumpfun", "pumpfun_detected_at": 0},
                "added_at": 1_000_000.0,
                "retries":  3,
            }
        }
        pipe._save_watchlist()

        with open(tmp_path / "watchlist.json") as f:
            data = json.load(f)

        assert "addr1" in data
        assert data["addr1"]["symbol"]  == "AAA"
        assert data["addr1"]["retries"] == 3

    def test_empty_watchlist(self, tmp_path, monkeypatch):
        import src.bot.pipeline as pipe
        monkeypatch.chdir(tmp_path)
        pipe.MIGRATION_WATCHLIST = {}
        pipe._save_watchlist()
        with open(tmp_path / "watchlist.json") as f:
            assert json.load(f) == {}


# ── _retry_watchlist expiry ───────────────────────────────────────────────────

class TestRetryWatchlistExpiry:
    @pytest.mark.asyncio
    async def test_expired_by_age_removed(self, tmp_path, monkeypatch):
        import src.bot.pipeline as pipe
        monkeypatch.chdir(tmp_path)

        pipe.MIGRATION_WATCHLIST = {
            "old_addr": {
                "token":    {"symbol": "OLD", "source": "pumpfun", "pumpfun_detected_at": 0},
                "added_at": time.time() - 700,   # older than WATCHLIST_MAX_AGE_SEC (600)
                "retries":  5,
            }
        }

        dex = safety = chain = fusion = executor = monitor = MagicMock()
        result = await pipe._retry_watchlist(dex, safety, chain, fusion, executor, monitor, 0.0)

        assert "old_addr" not in pipe.MIGRATION_WATCHLIST
        assert result == 0

    @pytest.mark.asyncio
    async def test_expired_by_retries_removed(self, tmp_path, monkeypatch):
        import src.bot.pipeline as pipe
        monkeypatch.chdir(tmp_path)

        pipe.MIGRATION_WATCHLIST = {
            "retry_addr": {
                "token":    {"symbol": "RET", "source": "pumpfun", "pumpfun_detected_at": 0},
                "added_at": time.time() - 10,   # fresh
                "retries":  pipe.WATCHLIST_MAX_RETRIES,   # max retries hit
            }
        }

        dex = safety = chain = fusion = executor = monitor = MagicMock()
        await pipe._retry_watchlist(dex, safety, chain, fusion, executor, monitor, 0.0)

        assert "retry_addr" not in pipe.MIGRATION_WATCHLIST


# ── evaluate_token gate 1 (no data) ──────────────────────────────────────────

class TestEvaluateTokenGate1:
    def _make_executor(self, tmp_path):
        db = str(tmp_path / "test.db")
        import sqlite3
        conn = sqlite3.connect(db)
        conn.execute("""CREATE TABLE trades (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            token_address TEXT, symbol TEXT, entry_price REAL,
            position_size REAL, score REAL, decision TEXT,
            rejection_reason TEXT, ai_reasoning TEXT,
            funnel_stage TEXT, timestamp DATETIME,
            gates_passed TEXT, pair_created_at INTEGER,
            tx_signature TEXT, tx_status TEXT,
            buy_amount_usd REAL, sell_amount_usd REAL
        )""")
        conn.execute("""CREATE TABLE bot_events (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            event_type TEXT, symbol TEXT, address TEXT,
            tx_signature TEXT, buy_amount_usd REAL,
            sell_amount_usd REAL, price_usd REAL,
            pnl_usd REAL, pnl_pct REAL,
            stage TEXT, message TEXT, timestamp TEXT
        )""")
        conn.commit()
        conn.close()

        from src.execution.executor import TradeExecutor
        ex = TradeExecutor.__new__(TradeExecutor)
        ex.dry_run  = True
        ex.db_path  = db
        ex.max_position_usd = 0.20
        ex.min_position_usd = 0.10
        ex.keypair  = None
        ex.http     = MagicMock()
        return ex

    @pytest.mark.asyncio
    async def test_blocked_token_skipped(self, tmp_path):
        from src.bot.pipeline import evaluate_token
        from src.bot.filters  import BLOCKED_TOKENS

        addr  = next(iter(BLOCKED_TOKENS))
        token = {"address": addr, "symbol": "SOL", "source": "dex"}
        dex   = MagicMock(); dex.get_token_data = AsyncMock(return_value=None)
        monitor = MagicMock(); monitor.positions = {}
        executor = self._make_executor(tmp_path)

        result = await evaluate_token(
            token, dex, MagicMock(), MagicMock(), MagicMock(),
            executor, monitor, 0.0,
        )
        assert result is False

    @pytest.mark.asyncio
    async def test_no_dex_data_non_migration_gate1_fail(self, tmp_path):
        import src.bot.pipeline as pipe
        from src.bot.pipeline import evaluate_token

        pipe.MIGRATION_WATCHLIST  = {}
        pipe.BOUGHT_THIS_SESSION  = set()

        token   = {"address": "freshaddr1", "symbol": "NEW", "source": "dex"}
        dex     = MagicMock(); dex.get_token_data = AsyncMock(return_value=None)
        monitor = MagicMock(); monitor.positions = {}
        executor = self._make_executor(tmp_path)
        executor.execute_trade = AsyncMock(return_value={})

        result = await evaluate_token(
            token, dex, MagicMock(), MagicMock(), MagicMock(),
            executor, monitor, 0.0,
        )
        assert result is False

    @pytest.mark.asyncio
    async def test_duplicate_address_skipped(self, tmp_path):
        import src.bot.pipeline as pipe
        from src.bot.pipeline import evaluate_token

        pipe.BOUGHT_THIS_SESSION = {"dup_addr"}
        token   = {"address": "dup_addr", "symbol": "DUP", "source": "dex"}
        dex     = MagicMock()
        monitor = MagicMock(); monitor.positions = {}
        executor = self._make_executor(tmp_path)

        result = await evaluate_token(
            token, dex, MagicMock(), MagicMock(), MagicMock(),
            executor, monitor, 0.0,
        )
        assert result is False
        dex.get_token_data.assert_not_called()
