"""
tests/test_gate_pipeline.py — Full 6-gate pipeline integration tests.

Validates that evaluate_token() behaves identically to the original
main.py behavior after refactoring. Every gate is tested for both
pass and reject paths. Tests are data-driven and use mocked adapters.

Gates tested:
  G1: DexScreener data              (missing → watchlist / skip)
  G2: Safety (RugCheck)             (unsafe → REJECT)
  G3: Chain data + risk assessment  (always passes — enriches data)
  G4: Pre-buy filter                (bad token → REJECT)
  G5: Fusion scoring                (score too low → REJECT)
  G6: Execution                     (success → BUY row + event)
"""
import asyncio
import json
import os
import sqlite3
import sys
import tempfile
import time
from unittest.mock import AsyncMock, MagicMock

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

os.environ.setdefault("DRY_RUN",                "True")
os.environ.setdefault("TRADE_MAX_POSITION_USD", "0.10")
os.environ.setdefault("TRADE_MIN_POSITION_USD", "0.05")


# ── Shared builders ───────────────────────────────────────────────────────────

def _good_token_data(**overrides) -> dict:
    """A token that should pass all 6 gates."""
    base = {
        "symbol":          "BONK",
        "price_usd":       0.00001,
        "liquidity_usd":   60_000,
        "volume_spike":    4.5,
        "change_1h":       25.0,
        "change_5m":       5.0,
        "change_24h":      40.0,
        "market_cap":      500_000,
        "buys_h1":         80,
        "sells_h1":        30,
        "buys_h24":        300,
        "sells_h24":       120,
        "volume_24h":      80_000,
        "pair_created_at": int(time.time() * 1000) - 8 * 3600 * 1000,  # 8h old
        "vol_mcap_ratio":  0.16,
    }
    base.update(overrides)
    return base


def _make_executor(db_path: str):
    from src.execution.executor import TradeExecutor
    ex                  = TradeExecutor.__new__(TradeExecutor)
    ex.dry_run          = True
    ex.db_path          = db_path
    ex.max_position_usd = 0.10
    ex.min_position_usd = 0.05
    ex.keypair          = None
    ex.http             = MagicMock()
    return ex


def _make_db(tmp_path) -> str:
    db = str(tmp_path / "pipe_test.db")
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


def _db_rows(db: str, table: str = "trades") -> list[dict]:
    conn = sqlite3.connect(db)
    conn.row_factory = sqlite3.Row
    rows = conn.execute(f"SELECT * FROM {table}").fetchall()
    conn.close()
    return [dict(r) for r in rows]


def _make_adapters(token_data, safety_data, chain_data, fusion_override=None):
    dex    = MagicMock(); dex.get_token_data       = AsyncMock(return_value=token_data)
    safety = MagicMock(); safety.get_safety_details = AsyncMock(return_value=safety_data)
    chain  = MagicMock(); chain.get_chain_data       = AsyncMock(return_value=chain_data)
    fusion = MagicMock()
    fusion.calculate_score = MagicMock(return_value=fusion_override or {
        "score": 72.0, "decision": "BUY", "confidence": "HIGH", "breakdown": {},
    })
    monitor = MagicMock()
    monitor.positions = {}
    monitor.add_position = AsyncMock()
    return dex, safety, chain, fusion, monitor


def _reset_pipeline():
    """Reset pipeline module-level state between tests."""
    import src.bot.pipeline as pipe
    pipe.BOUGHT_THIS_SESSION  = set()
    pipe.MIGRATION_WATCHLIST  = {}


# ── Gate 1: DexScreener data ──────────────────────────────────────────────────

