"""
Data consistency tests for Positions ↔ History ↔ Events.

Unit tests: verify invariants using a controlled test DB
Integration tests: simulate full buy→sell lifecycle and check cross-table consistency

All tests are fully offline (mocked network, temp DB, temp positions.json).
"""
import os
import sys
import json
import sqlite3
import asyncio
import tempfile
from datetime import datetime, timedelta
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

from src.execution import events as _events


# ── Helpers ───────────────────────────────────────────────────────────────────

def _create_test_db(db_path: str):
    """Create a fresh DB with the full schema."""
    from src.database import init_db
    # Temporarily monkey-patch the hardcoded path
    orig_connect = sqlite3.connect
    captured = {}
    def patched_connect(path, *a, **kw):
        if path == "memecoin_bot.db":
            path = db_path
        return orig_connect(path, *a, **kw)
    with patch("sqlite3.connect", side_effect=patched_connect):
        init_db()
    _events.init(db_path)


def _make_executor(db_path: str):
    """Create a TradeExecutor that writes to the test DB (dry-run, no keypair)."""
    from src.execution.executor import TradeExecutor
    ex = TradeExecutor.__new__(TradeExecutor)
    ex.dry_run          = True
    ex.max_position_usd = 0.20
    ex.min_position_usd = 0.10
    ex.db_path          = db_path
    ex.http             = MagicMock()
    ex.keypair          = None
    # Reset migration cache for this DB
    TradeExecutor._migrated_dbs.discard(db_path)
    return ex


def _query(db_path: str, sql: str, params=()):
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    rows = conn.execute(sql, params).fetchall()
    conn.close()
    return [dict(r) for r in rows]


def _count(db_path: str, table: str, where: str = "", params=()):
    sql = f"SELECT COUNT(*) as n FROM {table}"
    if where:
        sql += f" WHERE {where}"
    return _query(db_path, sql, params)[0]["n"]


# ══════════════════════════════════════════════════════════════════════════════
# UNIT TESTS — History internal consistency
# ══════════════════════════════════════════════════════════════════════════════

