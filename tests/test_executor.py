"""
Unit tests for src/execution/executor.py

Tests the pure logic helpers with mocked network calls.
No real wallet, no real RPC, no real Jupiter.
"""
import os
import sys
import asyncio
import sqlite3
import tempfile
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))


# ── _sol_price_sync ───────────────────────────────────────────────────────────

class TestSolPriceSync:
    def test_coingecko_primary(self):
        from src.execution.executor import _sol_price_sync
        mock_http = MagicMock()
        mock_http.get.return_value.json.return_value = {"solana": {"usd": 175.50}}
        mock_http.get.return_value.status_code = 200
        price = _sol_price_sync(mock_http)
        assert price == pytest.approx(175.50)

    def test_dexscreener_fallback(self):
        from src.execution.executor import _sol_price_sync
        mock_http = MagicMock()
        # First call (CoinGecko) raises
        mock_http.get.side_effect = [
            Exception("timeout"),
            MagicMock(
                json=lambda: {"pairs": [{"priceUsd": "160.0"}]},
                status_code=200,
            ),
        ]
        price = _sol_price_sync(mock_http)
        assert price == pytest.approx(160.0)

    def test_fallback_to_150_on_all_failures(self):
        from src.execution.executor import _sol_price_sync
        mock_http = MagicMock()
        mock_http.get.side_effect = Exception("all down")
        price = _sol_price_sync(mock_http)
        assert price == pytest.approx(150.0)


# ── TradeExecutor._position_size ──────────────────────────────────────────────

class TestPositionSize:
    @pytest.fixture()
    def executor(self, tmp_path):
        with (
            patch("src.execution.executor._is_jupiter_reachable", return_value=False),
            patch.dict(os.environ, {
                "DRY_RUN": "True",
                "TRADE_MAX_POSITION_USD": "0.20",
                "TRADE_MIN_POSITION_USD": "0.10",
            }),
        ):
            from src.execution.executor import TradeExecutor
            ex = TradeExecutor.__new__(TradeExecutor)
            ex.dry_run          = True
            ex.max_position_usd = 0.20
            ex.min_position_usd = 0.10
            ex.db_path          = str(tmp_path / "test.db")
            ex.http             = MagicMock()
            ex.keypair          = None
            return ex

    def test_high_confidence(self, executor):
        assert executor._position_size("HIGH") == pytest.approx(0.20)

    def test_medium_confidence(self, executor):
        assert executor._position_size("MEDIUM") == pytest.approx(0.15)

    def test_low_confidence(self, executor):
        assert executor._position_size("LOW") == pytest.approx(0.10)

    def test_override_ignores_confidence(self, executor):
        assert executor._position_size("HIGH", override=0.05) == pytest.approx(0.05)


# ── TradeExecutor._slippage_bps ───────────────────────────────────────────────

class TestSlippageBps:
    @pytest.fixture()
    def executor(self, tmp_path):
        from src.execution.executor import TradeExecutor
        ex = TradeExecutor.__new__(TradeExecutor)
        return ex

    @pytest.mark.parametrize("liq, expected", [
        (600_000, 100),
        (150_000, 200),
        (75_000,  300),
        (30_000,  500),
        (5_000,   800),
        (0,       800),
    ])
    def test_slippage_levels(self, executor, liq, expected):
        assert executor._slippage_bps(liq) == expected


# ── _get_quote fallback behaviour ─────────────────────────────────────────────

class TestGetQuote:
    @pytest.mark.asyncio
    async def test_primary_success(self):
        from src.execution.executor import _get_quote
        mock_resp = MagicMock(status_code=200)
        mock_resp.json.return_value = {"outAmount": "1000000", "inAmount": "100000"}
        mock_http = MagicMock()
        mock_http.get.return_value = mock_resp

        result = await _get_quote(mock_http, "mintA", "mintB", 100000, 200)
        assert result["outAmount"] == "1000000"
        assert mock_http.get.call_count == 1

    @pytest.mark.asyncio
    async def test_fallback_on_primary_fail(self):
        from src.execution.executor import _get_quote, JUPITER_QUOTE_URLS
        call_count = [0]

        def fake_get(url, **kwargs):
            call_count[0] += 1
            m = MagicMock()
            if call_count[0] == 1:
                m.status_code = 503
                m.text = "Service Unavailable"
            else:
                m.status_code = 200
                m.json.return_value = {"outAmount": "999", "inAmount": "100"}
            return m

        mock_http = MagicMock()
        mock_http.get.side_effect = fake_get

        result = await _get_quote(mock_http, "A", "B", 1000, 100)
        assert result["outAmount"] == "999"
        assert call_count[0] == 2   # tried both endpoints

    @pytest.mark.asyncio
    async def test_raises_when_all_fail(self):
        from src.execution.executor import _get_quote
        mock_http = MagicMock()
        mock_http.get.side_effect = Exception("network down")

        with pytest.raises(RuntimeError, match="All Jupiter quote endpoints failed"):
            await _get_quote(mock_http, "A", "B", 1000, 100)


# ── execute_trade dry-run ─────────────────────────────────────────────────────