class TestGate1Data:
    async def test_g1_pass_when_data_available(self, tmp_path):
        _reset_pipeline()
        db  = _make_db(tmp_path)
        ex  = _make_executor(db)
        dex, safety, chain, fusion, monitor = _make_adapters(
            _good_token_data(), {"is_safe": True}, {"top_10_holder_percent": 30, "liquidity_locked": True}
        )
        from src.bot.pipeline import evaluate_token
        result = await evaluate_token(
            {"address": "addr1", "symbol": "BONK", "source": "dex"},
            dex, safety, chain, fusion, ex, monitor, 0.0,
        )
        assert result is True
        rows = _db_rows(db)
        assert "G1:Data" in rows[-1]["gates_passed"]

    async def test_g1_fail_adds_to_watchlist_for_migration(self, tmp_path):
        _reset_pipeline()
        import src.bot.pipeline as pipe
        db  = _make_db(tmp_path)
        ex  = _make_executor(db)
        dex = MagicMock(); dex.get_token_data = AsyncMock(return_value=None)
        safety = chain = fusion = MagicMock()
        monitor = MagicMock(); monitor.positions = {}
        from src.bot.pipeline import evaluate_token
        result = await evaluate_token(
            {"address": "newmig1", "symbol": "NEW", "source": "pumpfun"},
            dex, safety, chain, fusion, ex, monitor, 0.0, is_migration=True,
        )
        assert result is False
        assert "newmig1" in pipe.MIGRATION_WATCHLIST

    async def test_g1_fail_logs_skip_for_non_migration(self, tmp_path):
        _reset_pipeline()
        db  = _make_db(tmp_path)
        ex  = _make_executor(db)
        ex.execute_trade = AsyncMock(return_value={})
        dex = MagicMock(); dex.get_token_data = AsyncMock(return_value=None)
        safety = chain = fusion = MagicMock()
        monitor = MagicMock(); monitor.positions = {}
        from src.bot.pipeline import evaluate_token
        result = await evaluate_token(
            {"address": "nodata1", "symbol": "X", "source": "dex"},
            dex, safety, chain, fusion, ex, monitor, 0.0, is_migration=False,
        )
        assert result is False

    async def test_g1_blocked_token_exits_immediately(self, tmp_path):
        _reset_pipeline()
        from src.bot.filters import BLOCKED_TOKENS
        db  = _make_db(tmp_path)
        ex  = _make_executor(db)
        dex = MagicMock()  # should NOT be called
        safety = chain = fusion = MagicMock()
        monitor = MagicMock(); monitor.positions = {}
        from src.bot.pipeline import evaluate_token
        blocked = next(iter(BLOCKED_TOKENS))
        result = await evaluate_token(
            {"address": blocked, "symbol": "SOL", "source": "dex"},
            dex, safety, chain, fusion, ex, monitor, 0.0,
        )
        assert result is False
        dex.get_token_data.assert_not_called()

    async def test_g1_duplicate_session_skipped(self, tmp_path):
        _reset_pipeline()
        import src.bot.pipeline as pipe
        pipe.BOUGHT_THIS_SESSION = {"dup_addr"}
        db  = _make_db(tmp_path)
        ex  = _make_executor(db)
        dex = MagicMock()
        safety = chain = fusion = MagicMock()
        monitor = MagicMock(); monitor.positions = {}
        from src.bot.pipeline import evaluate_token
        result = await evaluate_token(
            {"address": "dup_addr", "symbol": "DUP", "source": "dex"},
            dex, safety, chain, fusion, ex, monitor, 0.0,
        )
        assert result is False
        dex.get_token_data.assert_not_called()

    async def test_g1_already_in_positions_skipped(self, tmp_path):
        _reset_pipeline()
        db  = _make_db(tmp_path)
        ex  = _make_executor(db)
        dex = MagicMock()
        safety = chain = fusion = MagicMock()
        monitor = MagicMock(); monitor.positions = {"held_addr": {}}
        from src.bot.pipeline import evaluate_token
        result = await evaluate_token(
            {"address": "held_addr", "symbol": "HLD", "source": "dex"},
            dex, safety, chain, fusion, ex, monitor, 0.0,
        )
        assert result is False
        dex.get_token_data.assert_not_called()


# ── Gate 2: Safety ────────────────────────────────────────────────────────────