class TestHistoryInternalConsistency:
    """Verify trades table data is self-consistent."""

    @pytest.fixture(autouse=True)
    def setup_db(self, tmp_path):
        self.db_path = str(tmp_path / "test.db")
        _create_test_db(self.db_path)
        self.executor = _make_executor(self.db_path)

    # ── BUY rows ──────────────────────────────────────────────────────────────

    @pytest.mark.asyncio
    async def test_buy_has_entry_price(self):
        """Every BUY must have entry_price > 0."""
        await self.executor.execute_trade(
            "TEST", "addr_aaa", score=80, decision="BUY",
            price=0.00012, funnel_stage="BUY_EXEC", confidence="HIGH",
            gates_passed="G1:Data,G2:Safety,G3:Risk,G4:PreFilter,G5:Scoring,G6:Exec",
        )
        rows = _query(self.db_path, "SELECT * FROM trades WHERE decision LIKE '%BUY%'")
        assert len(rows) == 1
        assert rows[0]["entry_price"] > 0, "BUY must have entry_price > 0"

    @pytest.mark.asyncio
    async def test_buy_has_all_gates(self):
        """BUY trades should have passed all 6 gates."""
        await self.executor.execute_trade(
            "TEST", "addr_bbb", score=75, decision="BUY",
            price=0.001, funnel_stage="BUY_EXEC", confidence="MEDIUM",
            gates_passed="G1:Data,G2:Safety,G3:Risk,G4:PreFilter,G5:Scoring,G6:Exec",
        )
        row = _query(self.db_path, "SELECT * FROM trades WHERE decision LIKE '%BUY%'")[0]
        gates = row["gates_passed"].split(",")
        assert len(gates) == 6, f"BUY should have all 6 gates, got {len(gates)}"
        for g in ["G1:Data", "G2:Safety", "G3:Risk", "G4:PreFilter", "G5:Scoring", "G6:Exec"]:
            assert g in row["gates_passed"], f"Missing gate {g}"

    @pytest.mark.asyncio
    async def test_buy_has_funnel_stage_buy_exec(self):
        """BUY rows should have funnel_stage = BUY_EXEC."""
        await self.executor.execute_trade(
            "TEST", "addr_ccc", score=82, decision="BUY",
            price=0.0005, funnel_stage="BUY_EXEC", confidence="HIGH",
            gates_passed="G1:Data,G2:Safety,G3:Risk,G4:PreFilter,G5:Scoring,G6:Exec",
        )
        row = _query(self.db_path, "SELECT * FROM trades WHERE decision LIKE '%BUY%'")[0]
        assert row["funnel_stage"] == "BUY_EXEC"

    @pytest.mark.asyncio
    async def test_buy_has_position_size(self):
        """BUY trades must have position_size > 0."""
        await self.executor.execute_trade(
            "TEST", "addr_ddd", score=90, decision="BUY",
            price=0.001, funnel_stage="BUY_EXEC", confidence="HIGH",
            gates_passed="G1:Data,G2:Safety,G3:Risk,G4:PreFilter,G5:Scoring,G6:Exec",
        )
        row = _query(self.db_path, "SELECT * FROM trades WHERE decision LIKE '%BUY%'")[0]
        assert row["position_size"] > 0, "BUY must have position_size > 0"

    # ── REJECT rows ───────────────────────────────────────────────────────────

    @pytest.mark.asyncio
    async def test_reject_has_rejection_reason(self):
        """REJECT decisions must have a rejection_reason."""
        await self.executor.execute_trade(
            "SCAM", "addr_eee", score=20, decision="REJECT",
            price=0.0001, rejection_reason="Safety fail: mint authority",
            funnel_stage="SAFETY_CHECK", confidence="LOW",
            gates_passed="G1:Data",
        )
        row = _query(self.db_path, "SELECT * FROM trades WHERE decision='REJECT'")[0]
        assert row["rejection_reason"] is not None and len(row["rejection_reason"]) > 0

    @pytest.mark.asyncio
    async def test_reject_has_fewer_gates_than_buy(self):
        """REJECT should not pass all 6 gates."""
        await self.executor.execute_trade(
            "SCAM", "addr_fff", score=30, decision="REJECT",
            price=0.0001, rejection_reason="Low liquidity",
            funnel_stage="PRE_FILTER", confidence="LOW",
            gates_passed="G1:Data,G2:Safety,G3:Risk",
        )
        row = _query(self.db_path, "SELECT * FROM trades WHERE decision='REJECT'")[0]
        gates = [g for g in (row["gates_passed"] or "").split(",") if g.strip()]
        assert len(gates) < 6, "REJECT should fail before passing all gates"

    # ── Score / confidence ────────────────────────────────────────────────────

    @pytest.mark.asyncio
    async def test_buy_decision_stored_with_simulated_suffix(self):
        """Dry-run BUY should be stored as 'BUY (SIMULATED)'."""
        await self.executor.execute_trade(
            "TEST", "addr_ggg", score=80, decision="BUY",
            price=0.001, funnel_stage="BUY_EXEC", confidence="HIGH",
            gates_passed="G1:Data,G2:Safety,G3:Risk,G4:PreFilter,G5:Scoring,G6:Exec",
        )
        row = _query(self.db_path, "SELECT * FROM trades")[0]
        assert "BUY" in row["decision"]
        assert "SIMULATED" in row["decision"], "Dry-run BUY should say SIMULATED"

    @pytest.mark.asyncio
    async def test_sell_decision_stored_with_simulated_suffix(self):
        """Dry-run SELL should be stored as 'SELL (SIMULATED)'."""
        await self.executor.execute_trade(
            "TEST", "addr_hhh", score=0, decision="SELL",
            price=0.002, funnel_stage="STOP_LOSS", confidence="HIGH",
            rejection_reason="Stop-Loss -20%",
            gates_passed="G1:Data,G2:Safety,G3:Risk,G4:PreFilter,G5:Scoring,G6:Exec,POSITION",
        )
        row = _query(self.db_path, "SELECT * FROM trades")[0]
        assert "SELL" in row["decision"]
        assert "SIMULATED" in row["decision"]

    # ── Extended data columns ─────────────────────────────────────────────────

    @pytest.mark.asyncio
    async def test_extra_data_persisted(self):
        """Extra market/safety data passed via extra dict should be stored."""
        extra = {
            "source": "BOOSTED_TOP",
            "market_cap": 200000,
            "liquidity_usd": 50000,
            "change_5m": 7.3,
            "change_1h": 107.0,
            "rugcheck_score": 850,
            "holder_count": 20,
        }
        await self.executor.execute_trade(
            "MILKA", "addr_iii", score=82.5, decision="BUY",
            price=0.00020760, funnel_stage="BUY_EXEC", confidence="HIGH",
            gates_passed="G1:Data,G2:Safety,G3:Risk,G4:PreFilter,G5:Scoring,G6:Exec",
            extra=extra,
        )
        row = _query(self.db_path, "SELECT * FROM trades WHERE decision LIKE '%BUY%'")[0]
        assert row["source"] == "BOOSTED_TOP"
        assert row["market_cap"] == 200000
        assert row["liquidity_usd"] == 50000
        assert row["change_5m"] == pytest.approx(7.3)
        assert row["rugcheck_score"] == 850
        assert row["holder_count"] == 20

    # ── Timestamp ordering ────────────────────────────────────────────────────

    @pytest.mark.asyncio
    async def test_timestamps_are_chronological(self):
        """Multiple trades inserted sequentially should have increasing timestamps."""
        for i, addr in enumerate(["addr_j1", "addr_j2", "addr_j3"]):
            await self.executor.execute_trade(
                f"T{i}", addr, score=50+i, decision="REJECT",
                price=0.001, rejection_reason="test",
                funnel_stage="SCORING", confidence="LOW",
                gates_passed="G1:Data,G2:Safety,G3:Risk,G4:PreFilter,G5:Scoring",
            )
        rows = _query(self.db_path, "SELECT timestamp FROM trades ORDER BY id")
        assert len(rows) == 3
        for i in range(len(rows) - 1):
            assert rows[i]["timestamp"] <= rows[i + 1]["timestamp"]


# ══════════════════════════════════════════════════════════════════════════════
# UNIT TESTS — BUY ↔ SELL cross-reference
# ══════════════════════════════════════════════════════════════════════════════

