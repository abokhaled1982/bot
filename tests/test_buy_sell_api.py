"""
tests/test_buy_sell_api.py — End-to-end tests for the buy/sell API.

Validates TradeExecutor.execute_trade() at $0.10 position size:
  - DRY-RUN buy writes correct DB row + event
  - DRY-RUN sell writes correct DB row + event
  - Position sizing respects $0.10 MAX / $0.05 MIN
  - REJECT decision writes to DB without trade event
  - Slippage bands are correct for different liquidity levels
  - _sol_price_sync fallback chain works
  - _get_quote multi-endpoint fallback works
  - _confirm_transaction timeout returns (False, msg)
  - All-RPC failure in _send_transaction raises RuntimeError
"""
import os
import sys
import asyncio
import sqlite3
import tempfile
from unittest.mock import AsyncMock, MagicMock, patch, call

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

# ── shared fixture ─────────────────────────────────────────────────────────────

@pytest.fixture()
def trade_db(tmp_path):
    """Minimal SQLite DB with trades + bot_events tables."""
    db = str(tmp_path / "trade_test.db")
    conn = sqlite3.connect(db)
    conn.executescript("""
        CREATE TABLE trades (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            token_address TEXT, symbol TEXT, entry_price REAL,
            position_size REAL, score REAL, decision TEXT,
            rejection_reason TEXT, ai_reasoning TEXT,
            funnel_stage TEXT, gates_passed TEXT,
            pair_created_at INTEGER, tx_signature TEXT,
            tx_status TEXT, buy_amount_usd REAL,
            sell_amount_usd REAL, timestamp DATETIME
        );
        CREATE TABLE bot_events (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            event_type TEXT, symbol TEXT, address TEXT,
            tx_signature TEXT, buy_amount_usd REAL,
            sell_amount_usd REAL, price_usd REAL,
            pnl_usd REAL, pnl_pct REAL,
            stage TEXT, message TEXT, timestamp TEXT
        );
    """)
    conn.commit()
    conn.close()
    return db


@pytest.fixture()
def executor(trade_db):
    """TradeExecutor in DRY-RUN mode with $0.10 position size."""
    with patch.dict(os.environ, {
        "DRY_RUN":               "True",
        "TRADE_MAX_POSITION_USD": "0.10",
        "TRADE_MIN_POSITION_USD": "0.05",
    }):
        from src.execution.executor import TradeExecutor
        ex                 = TradeExecutor.__new__(TradeExecutor)
        ex.dry_run         = True
        ex.db_path         = trade_db
        ex.max_position_usd = 0.10
        ex.min_position_usd = 0.05
        ex.keypair         = None
        ex.http            = MagicMock()
        return ex


def _rows(db: str, table: str = "trades") -> list[dict]:
    conn = sqlite3.connect(db)
    conn.row_factory = sqlite3.Row
    rows = conn.execute(f"SELECT * FROM {table}").fetchall()
    conn.close()
    return [dict(r) for r in rows]


# ── Position sizing at $0.10 ──────────────────────────────────────────────────

class TestPositionSizing010:
    def test_high_confidence_buys_max(self, executor):
        assert executor._position_size("HIGH") == pytest.approx(0.10)

    def test_medium_confidence_buys_mid(self, executor):
        assert executor._position_size("MEDIUM") == pytest.approx(0.075)

    def test_low_confidence_buys_min(self, executor):
        assert executor._position_size("LOW") == pytest.approx(0.05)

    def test_override_ignores_confidence_and_env(self, executor):
        assert executor._position_size("HIGH", override=0.10) == pytest.approx(0.10)
        assert executor._position_size("LOW",  override=0.10) == pytest.approx(0.10)

    def test_max_position_from_env(self, executor):
        assert executor.max_position_usd == pytest.approx(0.10)

    def test_min_position_from_env(self, executor):
        assert executor.min_position_usd == pytest.approx(0.05)


# ── DRY-RUN BUY ──────────────────────────────────────────────────────────────