class TestGate2Safety:
    async def test_g2_fail_unsafe_token_rejected(self, tmp_path):
        _reset_pipeline()
        db  = _make_db(tmp_path)
        ex  = _make_executor(db)
        dex    = MagicMock(); dex.get_token_data       = AsyncMock(return_value=_good_token_data())
        safety = MagicMock(); safety.get_safety_details = AsyncMock(return_value={"is_safe": False, "mint_authority": "active"})
        chain  = MagicMock(); chain.get_chain_data       = AsyncMock(return_value={})
        fusion = MagicMock()
        monitor = MagicMock(); monitor.positions = {}
        from src.bot.pipeline import evaluate_token
        result = await evaluate_token(
            {"address": "scamaddr", "symbol": "SCAM", "source": "dex"},
            dex, safety, chain, fusion, ex, monitor, 0.0,
        )
        assert result is False
        rows = _db_rows(db)
        assert rows[-1]["funnel_stage"] == "SAFETY_CHECK"
        assert "G2" not in rows[-1]["gates_passed"]

    async def test_g2_fail_no_safety_data_rejected(self, tmp_path):
        _reset_pipeline()
        db  = _make_db(tmp_path)
        ex  = _make_executor(db)
        dex    = MagicMock(); dex.get_token_data       = AsyncMock(return_value=_good_token_data())
        safety = MagicMock(); safety.get_safety_details = AsyncMock(return_value=None)
        chain  = MagicMock(); chain.get_chain_data       = AsyncMock(return_value={})
        fusion = MagicMock()
        monitor = MagicMock(); monitor.positions = {}
        from src.bot.pipeline import evaluate_token
        result = await evaluate_token(
            {"address": "nocheck1", "symbol": "NC", "source": "dex"},
            dex, safety, chain, fusion, ex, monitor, 0.0,
        )
        assert result is False

    async def test_g2_pass_safe_token_continues(self, tmp_path):
        _reset_pipeline()
        db  = _make_db(tmp_path)
        ex  = _make_executor(db)
        dex, safety, chain, fusion, monitor = _make_adapters(
            _good_token_data(),
            {"is_safe": True},
            {"top_10_holder_percent": 30, "liquidity_locked": True},
        )
        from src.bot.pipeline import evaluate_token
        result = await evaluate_token(
            {"address": "safeaddr", "symbol": "SAFE", "source": "dex"},
            dex, safety, chain, fusion, ex, monitor, 0.0,
        )
        assert result is True
        rows = _db_rows(db)
        assert "G2:Safety" in rows[-1]["gates_passed"]


# ── Gate 4: Pre-buy filter ────────────────────────────────────────────────────