class TestBuySellCrossRef:
    """Verify BUY and SELL trades for the same token are consistent."""

    @pytest.fixture(autouse=True)
    def setup_db(self, tmp_path):
        self.db_path = str(tmp_path / "test.db")
        _create_test_db(self.db_path)
        self.executor = _make_executor(self.db_path)

    @pytest.mark.asyncio
    async def test_sell_has_matching_buy(self):
        """A SELL for a token should follow a BUY for the same token."""
        addr = "addr_sell1"
        # BUY first
        await self.executor.execute_trade(
            "TOKEN", addr, score=80, decision="BUY", price=0.001,
            funnel_stage="BUY_EXEC", confidence="HIGH",
            gates_passed="G1:Data,G2:Safety,G3:Risk,G4:PreFilter,G5:Scoring,G6:Exec",
        )
        # Then SELL
        await self.executor.execute_trade(
            "TOKEN", addr, score=0, decision="SELL", price=0.002,
            funnel_stage="STOP_LOSS", rejection_reason="Stop-Loss -20%",
            gates_passed="G1:Data,G2:Safety,G3:Risk,G4:PreFilter,G5:Scoring,G6:Exec,POSITION",
        )
        buys  = _query(self.db_path, "SELECT * FROM trades WHERE decision LIKE '%BUY%' AND token_address=?", (addr,))
        sells = _query(self.db_path, "SELECT * FROM trades WHERE decision LIKE '%SELL%' AND token_address=?", (addr,))
        assert len(buys) >= 1, "SELL should have a preceding BUY"
        assert len(sells) >= 1
        # SELL timestamp should be after BUY
        assert sells[0]["timestamp"] >= buys[0]["timestamp"]

    @pytest.mark.asyncio
    async def test_sell_price_different_from_buy_price(self):
        """Sell price should be stored independently from buy price."""
        addr = "addr_sell2"
        buy_price  = 0.001
        sell_price = 0.0015  # +50%
        await self.executor.execute_trade(
            "TOKEN", addr, score=80, decision="BUY", price=buy_price,
            funnel_stage="BUY_EXEC", confidence="HIGH",
            gates_passed="G1:Data,G2:Safety,G3:Risk,G4:PreFilter,G5:Scoring,G6:Exec",
        )
        await self.executor.execute_trade(
            "TOKEN", addr, score=0, decision="SELL", price=sell_price,
            funnel_stage="TP1", rejection_reason="Take-Profit 1 (+50%)",
            gates_passed="G1:Data,G2:Safety,G3:Risk,G4:PreFilter,G5:Scoring,G6:Exec,POSITION",
        )
        buy_row  = _query(self.db_path, "SELECT * FROM trades WHERE decision LIKE '%BUY%' AND token_address=?", (addr,))[0]
        sell_row = _query(self.db_path, "SELECT * FROM trades WHERE decision LIKE '%SELL%' AND token_address=?", (addr,))[0]
        assert buy_row["entry_price"] == pytest.approx(buy_price)
        assert sell_row["entry_price"] == pytest.approx(sell_price)
        assert sell_row["entry_price"] != buy_row["entry_price"]

    @pytest.mark.asyncio
    async def test_multiple_sells_for_partial_tp(self):
        """Token can have multiple SELL rows (TP1 + TP2 + TP3)."""
        addr = "addr_sell3"
        await self.executor.execute_trade(
            "TOKEN", addr, score=80, decision="BUY", price=0.001,
            funnel_stage="BUY_EXEC", confidence="HIGH",
            gates_passed="G1:Data,G2:Safety,G3:Risk,G4:PreFilter,G5:Scoring,G6:Exec",
        )
        for stage, price in [("TP1", 0.0015), ("TP2", 0.002), ("TP3", 0.003)]:
            await self.executor.execute_trade(
                "TOKEN", addr, score=0, decision="SELL", price=price,
                funnel_stage=stage, rejection_reason=f"Take-Profit {stage}",
                gates_passed="G1:Data,G2:Safety,G3:Risk,G4:PreFilter,G5:Scoring,G6:Exec,POSITION",
                sell_fraction=0.5 if stage != "TP3" else 1.0,
            )
        sells = _query(self.db_path,
            "SELECT * FROM trades WHERE decision LIKE '%SELL%' AND token_address=? ORDER BY id",
            (addr,))
        assert len(sells) == 3, "Should have 3 partial sell rows"
        # Each sell price should be higher (TP1 < TP2 < TP3)
        prices = [s["entry_price"] for s in sells]
        assert prices == sorted(prices), "TP sell prices should be increasing"


# ══════════════════════════════════════════════════════════════════════════════
# UNIT TESTS — bot_events ↔ trades consistency
# ══════════════════════════════════════════════════════════════════════════════