class TestExecuteTradeDryRun:
    @pytest.fixture()
    def executor(self, tmp_path):
        db = str(tmp_path / "test.db")
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

        with patch.dict(os.environ, {
            "DRY_RUN": "True",
            "TRADE_MAX_POSITION_USD": "0.20",
            "TRADE_MIN_POSITION_USD": "0.10",
        }):
            from src.execution.executor import TradeExecutor
            ex          = TradeExecutor.__new__(TradeExecutor)
            ex.dry_run  = True
            ex.db_path  = db
            ex.max_position_usd = 0.20
            ex.min_position_usd = 0.10
            ex.keypair  = None
            ex.http     = MagicMock()
            return ex

    @pytest.mark.asyncio
    async def test_dry_run_buy_returns_success(self, executor):
        result = await executor.execute_trade(
            token_symbol="BONK", token_address="addr123",
            score=75, decision="BUY",
            price=0.000001, confidence="HIGH",
        )
        assert result.get("status") == "success"
        assert result.get("dry_run") is True
        assert result.get("tx", "").startswith("SIM_")
        assert result.get("tx_status") == "simulated"
        assert result.get("buy_amount_usd") == pytest.approx(0.20)  # HIGH confidence
        assert result.get("sell_amount_usd") is None

    @pytest.mark.asyncio
    async def test_dry_run_sell_returns_success(self, executor):
        result = await executor.execute_trade(
            token_symbol="WIF", token_address="addr456",
            score=80, decision="SELL",
            price=0.05,
        )
        assert result.get("status") == "success"
        assert result.get("dry_run") is True
        assert result.get("tx", "").startswith("SIM_")
        assert result.get("tx_status") == "simulated"
        assert result.get("buy_amount_usd") is None
        assert result.get("sell_amount_usd") is not None

    @pytest.mark.asyncio
    async def test_non_trade_decision_returns_empty(self, executor):
        result = await executor.execute_trade(
            token_symbol="RUG", token_address="rugaddr",
            score=10, decision="REJECT",
            rejection_reason="Too risky",
        )
        assert result == {}

    @pytest.mark.asyncio
    async def test_dry_run_writes_to_db(self, executor):
        await executor.execute_trade(
            token_symbol="BONK", token_address="addr123",
            score=75, decision="BUY", price=0.000001,
        )
        conn  = sqlite3.connect(executor.db_path)
        rows  = conn.execute("SELECT * FROM trades").fetchall()
        conn.close()
        assert len(rows) == 1
        assert "BUY" in rows[0][6]   # decision column

    @pytest.mark.asyncio
    async def test_dry_run_buy_stores_tx_and_amounts(self, executor):
        """DB row should have tx_signature, tx_status, buy_amount_usd."""
        await executor.execute_trade(
            token_symbol="BONK", token_address="addr_buy_db",
            score=75, decision="BUY", price=0.000001, confidence="HIGH",
        )
        conn = sqlite3.connect(executor.db_path)
        row = conn.execute(
            "SELECT tx_signature, tx_status, buy_amount_usd, sell_amount_usd FROM trades"
        ).fetchone()
        conn.close()
        assert row[0].startswith("SIM_")           # tx_signature
        assert row[1] == "simulated"                # tx_status
        assert row[2] == pytest.approx(0.20)        # buy_amount_usd (HIGH)
        assert row[3] is None                       # sell_amount_usd

    @pytest.mark.asyncio
    async def test_dry_run_sell_calculates_pnl(self, executor, tmp_path):
        """Sell simulation reads positions.json and computes sell_amount_usd."""
        import json as _j
        pos_file = tmp_path / "positions.json"
        pos_file.write_text(_j.dumps({
            "addr_with_pos": {
                "symbol": "PROFIT", "entry_price": 0.001,
                "created_at": 1000000, "remaining_pct": 1.0,
            }
        }))
        # Patch the open to read our temp positions file
        import builtins
        _orig_open = builtins.open
        def _fake_open(path, *a, **kw):
            if str(path) == "positions.json":
                return _orig_open(str(pos_file), *a, **kw)
            return _orig_open(path, *a, **kw)

        with patch("builtins.open", side_effect=_fake_open):
            result = await executor.execute_trade(
                token_symbol="PROFIT", token_address="addr_with_pos",
                score=80, decision="SELL",
                price=0.002,   # doubled from entry
                confidence="HIGH",
                sell_fraction=1.0,
            )
        # entry 0.001, current 0.002 → 2x → sell_amount = 0.20 * 1.0 * 2 = 0.40
        assert result.get("sell_amount_usd") == pytest.approx(0.40)
        assert result.get("buy_amount_usd") is None

    @pytest.mark.asyncio
    async def test_dry_run_emits_event(self, executor):
        await executor.execute_trade(
            token_symbol="BONK", token_address="addr123",
            score=75, decision="BUY", price=0.000001,
        )
        conn = sqlite3.connect(executor.db_path)
        rows = conn.execute("SELECT event_type FROM bot_events").fetchall()
        conn.close()
        assert any("SIMULATED" in r[0] for r in rows)