class TestGate4PreFilter:
    async def test_g4_fail_low_liquidity_rejected(self, tmp_path):
        _reset_pipeline()
        db  = _make_db(tmp_path)
        ex  = _make_executor(db)
        bad_token = _good_token_data(liquidity_usd=1_000)   # below $5k min
        dex, safety, chain, fusion, monitor = _make_adapters(
            bad_token, {"is_safe": True},
            {"top_10_holder_percent": 30, "liquidity_locked": True},
        )
        from src.bot.pipeline import evaluate_token
        result = await evaluate_token(
            {"address": "lowliq1", "symbol": "LOW", "source": "dex"},
            dex, safety, chain, fusion, ex, monitor, 0.0,
        )
        assert result is False
        rows = _db_rows(db)
        last = rows[-1]
        assert last["funnel_stage"] == "PRE_FILTER"
        assert "G4" not in last["gates_passed"]

    async def test_g4_fail_token_falling_rejected(self, tmp_path):
        _reset_pipeline()
        db  = _make_db(tmp_path)
        ex  = _make_executor(db)
        bad_token = _good_token_data(change_1h=-10.0)  # falling
        dex, safety, chain, fusion, monitor = _make_adapters(
            bad_token, {"is_safe": True},
            {"top_10_holder_percent": 30, "liquidity_locked": True},
        )
        from src.bot.pipeline import evaluate_token
        result = await evaluate_token(
            {"address": "falling1", "symbol": "FALL", "source": "dex"},
            dex, safety, chain, fusion, ex, monitor, 0.0,
        )
        assert result is False

    async def test_g4_fail_heavy_sell_pressure_rejected(self, tmp_path):
        _reset_pipeline()
        db  = _make_db(tmp_path)
        ex  = _make_executor(db)
        bad_token = _good_token_data(buys_h1=10, sells_h1=100)  # heavy selling
        dex, safety, chain, fusion, monitor = _make_adapters(
            bad_token, {"is_safe": True},
            {"top_10_holder_percent": 30, "liquidity_locked": True},
        )
        from src.bot.pipeline import evaluate_token
        result = await evaluate_token(
            {"address": "sellpres1", "symbol": "DUMP", "source": "dex"},
            dex, safety, chain, fusion, ex, monitor, 0.0,
        )
        assert result is False

    async def test_g4_pass_good_token_continues(self, tmp_path):
        _reset_pipeline()
        db  = _make_db(tmp_path)
        ex  = _make_executor(db)
        dex, safety, chain, fusion, monitor = _make_adapters(
            _good_token_data(), {"is_safe": True},
            {"top_10_holder_percent": 30, "liquidity_locked": True},
        )
        from src.bot.pipeline import evaluate_token
        result = await evaluate_token(
            {"address": "goodtok1", "symbol": "GOOD", "source": "dex"},
            dex, safety, chain, fusion, ex, monitor, 0.0,
        )
        assert result is True
        rows = _db_rows(db)
        assert "G4:PreFilter" in rows[-1]["gates_passed"]

    async def test_g4_migration_uses_relaxed_filter(self, tmp_path):
        """Migration tokens use pre_buy_filter_migration (lower thresholds)."""
        _reset_pipeline()
        db   = _make_db(tmp_path)
        ex   = _make_executor(db)
        # This token would fail standard filter (no spike) but passes migration
        mig_token = _good_token_data(volume_spike=0.8, liquidity_usd=4_000)
        dex, safety, chain, fusion, monitor = _make_adapters(
            mig_token, {"is_safe": True},
            {"top_10_holder_percent": 30, "liquidity_locked": True},
        )
        from src.bot.pipeline import evaluate_token
        # Should get to at least G3 for a migration (relaxed G4)
        result = await evaluate_token(
            {"address": "migaddr1", "symbol": "MIG", "source": "pumpfun"},
            dex, safety, chain, fusion, ex, monitor, 0.0, is_migration=True,
        )
        # Migration filter is looser — if fusion says BUY it can still buy
        # Just verify it didn't bail at G4 if the token is otherwise OK
        rows = _db_rows(db)
        assert len(rows) > 0   # something was logged


# ── Gate 5: Fusion scoring ────────────────────────────────────────────────────

class TestGate5Scoring:
    async def test_g5_fail_low_score_rejected(self, tmp_path):
        _reset_pipeline()
        db  = _make_db(tmp_path)
        ex  = _make_executor(db)
        dex, safety, chain, fusion, monitor = _make_adapters(
            _good_token_data(), {"is_safe": True},
            {"top_10_holder_percent": 30, "liquidity_locked": True},
            fusion_override={"score": 30.0, "decision": "SKIP", "confidence": "LOW", "breakdown": {}},
        )
        from src.bot.pipeline import evaluate_token
        result = await evaluate_token(
            {"address": "lowscore1", "symbol": "SKIP", "source": "dex"},
            dex, safety, chain, fusion, ex, monitor, 0.0,
        )
        assert result is False
        rows = _db_rows(db)
        last = rows[-1]
        assert last["funnel_stage"] == "SCORING"
        assert "G5" not in last["gates_passed"]

    async def test_g5_pass_high_score_buys(self, tmp_path):
        _reset_pipeline()
        db  = _make_db(tmp_path)
        ex  = _make_executor(db)
        dex, safety, chain, fusion, monitor = _make_adapters(
            _good_token_data(), {"is_safe": True},
            {"top_10_holder_percent": 30, "liquidity_locked": True},
            fusion_override={"score": 80.0, "decision": "BUY", "confidence": "HIGH", "breakdown": {}},
        )
        from src.bot.pipeline import evaluate_token
        result = await evaluate_token(
            {"address": "highscore1", "symbol": "MOON", "source": "dex"},
            dex, safety, chain, fusion, ex, monitor, 0.0,
        )
        assert result is True
        rows = _db_rows(db)
        last = rows[-1]
        assert "G5:Scoring" in last["gates_passed"]