class TestEventsTradesConsistency:
    """Verify bot_events match corresponding trades rows."""

    @pytest.fixture(autouse=True)
    def setup_db(self, tmp_path):
        self.db_path = str(tmp_path / "test.db")
        _create_test_db(self.db_path)
        self.executor = _make_executor(self.db_path)
        # Point events module to our test DB
        _events._DB_PATH = self.db_path

    @pytest.mark.asyncio
    async def test_buy_creates_event(self):
        """A BUY trade should emit a BUY_SIMULATED event."""
        await self.executor.execute_trade(
            "TEST", "addr_ev1", score=80, decision="BUY", price=0.001,
            funnel_stage="BUY_EXEC", confidence="HIGH",
            gates_passed="G1:Data,G2:Safety,G3:Risk,G4:PreFilter,G5:Scoring,G6:Exec",
        )
        events = _query(self.db_path,
            "SELECT * FROM bot_events WHERE event_type='BUY_SIMULATED' AND address=?",
            ("addr_ev1",))
        assert len(events) == 1, "BUY should create exactly one BUY_SIMULATED event"
        assert events[0]["symbol"] == "TEST"
        assert events[0]["price_usd"] == pytest.approx(0.001)

    @pytest.mark.asyncio
    async def test_sell_creates_event(self):
        """A SELL trade should emit a SELL_SIMULATED event."""
        await self.executor.execute_trade(
            "TEST", "addr_ev2", score=0, decision="SELL", price=0.002,
            funnel_stage="STOP_LOSS", confidence="HIGH",
            rejection_reason="Stop-Loss",
            gates_passed="G1:Data,G2:Safety,G3:Risk,G4:PreFilter,G5:Scoring,G6:Exec,POSITION",
        )
        events = _query(self.db_path,
            "SELECT * FROM bot_events WHERE event_type='SELL_SIMULATED' AND address=?",
            ("addr_ev2",))
        assert len(events) == 1

    @pytest.mark.asyncio
    async def test_reject_does_not_create_event(self):
        """REJECT decisions should NOT emit an event."""
        await self.executor.execute_trade(
            "SCAM", "addr_ev3", score=20, decision="REJECT", price=0.0001,
            rejection_reason="Rug detected", funnel_stage="SAFETY_CHECK",
            gates_passed="G1:Data",
        )
        events = _query(self.db_path,
            "SELECT * FROM bot_events WHERE address=?", ("addr_ev3",))
        assert len(events) == 0

    @pytest.mark.asyncio
    async def test_event_address_matches_trade(self):
        """Event address should exactly match the trade's token_address."""
        addr = "So11111111111111111111111111111111111111FAKE"
        await self.executor.execute_trade(
            "FAKE", addr, score=85, decision="BUY", price=0.005,
            funnel_stage="BUY_EXEC", confidence="HIGH",
            gates_passed="G1:Data,G2:Safety,G3:Risk,G4:PreFilter,G5:Scoring,G6:Exec",
        )
        trade = _query(self.db_path, "SELECT * FROM trades WHERE decision LIKE '%BUY%'")[0]
        event = _query(self.db_path, "SELECT * FROM bot_events WHERE event_type='BUY_SIMULATED'")[0]
        assert trade["token_address"] == event["address"]
        assert trade["symbol"] == event["symbol"]

    @pytest.mark.asyncio
    async def test_event_count_matches_trade_count(self):
        """Number of BUY events should equal number of BUY trades."""
        for i in range(5):
            await self.executor.execute_trade(
                f"T{i}", f"addr_ev_{i}", score=80, decision="BUY", price=0.001,
                funnel_stage="BUY_EXEC", confidence="HIGH",
                gates_passed="G1:Data,G2:Safety,G3:Risk,G4:PreFilter,G5:Scoring,G6:Exec",
            )
        buy_trades = _count(self.db_path, "trades", "decision LIKE '%BUY%'")
        buy_events = _count(self.db_path, "bot_events", "event_type='BUY_SIMULATED'")
        assert buy_trades == buy_events == 5


# ══════════════════════════════════════════════════════════════════════════════
# UNIT TESTS — Positions ↔ History consistency
# ══════════════════════════════════════════════════════════════════════════════