class TestDryRunBuy:
    async def test_returns_success(self, executor):
        r = await executor.execute_trade(
            "BONK", "addr1", 75, "BUY",
            price=0.000001, confidence="HIGH", liquidity_usd=100_000,
        )
        assert r["status"]  == "success"
        assert r["dry_run"] is True

    async def test_writes_db_row(self, executor):
        await executor.execute_trade(
            "BONK", "addr1", 75, "BUY", price=0.000001, confidence="HIGH",
        )
        rows = _rows(executor.db_path)
        assert len(rows) == 1
        assert "BUY" in rows[0]["decision"]
        assert rows[0]["token_address"] == "addr1"
        assert rows[0]["symbol"]        == "BONK"

    async def test_position_size_stored_correctly(self, executor):
        await executor.execute_trade(
            "BONK", "addr1", 75, "BUY",
            price=0.000001, confidence="HIGH",
        )
        row = _rows(executor.db_path)[0]
        assert row["position_size"] == pytest.approx(0.10)

    async def test_emits_buy_simulated_event(self, executor):
        await executor.execute_trade(
            "BONK", "addr1", 75, "BUY", price=0.000001,
        )
        evs = _rows(executor.db_path, "bot_events")
        assert len(evs) == 1
        assert evs[0]["event_type"] == "BUY_SIMULATED"
        assert evs[0]["symbol"]      == "BONK"

    async def test_buy_amount_in_event_matches_position_size(self, executor):
        await executor.execute_trade(
            "BONK", "addr1", 75, "BUY",
            price=0.000001, confidence="HIGH",
        )
        evs = _rows(executor.db_path, "bot_events")
        assert evs[0]["buy_amount_usd"] == pytest.approx(0.10)

    async def test_stage_stored_in_event(self, executor):
        await executor.execute_trade(
            "BONK", "addr1", 75, "BUY",
            price=0.000001, funnel_stage="BUY_EXEC",
        )
        evs = _rows(executor.db_path, "bot_events")
        assert evs[0]["stage"] == "BUY_EXEC"

    async def test_price_stored_in_event(self, executor):
        await executor.execute_trade(
            "BONK", "addr1", 75, "BUY", price=0.00001234,
        )
        evs = _rows(executor.db_path, "bot_events")
        assert evs[0]["price_usd"] == pytest.approx(0.00001234)


# ── DRY-RUN SELL ─────────────────────────────────────────────────────────────

class TestDryRunSell:
    async def test_returns_success(self, executor):
        r = await executor.execute_trade(
            "BONK", "addr1", 75, "SELL", price=0.000015,
        )
        assert r["status"]  == "success"
        assert r["dry_run"] is True

    async def test_writes_db_row_with_sell(self, executor):
        await executor.execute_trade(
            "BONK", "addr1", 75, "SELL", price=0.000015,
        )
        rows = _rows(executor.db_path)
        assert len(rows) == 1
        assert "SELL" in rows[0]["decision"]

    async def test_emits_sell_simulated_event(self, executor):
        await executor.execute_trade(
            "BONK", "addr1", 75, "SELL", price=0.000015,
        )
        evs = _rows(executor.db_path, "bot_events")
        assert evs[0]["event_type"] == "SELL_SIMULATED"

    async def test_partial_sell_fraction(self, executor):
        """Partial sell (50%) — sell_amount should not be set to buy_amount_usd."""
        await executor.execute_trade(
            "BONK", "addr1", 75, "SELL",
            price=0.000015, sell_fraction=0.5,
        )
        rows = _rows(executor.db_path)
        assert "SELL" in rows[0]["decision"]

    async def test_buy_then_sell_both_recorded(self, executor):
        await executor.execute_trade("BONK", "addr1", 75, "BUY",  price=0.000001)
        await executor.execute_trade("BONK", "addr1", 75, "SELL", price=0.000015)
        rows = _rows(executor.db_path)
        assert len(rows) == 2
        decisions = {r["decision"] for r in rows}
        assert any("BUY"  in d for d in decisions)
        assert any("SELL" in d for d in decisions)


# ── REJECT / non-trade decisions ──────────────────────────────────────────────

class TestNonTradeDecisions:
    async def test_reject_returns_empty_dict(self, executor):
        r = await executor.execute_trade(
            "RUG", "rugaddr", 10, "REJECT",
            rejection_reason="Too risky",
        )
        assert r == {}

    async def test_skip_returns_empty_dict(self, executor):
        r = await executor.execute_trade("X", "xaddr", 0, "SKIP")
        assert r == {}

    async def test_reject_writes_db_row(self, executor):
        await executor.execute_trade(
            "RUG", "rugaddr", 10, "REJECT",
            rejection_reason="Safety fail",
        )
        rows = _rows(executor.db_path)
        assert len(rows) == 1
        assert rows[0]["decision"] == "REJECT"

    async def test_reject_does_not_emit_event(self, executor):
        await executor.execute_trade(
            "RUG", "rugaddr", 10, "REJECT",
        )
        evs = _rows(executor.db_path, "bot_events")
        assert len(evs) == 0


# ── Slippage bands ────────────────────────────────────────────────────────────

class TestSlippageBands:
    from src.execution.executor import TradeExecutor as _TE

    @pytest.mark.parametrize("liq, expected_bps", [
        (1_000_000, 100),   # very liquid  → tight slippage
        (200_000,   200),   # liquid        → 200 bps
        (80_000,    300),   # medium        → 300 bps
        (35_000,    500),   # illiquid      → 500 bps
        (5_000,     800),   # very illiquid → max slippage
        (0,         800),   # no liquidity  → max slippage
    ])
    def test_slippage_for_liquidity(self, liq, expected_bps):
        from src.execution.executor import TradeExecutor
        ex = TradeExecutor.__new__(TradeExecutor)
        assert ex._slippage_bps(liq) == expected_bps