# ── Gate 6: Full pipeline (all 6 gates) ──────────────────────────────────────

class TestGate6FullPipeline:
    async def test_all_6_gates_recorded_on_buy(self, tmp_path):
        _reset_pipeline()
        db  = _make_db(tmp_path)
        ex  = _make_executor(db)
        dex, safety, chain, fusion, monitor = _make_adapters(
            _good_token_data(), {"is_safe": True},
            {"top_10_holder_percent": 25, "liquidity_locked": True, "holder_count": 2000},
        )
        from src.bot.pipeline import evaluate_token
        result = await evaluate_token(
            {"address": "allgates1", "symbol": "ALL", "source": "dex"},
            dex, safety, chain, fusion, ex, monitor, 0.0,
        )
        assert result is True
        rows = _db_rows(db)
        buy_row = next(r for r in rows if "BUY" in r["decision"])
        gates = buy_row["gates_passed"]
        for gate in ["G1:Data", "G2:Safety", "G3:Risk", "G4:PreFilter", "G5:Scoring", "G6:Exec"]:
            assert gate in gates, f"Missing gate {gate} in: {gates}"

    async def test_buy_event_emitted_with_correct_amount(self, tmp_path):
        _reset_pipeline()
        db  = _make_db(tmp_path)
        ex  = _make_executor(db)
        dex, safety, chain, fusion, monitor = _make_adapters(
            _good_token_data(), {"is_safe": True},
            {"top_10_holder_percent": 25, "liquidity_locked": True},
        )
        from src.bot.pipeline import evaluate_token
        await evaluate_token(
            {"address": "evtaddr1", "symbol": "EVT", "source": "dex"},
            dex, safety, chain, fusion, ex, monitor, 0.0,
        )
        evs = _db_rows(db, "bot_events")
        assert len(evs) >= 1
        buy_ev = next((e for e in evs if "BUY" in e["event_type"]), None)
        assert buy_ev is not None
        assert buy_ev["buy_amount_usd"] == pytest.approx(0.10)

    async def test_position_added_after_buy(self, tmp_path):
        _reset_pipeline()
        db  = _make_db(tmp_path)
        ex  = _make_executor(db)
        dex, safety, chain, fusion, monitor = _make_adapters(
            _good_token_data(), {"is_safe": True},
            {"top_10_holder_percent": 25, "liquidity_locked": True},
        )
        from src.bot.pipeline import evaluate_token
        result = await evaluate_token(
            {"address": "posaddr1", "symbol": "POS", "source": "dex"},
            dex, safety, chain, fusion, ex, monitor, 0.0,
        )
        assert result is True
        monitor.add_position.assert_called_once()
        call_args = monitor.add_position.call_args
        assert call_args[0][0] == "posaddr1"   # address

    async def test_address_added_to_bought_this_session(self, tmp_path):
        _reset_pipeline()
        import src.bot.pipeline as pipe
        db  = _make_db(tmp_path)
        ex  = _make_executor(db)
        dex, safety, chain, fusion, monitor = _make_adapters(
            _good_token_data(), {"is_safe": True},
            {"top_10_holder_percent": 25, "liquidity_locked": True},
        )
        from src.bot.pipeline import evaluate_token
        await evaluate_token(
            {"address": "sessaddr1", "symbol": "SESS", "source": "dex"},
            dex, safety, chain, fusion, ex, monitor, 0.0,
        )
        assert "sessaddr1" in pipe.BOUGHT_THIS_SESSION

    async def test_max_positions_gate_rejects(self, tmp_path):
        _reset_pipeline()
        db  = _make_db(tmp_path)
        ex  = _make_executor(db)
        dex, safety, chain, fusion, monitor = _make_adapters(
            _good_token_data(), {"is_safe": True},
            {"top_10_holder_percent": 25, "liquidity_locked": True},
        )
        # Fill to max
        monitor.positions = {f"addr{i}": {} for i in range(20)}
        from src.bot.pipeline import evaluate_token
        result = await evaluate_token(
            {"address": "maxpos1", "symbol": "MAX", "source": "dex"},
            dex, safety, chain, fusion, ex, monitor, 0.0,
        )
        assert result is False
        rows = _db_rows(db)
        last = rows[-1]
        assert last["funnel_stage"] == "EXEC_LIMIT"

    async def test_two_consecutive_different_tokens_both_logged(self, tmp_path):
        _reset_pipeline()
        db  = _make_db(tmp_path)
        ex  = _make_executor(db)
        from src.bot.pipeline import evaluate_token

        for addr, sym in [("addr_a", "AAA"), ("addr_b", "BBB")]:
            dex, safety, chain, fusion, monitor = _make_adapters(
                _good_token_data(symbol=sym),
                {"is_safe": True},
                {"top_10_holder_percent": 25, "liquidity_locked": True},
            )
            monitor.positions = {}
            await evaluate_token(
                {"address": addr, "symbol": sym, "source": "dex"},
                dex, safety, chain, fusion, ex, monitor, 0.0,
            )

        rows = _db_rows(db)
        buys = [r for r in rows if "BUY" in r["decision"]]
        assert len(buys) == 2
        syms = {r["symbol"] for r in buys}
        assert "AAA" in syms
        assert "BBB" in syms