class TestPositionsTradesConsistency:
    """Verify positions.json is consistent with trades DB."""

    @pytest.fixture(autouse=True)
    def setup(self, tmp_path):
        self.db_path   = str(tmp_path / "test.db")
        self.pos_file  = str(tmp_path / "positions.json")
        _create_test_db(self.db_path)
        self.executor = _make_executor(self.db_path)

    def _write_positions(self, pos: dict):
        with open(self.pos_file, "w") as f:
            json.dump(pos, f)

    def _read_positions(self) -> dict:
        with open(self.pos_file) as f:
            return json.load(f)

    @pytest.mark.asyncio
    async def test_position_has_matching_buy_in_db(self):
        """Every position should have a BUY row in trades."""
        addr = "addr_pos1"
        buy_price = 0.00012

        # Record BUY in DB
        await self.executor.execute_trade(
            "MILKA", addr, score=82, decision="BUY", price=buy_price,
            funnel_stage="BUY_EXEC", confidence="HIGH",
            gates_passed="G1:Data,G2:Safety,G3:Risk,G4:PreFilter,G5:Scoring,G6:Exec",
        )
        # Create positions.json entry (as monitor would)
        self._write_positions({
            addr: {
                "symbol":       "MILKA",
                "entry_price":  buy_price,
                "created_at":   1713042038,
                "remaining_pct": 1.0,
                "tp1_hit": False, "tp2_hit": False, "tp3_hit": False,
                "highest_price": buy_price,
                "trailing_active": False,
            }
        })

        positions = self._read_positions()
        for addr, pos in positions.items():
            buys = _query(self.db_path,
                "SELECT * FROM trades WHERE decision LIKE '%BUY%' AND token_address=?",
                (addr,))
            assert len(buys) >= 1, f"Position {addr} has no BUY in trades"

    @pytest.mark.asyncio
    async def test_position_entry_price_matches_buy(self):
        """Position entry_price should match the BUY trade entry_price."""
        addr = "addr_pos2"
        buy_price = 0.00034567

        await self.executor.execute_trade(
            "TOKEN2", addr, score=75, decision="BUY", price=buy_price,
            funnel_stage="BUY_EXEC", confidence="MEDIUM",
            gates_passed="G1:Data,G2:Safety,G3:Risk,G4:PreFilter,G5:Scoring,G6:Exec",
        )
        self._write_positions({
            addr: {
                "symbol": "TOKEN2", "entry_price": buy_price,
                "created_at": 1713042038, "remaining_pct": 1.0,
                "tp1_hit": False, "tp2_hit": False, "tp3_hit": False,
                "highest_price": buy_price, "trailing_active": False,
            }
        })

        positions = self._read_positions()
        buy_row = _query(self.db_path,
            "SELECT entry_price FROM trades WHERE decision LIKE '%BUY%' AND token_address=?",
            (addr,))[0]
        assert positions[addr]["entry_price"] == pytest.approx(buy_row["entry_price"])

    @pytest.mark.asyncio
    async def test_position_symbol_matches_buy(self):
        """Position symbol should match the BUY trade symbol."""
        addr = "addr_pos3"
        await self.executor.execute_trade(
            "DOGE2", addr, score=80, decision="BUY", price=0.005,
            funnel_stage="BUY_EXEC", confidence="HIGH",
            gates_passed="G1:Data,G2:Safety,G3:Risk,G4:PreFilter,G5:Scoring,G6:Exec",
        )
        self._write_positions({
            addr: {
                "symbol": "DOGE2", "entry_price": 0.005,
                "created_at": 1713042038, "remaining_pct": 1.0,
                "tp1_hit": False, "tp2_hit": False, "tp3_hit": False,
                "highest_price": 0.005, "trailing_active": False,
            }
        })

        positions = self._read_positions()
        buy_row = _query(self.db_path,
            "SELECT symbol FROM trades WHERE decision LIKE '%BUY%' AND token_address=?",
            (addr,))[0]
        assert positions[addr]["symbol"] == buy_row["symbol"]

    @pytest.mark.asyncio
    async def test_closed_position_has_sell_in_db(self):
        """A fully sold position should have both BUY and SELL rows."""
        addr = "addr_pos4"

        # BUY
        await self.executor.execute_trade(
            "CLOSED", addr, score=80, decision="BUY", price=0.001,
            funnel_stage="BUY_EXEC", confidence="HIGH",
            gates_passed="G1:Data,G2:Safety,G3:Risk,G4:PreFilter,G5:Scoring,G6:Exec",
        )
        # SELL (stop-loss)
        await self.executor.execute_trade(
            "CLOSED", addr, score=0, decision="SELL", price=0.0008,
            funnel_stage="STOP_LOSS", rejection_reason="Stop-Loss -20%",
            gates_passed="G1:Data,G2:Safety,G3:Risk,G4:PreFilter,G5:Scoring,G6:Exec,POSITION",
        )

        buys  = _count(self.db_path, "trades", "decision LIKE '%BUY%' AND token_address=?", (addr,))
        sells = _count(self.db_path, "trades", "decision LIKE '%SELL%' AND token_address=?", (addr,))
        assert buys >= 1 and sells >= 1, "Closed position should have BUY + SELL"

    @pytest.mark.asyncio
    async def test_position_remaining_pct_consistent_after_partial_sell(self):
        """After TP1 (sell 50%), remaining_pct should be ~0.5."""
        addr = "addr_pos5"

        await self.executor.execute_trade(
            "PARTIAL", addr, score=80, decision="BUY", price=0.001,
            funnel_stage="BUY_EXEC", confidence="HIGH",
            gates_passed="G1:Data,G2:Safety,G3:Risk,G4:PreFilter,G5:Scoring,G6:Exec",
        )
        # TP1 sell 50%
        await self.executor.execute_trade(
            "PARTIAL", addr, score=0, decision="SELL", price=0.0015,
            funnel_stage="TP1", rejection_reason="Take-Profit 1",
            sell_fraction=0.5,
            gates_passed="G1:Data,G2:Safety,G3:Risk,G4:PreFilter,G5:Scoring,G6:Exec,POSITION",
        )

        # Simulate what monitor does: update remaining_pct
        self._write_positions({
            addr: {
                "symbol": "PARTIAL", "entry_price": 0.001,
                "created_at": 1713042038,
                "remaining_pct": 0.5,  # after 50% sell
                "tp1_hit": True, "tp2_hit": False, "tp3_hit": False,
                "highest_price": 0.0015, "trailing_active": False,
            }
        })

        pos = self._read_positions()
        sells = _query(self.db_path,
            "SELECT * FROM trades WHERE decision LIKE '%SELL%' AND token_address=?",
            (addr,))
        assert len(sells) == 1, "Should have 1 sell after TP1"
        assert pos[addr]["tp1_hit"] is True
        assert pos[addr]["remaining_pct"] == pytest.approx(0.5)


# ══════════════════════════════════════════════════════════════════════════════
# INTEGRATION TESTS — Full buy→sell lifecycle
# ══════════════════════════════════════════════════════════════════════════════