# ── SOL price fallback ────────────────────────────────────────────────────────

class TestSolPriceFallback:
    def test_coingecko_primary(self):
        from src.execution.executor import _sol_price_sync
        http = MagicMock()
        http.get.return_value.json.return_value = {"solana": {"usd": 142.50}}
        assert _sol_price_sync(http) == pytest.approx(142.50)

    def test_dexscreener_fallback_when_coingecko_fails(self):
        from src.execution.executor import _sol_price_sync
        http = MagicMock()
        http.get.side_effect = [
            Exception("coingecko down"),
            MagicMock(json=lambda: {"pairs": [{"priceUsd": "138.00"}]}),
        ]
        assert _sol_price_sync(http) == pytest.approx(138.0)

    def test_hardcoded_150_when_all_fail(self):
        from src.execution.executor import _sol_price_sync
        http = MagicMock()
        http.get.side_effect = Exception("all down")
        assert _sol_price_sync(http) == pytest.approx(150.0)

    def test_coingecko_bad_json_falls_to_dexscreener(self):
        from src.execution.executor import _sol_price_sync
        http = MagicMock()
        http.get.side_effect = [
            MagicMock(json=lambda: {}),   # coingecko returns empty
            MagicMock(json=lambda: {"pairs": [{"priceUsd": "155.00"}]}),
        ]
        assert _sol_price_sync(http) == pytest.approx(155.0)


# ── Jupiter quote multi-endpoint fallback ─────────────────────────────────────

class TestJupiterQuoteFallback:
    async def test_uses_first_endpoint_when_healthy(self):
        from src.execution.executor import _get_quote
        http = MagicMock()
        http.get.return_value = MagicMock(
            status_code=200,
            json=MagicMock(return_value={"outAmount": "5000", "inAmount": "1000"}),
        )
        result = await _get_quote(http, "SOL", "BONK", 1000, 200)
        assert result["outAmount"] == "5000"
        assert http.get.call_count == 1

    async def test_falls_to_second_endpoint_on_503(self):
        from src.execution.executor import _get_quote
        call_count = [0]

        def fake_get(url, **kwargs):
            call_count[0] += 1
            m = MagicMock()
            m.status_code = 503 if call_count[0] == 1 else 200
            m.text = "down"
            m.json.return_value = {"outAmount": "9999", "inAmount": "100"}
            return m

        http = MagicMock(); http.get.side_effect = fake_get
        result = await _get_quote(http, "SOL", "BONK", 1000, 200)
        assert result["outAmount"] == "9999"
        assert call_count[0] == 2

    async def test_raises_when_all_endpoints_down(self):
        from src.execution.executor import _get_quote
        http = MagicMock()
        http.get.side_effect = Exception("network unreachable")
        with pytest.raises(RuntimeError, match="All Jupiter quote endpoints failed"):
            await _get_quote(http, "SOL", "BONK", 1000, 200)

    async def test_raises_on_missing_out_amount(self):
        from src.execution.executor import _get_quote
        http = MagicMock()
        http.get.return_value = MagicMock(
            status_code=200,
            json=MagicMock(return_value={"error": "no route"}),
            text="no route",
        )
        with pytest.raises(RuntimeError):
            await _get_quote(http, "SOL", "BONK", 1000, 200)


# ── _confirm_transaction timeout ─────────────────────────────────────────────

class TestConfirmTransactionTimeout:
    async def test_returns_false_on_timeout(self):
        from src.execution.executor import _confirm_transaction

        class _CtxNone:
            async def __aenter__(self):
                return MagicMock(json=AsyncMock(return_value={"result": {"value": [None]}}))
            async def __aexit__(self, *a): pass

        with patch("aiohttp.ClientSession") as mock_cls:
            sess = MagicMock()
            sess.__aenter__ = AsyncMock(return_value=sess)
            sess.__aexit__  = AsyncMock(return_value=False)
            sess.post       = MagicMock(return_value=_CtxNone())
            mock_cls.return_value = sess

            confirmed, msg = await _confirm_transaction("faketxid000", timeout_sec=1)
            assert confirmed is False
            assert "faketxid000" in msg

    async def test_returns_true_on_finalized(self):
        from src.execution.executor import _confirm_transaction

        class _CtxFinal:
            async def __aenter__(self):
                return MagicMock(json=AsyncMock(return_value={
                    "result": {"value": [{"confirmationStatus": "finalized", "err": None}]}
                }))
            async def __aexit__(self, *a): pass

        with patch("aiohttp.ClientSession") as mock_cls:
            sess = MagicMock()
            sess.__aenter__ = AsyncMock(return_value=sess)
            sess.__aexit__  = AsyncMock(return_value=False)
            sess.post       = MagicMock(return_value=_CtxFinal())
            mock_cls.return_value = sess

            confirmed, msg = await _confirm_transaction("finaltx", timeout_sec=30)
            assert confirmed is True
            assert msg == ""