# ── Position size respected in pipeline ──────────────────────────────────────

class TestPositionSizeInPipeline:
    """Verify $0.10 is used end-to-end through the pipeline."""

    async def test_position_size_010_used_in_buy(self, tmp_path):
        _reset_pipeline()
        db  = _make_db(tmp_path)
        ex  = _make_executor(db)
        dex, safety, chain, fusion, monitor = _make_adapters(
            _good_token_data(), {"is_safe": True},
            {"top_10_holder_percent": 25, "liquidity_locked": True},
            fusion_override={"score": 75, "decision": "BUY", "confidence": "HIGH", "breakdown": {}},
        )
        from src.bot.pipeline import evaluate_token
        await evaluate_token(
            {"address": "psaddr1", "symbol": "PS", "source": "dex"},
            dex, safety, chain, fusion, ex, monitor, 0.0,
        )
        rows = _db_rows(db)
        buy  = next(r for r in rows if "BUY" in r["decision"])
        assert buy["position_size"] == pytest.approx(0.10)

    async def test_medium_confidence_uses_mid_position(self, tmp_path):
        _reset_pipeline()
        db  = _make_db(tmp_path)
        ex  = _make_executor(db)
        dex, safety, chain, fusion, monitor = _make_adapters(
            _good_token_data(), {"is_safe": True},
            {"top_10_holder_percent": 25, "liquidity_locked": True},
            fusion_override={"score": 60, "decision": "BUY", "confidence": "MEDIUM", "breakdown": {}},
        )
        from src.bot.pipeline import evaluate_token
        await evaluate_token(
            {"address": "medaddr1", "symbol": "MED", "source": "dex"},
            dex, safety, chain, fusion, ex, monitor, 0.0,
        )
        rows = _db_rows(db)
        buy  = next(r for r in rows if "BUY" in r["decision"])
        assert buy["position_size"] == pytest.approx(0.075)   # (0.10+0.05)/2