class TestFullLifecycleIntegration:
    """
    Simulate the full pipeline → executor → monitor → sell lifecycle.
    After each lifecycle, verify all three data stores are consistent:
      - trades table (BUY + SELL rows)
      - bot_events table (events)
      - positions.json (position state)
    """

    @pytest.fixture(autouse=True)
    def setup(self, tmp_path):
        self.db_path  = str(tmp_path / "test.db")
        self.pos_file = str(tmp_path / "positions.json")
        _create_test_db(self.db_path)
        self.executor = _make_executor(self.db_path)
        _events._DB_PATH = self.db_path
        # Start with empty positions
        with open(self.pos_file, "w") as f:
            json.dump({}, f)

    def _positions(self) -> dict:
        with open(self.pos_file) as f:
            return json.load(f)

    def _update_positions(self, pos: dict):
        with open(self.pos_file, "w") as f:
            json.dump(pos, f)

    @pytest.mark.asyncio
    async def test_buy_reject_lifecycle(self):
        """
        Lifecycle: pipeline evaluates 5 tokens, 3 rejected + 2 bought.
        Verify: counts, events, no orphaned data.
        """
        rejected_addrs = ["rej1", "rej2", "rej3"]
        bought_addrs   = ["buy1", "buy2"]

        # Rejections
        for addr in rejected_addrs:
            await self.executor.execute_trade(
                f"R-{addr}", addr, score=30, decision="REJECT",
                price=0.0001, rejection_reason="Safety fail",
                funnel_stage="SAFETY_CHECK", gates_passed="G1:Data",
            )

        # Buys
        positions = {}
        for i, addr in enumerate(bought_addrs):
            price = 0.001 * (i + 1)
            await self.executor.execute_trade(
                f"B-{addr}", addr, score=80 + i, decision="BUY",
                price=price, funnel_stage="BUY_EXEC", confidence="HIGH",
                gates_passed="G1:Data,G2:Safety,G3:Risk,G4:PreFilter,G5:Scoring,G6:Exec",
            )
            positions[addr] = {
                "symbol": f"B-{addr}", "entry_price": price,
                "created_at": 1713042038 + i, "remaining_pct": 1.0,
                "tp1_hit": False, "tp2_hit": False, "tp3_hit": False,
                "highest_price": price, "trailing_active": False,
            }
        self._update_positions(positions)

        # ── Verify consistency ────────────────────────────────────────────
        total_trades = _count(self.db_path, "trades")
        assert total_trades == 5, f"Expected 5 trades (3 rej + 2 buy), got {total_trades}"

        buy_trades  = _count(self.db_path, "trades", "decision LIKE '%BUY%'")
        rej_trades  = _count(self.db_path, "trades", "decision = 'REJECT'")
        assert buy_trades == 2
        assert rej_trades == 3

        # Events: only BUY trades should emit events
        buy_events = _count(self.db_path, "bot_events", "event_type='BUY_SIMULATED'")
        assert buy_events == 2

        # Positions match buys
        pos = self._positions()
        assert len(pos) == 2
        for addr in bought_addrs:
            assert addr in pos

    @pytest.mark.asyncio
    async def test_buy_then_stop_loss(self):
        """
        Lifecycle: BUY → price drops → SELL (stop-loss).
        Verify all 3 data stores are consistent.
        """
        addr = "lifecycle_sl"
        sym  = "STOPPER"
        buy_price  = 0.001
        sell_price = 0.0008  # -20%

        # BUY
        await self.executor.execute_trade(
            sym, addr, score=80, decision="BUY", price=buy_price,
            funnel_stage="BUY_EXEC", confidence="HIGH",
            gates_passed="G1:Data,G2:Safety,G3:Risk,G4:PreFilter,G5:Scoring,G6:Exec",
        )
        self._update_positions({
            addr: {
                "symbol": sym, "entry_price": buy_price,
                "created_at": 1713042038, "remaining_pct": 1.0,
                "tp1_hit": False, "tp2_hit": False, "tp3_hit": False,
                "highest_price": buy_price, "trailing_active": False,
            }
        })

        # Verify BUY is in all 3 stores
        assert _count(self.db_path, "trades", "decision LIKE '%BUY%' AND token_address=?", (addr,)) == 1
        assert _count(self.db_path, "bot_events", "event_type='BUY_SIMULATED' AND address=?", (addr,)) == 1
        assert addr in self._positions()

        # SELL (stop-loss)
        await self.executor.execute_trade(
            sym, addr, score=0, decision="SELL", price=sell_price,
            funnel_stage="STOP_LOSS", rejection_reason="Stop-Loss -20%",
            gates_passed="G1:Data,G2:Safety,G3:Risk,G4:PreFilter,G5:Scoring,G6:Exec,POSITION",
        )
        # Simulate monitor removing position
        self._update_positions({})

        # Verify SELL is consistent
        sells = _query(self.db_path, "SELECT * FROM trades WHERE decision LIKE '%SELL%' AND token_address=?", (addr,))
        assert len(sells) == 1
        assert sells[0]["entry_price"] == pytest.approx(sell_price)
        assert sells[0]["funnel_stage"] == "STOP_LOSS"

        # Verify events
        sell_events = _query(self.db_path,
            "SELECT * FROM bot_events WHERE event_type='SELL_SIMULATED' AND address=?",
            (addr,))
        assert len(sell_events) == 1

        # Position should be gone
        assert addr not in self._positions()

    @pytest.mark.asyncio
    async def test_buy_then_tp1_tp2_tp3_full_lifecycle(self):
        """
        Full TP lifecycle: BUY → TP1 (sell 50%) → TP2 (sell 25%) → TP3 (sell rest).
        Verify remaining_pct tracking and all sells recorded.
        """
        addr = "lifecycle_tp"
        sym  = "MOONER"
        buy_price = 0.001

        # BUY
        await self.executor.execute_trade(
            sym, addr, score=90, decision="BUY", price=buy_price,
            funnel_stage="BUY_EXEC", confidence="HIGH",
            gates_passed="G1:Data,G2:Safety,G3:Risk,G4:PreFilter,G5:Scoring,G6:Exec",
        )
        pos = {
            addr: {
                "symbol": sym, "entry_price": buy_price,
                "created_at": 1713042038, "remaining_pct": 1.0,
                "tp1_hit": False, "tp2_hit": False, "tp3_hit": False,
                "highest_price": buy_price, "trailing_active": False,
            }
        }
        self._update_positions(pos)

        # TP1: sell 50%, price = +50%
        tp1_price = buy_price * 1.5
        await self.executor.execute_trade(
            sym, addr, score=0, decision="SELL", price=tp1_price,
            funnel_stage="TP1", rejection_reason="Take-Profit 1 (+50%)",
            sell_fraction=0.5,
            gates_passed="G1:Data,G2:Safety,G3:Risk,G4:PreFilter,G5:Scoring,G6:Exec,POSITION",
        )
        pos[addr]["tp1_hit"] = True
        pos[addr]["remaining_pct"] = 0.5
        self._update_positions(pos)

        # TP2: sell 25% of original, price = +100%
        tp2_price = buy_price * 2.0
        await self.executor.execute_trade(
            sym, addr, score=0, decision="SELL", price=tp2_price,
            funnel_stage="TP2", rejection_reason="Take-Profit 2 (+100%)",
            sell_fraction=0.5,  # 50% of remaining (= 25% of original)
            gates_passed="G1:Data,G2:Safety,G3:Risk,G4:PreFilter,G5:Scoring,G6:Exec,POSITION",
        )
        pos[addr]["tp2_hit"] = True
        pos[addr]["remaining_pct"] = 0.25
        self._update_positions(pos)

        # TP3: sell remaining, price = +200%
        tp3_price = buy_price * 3.0
        await self.executor.execute_trade(
            sym, addr, score=0, decision="SELL", price=tp3_price,
            funnel_stage="TP3", rejection_reason="Take-Profit 3 (+200%)",
            sell_fraction=1.0,
            gates_passed="G1:Data,G2:Safety,G3:Risk,G4:PreFilter,G5:Scoring,G6:Exec,POSITION",
        )
        # Position closed
        self._update_positions({})

        # ── Verify full consistency ───────────────────────────────────────
        # 1 BUY + 3 SELLs = 4 trades
        assert _count(self.db_path, "trades") == 4

        buys  = _query(self.db_path, "SELECT * FROM trades WHERE decision LIKE '%BUY%' AND token_address=?", (addr,))
        sells = _query(self.db_path, "SELECT * FROM trades WHERE decision LIKE '%SELL%' AND token_address=? ORDER BY id", (addr,))
        assert len(buys) == 1
        assert len(sells) == 3

        # Sell prices should be increasing (TP1 < TP2 < TP3)
        sell_prices = [s["entry_price"] for s in sells]
        assert sell_prices == sorted(sell_prices)

        # Stages should match
        assert sells[0]["funnel_stage"] == "TP1"
        assert sells[1]["funnel_stage"] == "TP2"
        assert sells[2]["funnel_stage"] == "TP3"

        # All sell timestamps after buy timestamp
        buy_ts = buys[0]["timestamp"]
        for s in sells:
            assert s["timestamp"] >= buy_ts

        # Events: 1 BUY + 3 SELL events
        events = _query(self.db_path, "SELECT * FROM bot_events WHERE address=? ORDER BY id", (addr,))
        assert len(events) == 4  # BUY_SIMULATED + 3x SELL_SIMULATED
        assert events[0]["event_type"] == "BUY_SIMULATED"
        for e in events[1:]:
            assert e["event_type"] == "SELL_SIMULATED"

        # Position should be fully closed
        assert addr not in self._positions()

    @pytest.mark.asyncio
    async def test_mixed_batch_consistency(self):
        """
        Batch of 10 tokens: 6 rejected, 2 bought, 2 skipped.
        Verify total counts across all tables are consistent.
        """
        # 6 rejects
        for i in range(6):
            await self.executor.execute_trade(
                f"REJ{i}", f"rej_{i}", score=20+i*5, decision="REJECT",
                price=0.0001, rejection_reason=f"Fail reason {i}",
                funnel_stage="SAFETY_CHECK", gates_passed="G1:Data",
            )
        # 2 skips
        for i in range(2):
            await self.executor.execute_trade(
                f"SKIP{i}", f"skip_{i}", score=45+i, decision="SKIP",
                price=0.0001, rejection_reason="Score too low",
                funnel_stage="SCORING", gates_passed="G1:Data,G2:Safety,G3:Risk,G4:PreFilter,G5:Scoring",
            )
        # 2 buys
        positions = {}
        for i in range(2):
            addr = f"bought_{i}"
            price = 0.001 * (i + 1)
            await self.executor.execute_trade(
                f"BUY{i}", addr, score=80+i, decision="BUY",
                price=price, funnel_stage="BUY_EXEC", confidence="HIGH",
                gates_passed="G1:Data,G2:Safety,G3:Risk,G4:PreFilter,G5:Scoring,G6:Exec",
            )
            positions[addr] = {
                "symbol": f"BUY{i}", "entry_price": price,
                "created_at": 1713042038, "remaining_pct": 1.0,
                "tp1_hit": False, "tp2_hit": False, "tp3_hit": False,
                "highest_price": price, "trailing_active": False,
            }
        self._update_positions(positions)

        # ── Verify totals ─────────────────────────────────────────────────
        total = _count(self.db_path, "trades")
        assert total == 10

        rejects = _count(self.db_path, "trades", "decision='REJECT'")
        skips   = _count(self.db_path, "trades", "decision='SKIP'")
        buys    = _count(self.db_path, "trades", "decision LIKE '%BUY%'")
        assert rejects == 6
        assert skips == 2
        assert buys == 2

        # Only BUY events should exist (no events for REJECT/SKIP)
        all_events = _count(self.db_path, "bot_events")
        assert all_events == 2  # 2 BUY_SIMULATED events

        # Positions match
        pos = self._positions()
        assert len(pos) == 2

        # Every position should have a matching BUY trade
        for addr in pos:
            buy_rows = _query(self.db_path,
                "SELECT * FROM trades WHERE decision LIKE '%BUY%' AND token_address=?",
                (addr,))
            assert len(buy_rows) == 1

    @pytest.mark.asyncio
    async def test_no_orphan_sells_without_buys(self):
        """Verify we can detect if a SELL exists without a prior BUY (data anomaly)."""
        # Manually insert a SELL without a BUY (simulates data corruption)
        conn = sqlite3.connect(self.db_path)
        conn.execute(
            "INSERT INTO trades (token_address, symbol, entry_price, decision, funnel_stage, timestamp) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            ("orphan_addr", "ORPHAN", 0.002, "SELL (SIMULATED)", "STOP_LOSS", datetime.now().isoformat()),
        )
        conn.commit()
        conn.close()

        # Check for orphaned sells
        sells = _query(self.db_path,
            "SELECT DISTINCT token_address FROM trades WHERE decision LIKE '%SELL%'")
        orphans = []
        for sell in sells:
            addr = sell["token_address"]
            buy_count = _count(self.db_path, "trades",
                "decision LIKE '%BUY%' AND token_address=?", (addr,))
            if buy_count == 0:
                orphans.append(addr)

        assert len(orphans) == 1
        assert orphans[0] == "orphan_addr"

    @pytest.mark.asyncio
    async def test_history_dashboard_query_matches_db(self):
        """
        Simulate the exact SQL query used by the History tab and verify
        it returns the same data as direct DB access.
        """
        # Insert some test trades
        await self.executor.execute_trade(
            "DASH1", "dash_addr1", score=80, decision="BUY", price=0.001,
            funnel_stage="BUY_EXEC", confidence="HIGH",
            gates_passed="G1:Data,G2:Safety,G3:Risk,G4:PreFilter,G5:Scoring,G6:Exec",
        )
        await self.executor.execute_trade(
            "DASH2", "dash_addr2", score=25, decision="REJECT", price=0.0001,
            rejection_reason="Scam token", funnel_stage="SAFETY_CHECK",
            gates_passed="G1:Data",
        )

        # This is the exact query from history.py render()
        history_rows = _query(self.db_path,
            """SELECT id, symbol, token_address, entry_price, position_size,
                      buy_amount_usd, sell_amount_usd,
                      score, decision, rejection_reason, ai_reasoning,
                      funnel_stage, gates_passed, timestamp, tx_signature, tx_status
               FROM trades ORDER BY timestamp DESC LIMIT 50""")

        all_rows = _query(self.db_path, "SELECT * FROM trades ORDER BY timestamp DESC")

        assert len(history_rows) == len(all_rows) == 2
        # Verify data matches
        assert history_rows[0]["symbol"] == all_rows[0]["symbol"]
        assert history_rows[0]["token_address"] == all_rows[0]["token_address"]
        assert history_rows[0]["decision"] == all_rows[0]["decision"]

    @pytest.mark.asyncio
    async def test_positions_summary_kpi_matches_trades(self):
        """
        Verify the Positions tab's summary KPI query returns values
        consistent with the actual trades data.
        """
        # 3 buys, 1 sell
        for i in range(3):
            await self.executor.execute_trade(
                f"KPI{i}", f"kpi_addr{i}", score=80, decision="BUY",
                price=0.001*(i+1), funnel_stage="BUY_EXEC", confidence="HIGH",
                gates_passed="G1:Data,G2:Safety,G3:Risk,G4:PreFilter,G5:Scoring,G6:Exec",
            )
        await self.executor.execute_trade(
            "KPI0", "kpi_addr0", score=0, decision="SELL", price=0.0015,
            funnel_stage="TP1", rejection_reason="TP1",
            gates_passed="G1:Data,G2:Safety,G3:Risk,G4:PreFilter,G5:Scoring,G6:Exec,POSITION",
        )

        # This is the exact KPI query from positions.py render()
        kpi = _query(self.db_path, """
            SELECT
                COUNT(DISTINCT CASE WHEN decision LIKE '%BUY%' THEN token_address END) as tokens_bought,
                COUNT(CASE WHEN decision LIKE '%SELL_FAILED%' THEN 1 END) as sell_failed
            FROM trades
        """)[0]

        assert kpi["tokens_bought"] == 3
        assert kpi["sell_failed"] == 0

        # Cross-check: distinct bought tokens should match
        unique_buys = _query(self.db_path,
            "SELECT DISTINCT token_address FROM trades WHERE decision LIKE '%BUY%'")
        assert len(unique_buys) == kpi["tokens_bought"]
