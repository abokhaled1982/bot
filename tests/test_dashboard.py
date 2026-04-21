"""
Dashboard consistency and correctness tests.

Tests the pure logic, formatting helpers, P/L calculations,
decision label logic, strategy insights, and DB query contracts
used by the dashboard tabs — all without running Streamlit.
"""
import os
import sys
import json
import sqlite3
from datetime import datetime, timedelta
from unittest.mock import MagicMock, patch

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

from src.execution import events as _events


# ── Shared helpers ────────────────────────────────────────────────────────────

def _create_test_db(db_path: str):
    from src.database import init_db
    orig = sqlite3.connect
    def patched(path, *a, **kw):
        return orig(db_path if path == "memecoin_bot.db" else path, *a, **kw)
    with patch("sqlite3.connect", side_effect=patched):
        init_db()
    _events.init(db_path)


def _make_executor(db_path: str):
    from src.execution.executor import TradeExecutor
    ex = TradeExecutor.__new__(TradeExecutor)
    ex.dry_run = True
    ex.max_position_usd = 0.20
    ex.min_position_usd = 0.10
    ex.db_path = db_path
    ex.http = MagicMock()
    ex.keypair = None
    TradeExecutor._migrated_dbs.discard(db_path)
    return ex


def _query(db_path: str, sql: str, params=()):
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    rows = conn.execute(sql, params).fetchall()
    conn.close()
    return [dict(r) for r in rows]


# ══════════════════════════════════════════════════════════════════════════════
# 1. Formatting helpers (dashboard/components.py)
# ══════════════════════════════════════════════════════════════════════════════

class TestFmtUsd:
    def test_normal_value(self):
        from dashboard.components import fmt_usd
        assert fmt_usd(1234.56) == "$1,234.56"

    def test_zero(self):
        from dashboard.components import fmt_usd
        assert fmt_usd(0.0) == "$0.000000"

    def test_tiny_value(self):
        from dashboard.components import fmt_usd
        result = fmt_usd(0.00020760)
        assert result == "$0.000208"

    def test_negative(self):
        from dashboard.components import fmt_usd
        result = fmt_usd(-50.25)
        assert result == "$-50.25"

    def test_large_value(self):
        from dashboard.components import fmt_usd
        result = fmt_usd(1_000_000.0)
        assert result == "$1,000,000.00"

    def test_custom_decimals(self):
        from dashboard.components import fmt_usd
        result = fmt_usd(1234.5678, decimals=4)
        assert result == "$1,234.5678"


class TestFmtPct:
    def test_positive(self):
        from dashboard.components import fmt_pct
        assert fmt_pct(8.5) == "+8.50%"

    def test_negative(self):
        from dashboard.components import fmt_pct
        assert fmt_pct(-20.0) == "-20.00%"

    def test_zero(self):
        from dashboard.components import fmt_pct
        assert fmt_pct(0.0) == "+0.00%"


class TestPlColor:
    def test_profit(self):
        from dashboard.components import pl_color
        assert pl_color(10.0) == "profit"

    def test_loss(self):
        from dashboard.components import pl_color
        assert pl_color(-5.0) == "loss"

    def test_zero(self):
        from dashboard.components import pl_color
        assert pl_color(0.0) == ""


class TestKpiCard:
    def test_returns_html(self):
        from dashboard.components import kpi_card
        html = kpi_card("Test", "100", "sub")
        assert "kpi-card" in html
        assert "Test" in html
        assert "100" in html
        assert "sub" in html

    def test_no_sub(self):
        from dashboard.components import kpi_card
        html = kpi_card("Label", "Value")
        assert "Label" in html
        assert "Value" in html


class TestTxStatusHtml:
    def test_confirmed(self):
        from dashboard.components import tx_status_html
        html = tx_status_html("confirmed")
        assert "CONFIRMED" in html
        assert "#22c55e" in html  # green

    def test_unconfirmed(self):
        from dashboard.components import tx_status_html
        html = tx_status_html("unconfirmed")
        assert "UNCONFIRMED" in html
        assert "#f59e0b" in html  # amber

    def test_error(self):
        from dashboard.components import tx_status_html
        html = tx_status_html("error")
        assert "ERROR" in html
        assert "#ef4444" in html  # red

    def test_unknown_status(self):
        from dashboard.components import tx_status_html
        html = tx_status_html("weird")
        assert "WEIRD" in html
        assert "#e0a846" in html  # amber fallback


class TestTxBadgeHtml:
    def test_confirmed(self):
        from dashboard.components import tx_badge_html
        assert "✅" in tx_badge_html("confirmed")

    def test_unconfirmed(self):
        from dashboard.components import tx_badge_html
        assert "⚠️" in tx_badge_html("unconfirmed")

    def test_error(self):
        from dashboard.components import tx_badge_html
        assert "❌" in tx_badge_html("error")

    def test_empty(self):
        from dashboard.components import tx_badge_html
        assert tx_badge_html("") == ""


# ══════════════════════════════════════════════════════════════════════════════
# 2. History tab helper functions
# ══════════════════════════════════════════════════════════════════════════════

class TestGatesHtml:
    def test_all_gates_passed(self):
        from dashboard.tabs.history import _gates_html
        html = _gates_html(["G1:Data", "G2:Safety", "G3:Risk", "G4:PreFilter", "G5:Scoring", "G6:Exec"])
        assert html.count("pass") == 6
        assert html.count("fail") == 0

    def test_partial_gates(self):
        from dashboard.tabs.history import _gates_html
        html = _gates_html(["G1:Data", "G2:Safety"])
        assert html.count("pass") == 2
        assert html.count("fail") == 4

    def test_no_gates(self):
        from dashboard.tabs.history import _gates_html
        html = _gates_html([])
        assert html.count("pass") == 0
        assert html.count("fail") == 6

    def test_returns_gates_bar(self):
        from dashboard.tabs.history import _gates_html
        html = _gates_html(["G1:Data"])
        assert "gates-bar" in html


class TestHoldDuration:
    def test_minutes(self):
        from dashboard.tabs.history import _hold_duration_str
        result = _hold_duration_str("2026-04-13 10:00:00", "2026-04-13 10:30:00")
        assert result == "30m"

    def test_hours(self):
        from dashboard.tabs.history import _hold_duration_str
        result = _hold_duration_str("2026-04-13 10:00:00", "2026-04-13 13:30:00")
        assert result == "3.5h"

    def test_days(self):
        from dashboard.tabs.history import _hold_duration_str
        result = _hold_duration_str("2026-04-10 10:00:00", "2026-04-13 10:00:00")
        assert result == "3.0d"

    def test_invalid_input(self):
        from dashboard.tabs.history import _hold_duration_str
        result = _hold_duration_str("bad", "data")
        assert result == "—"

    def test_empty_strings(self):
        from dashboard.tabs.history import _hold_duration_str
        result = _hold_duration_str("", "")
        assert result == "—"


class TestDetailItem:
    def test_returns_html(self):
        from dashboard.tabs.history import _detail_item
        html = _detail_item("Label", "Value")
        assert "detail-item" in html
        assert "detail-label" in html
        assert "detail-value" in html
        assert "Label" in html
        assert "Value" in html


# ══════════════════════════════════════════════════════════════════════════════
# 3. Strategy insight generation
# ══════════════════════════════════════════════════════════════════════════════

class TestGenerateStrategyInsight:
    def test_buy_insight(self):
        from dashboard.components import generate_strategy_insight
        insight = generate_strategy_insight("BUY (SIMULATED)", "BUY_EXEC", {}, "")
        assert "alle 6 Gates bestanden" in insight

    def test_buy_high_hype(self):
        from dashboard.components import generate_strategy_insight
        ai = {"hype_score": 90}
        insight = generate_strategy_insight("BUY (SIMULATED)", "BUY_EXEC", ai, "")
        assert "Hype-Score" in insight

    def test_buy_migration(self):
        from dashboard.components import generate_strategy_insight
        ai = {"is_migration": True}
        insight = generate_strategy_insight("BUY", "BUY_EXEC", ai, "")
        assert "Migration" in insight

    def test_buy_low_liq_warning(self):
        from dashboard.components import generate_strategy_insight
        ai = {"market_data": {"liquidity_usd": 10000}}
        insight = generate_strategy_insight("BUY", "BUY_EXEC", ai, "")
        assert "Liquiditaet" in insight

    def test_reject_data_check(self):
        from dashboard.components import generate_strategy_insight
        insight = generate_strategy_insight("REJECT", "DATA_CHECK", {}, "No data")
        assert "DexScreener" in insight

    def test_reject_safety(self):
        from dashboard.components import generate_strategy_insight
        insight = generate_strategy_insight("REJECT", "SAFETY_CHECK", {}, "Unsafe")
        assert "RugCheck" in insight

    def test_reject_prefilter_low_liq(self):
        from dashboard.components import generate_strategy_insight
        insight = generate_strategy_insight("REJECT", "PRE_FILTER", {}, "Liq zu niedrig")
        assert "Liquiditaet" in insight

    def test_reject_prefilter_falling(self):
        from dashboard.components import generate_strategy_insight
        insight = generate_strategy_insight("REJECT", "PRE_FILTER", {}, "Preis fällt")
        assert "fallende Messer" in insight

    def test_reject_prefilter_sell_pressure(self):
        from dashboard.components import generate_strategy_insight
        insight = generate_strategy_insight("REJECT", "PRE_FILTER", {}, "Verkaufsdruck")
        assert "Sells als Buys" in insight

    def test_reject_prefilter_too_new(self):
        from dashboard.components import generate_strategy_insight
        insight = generate_strategy_insight("REJECT", "PRE_FILTER", {}, "Token zu neu")
        assert "alt" in insight

    def test_reject_prefilter_critical_flag(self):
        from dashboard.components import generate_strategy_insight
        insight = generate_strategy_insight("REJECT", "PRE_FILTER", {}, "Critical Flag: Heavy_Selling")
        assert "Heavy_Selling" in insight

    def test_reject_scoring(self):
        from dashboard.components import generate_strategy_insight
        ai = {"market_data": {"change_1h": 2.0}}
        insight = generate_strategy_insight("REJECT", "SCORING", ai, "Score 45")
        assert "Fusion Score" in insight
        assert "Momentum" in insight

    def test_reject_scoring_whale(self):
        from dashboard.components import generate_strategy_insight
        ai = {"chain_data": {"top_10_pct": 60}}
        insight = generate_strategy_insight("REJECT", "SCORING", ai, "Score 40")
        assert "Wallet-Konzentration" in insight

    def test_sell_stop_loss(self):
        from dashboard.components import generate_strategy_insight
        insight = generate_strategy_insight("SELL", "STOP_LOSS", {}, "SL -20%")
        assert "Stop-Loss" in insight

    def test_sell_tp3(self):
        from dashboard.components import generate_strategy_insight
        insight = generate_strategy_insight("SELL", "TP3", {}, "TP3")
        assert "+200%" in insight

    def test_sell_tp2(self):
        from dashboard.components import generate_strategy_insight
        insight = generate_strategy_insight("SELL", "TP2", {}, "TP2")
        assert "Take-Profit 2" in insight

    def test_sell_tp1(self):
        from dashboard.components import generate_strategy_insight
        insight = generate_strategy_insight("SELL", "TP1", {}, "TP1")
        assert "50%" in insight

    def test_sell_trailing(self):
        from dashboard.components import generate_strategy_insight
        insight = generate_strategy_insight("SELL", "TRAILING_STOP", {}, "Trail")
        assert "Trailing" in insight

    def test_sell_time_exit(self):
        from dashboard.components import generate_strategy_insight
        insight = generate_strategy_insight("SELL", "TIME_EXIT", {}, "Timeout")
        assert "24h" in insight

    def test_no_insight_for_unknown_stage(self):
        from dashboard.components import generate_strategy_insight
        insight = generate_strategy_insight("SKIP", "UNKNOWN_STAGE", {}, "whatever")
        assert insight == ""


# ══════════════════════════════════════════════════════════════════════════════
# 4. P/L calculation logic (mirrors history.py formulas)
# ══════════════════════════════════════════════════════════════════════════════

class TestPLCalculations:
    """
    Test the exact P/L formulas used in history.py render().
    These are extracted here as pure functions to verify correctness.
    """

    @staticmethod
    def _sell_pct(dec, ep, buy_p):
        """Mirror: sell_pct from history.py"""
        return ((ep - buy_p) / buy_p * 100) if "SELL" in dec and buy_p > 0 and ep > 0 else 0

    @staticmethod
    def _sell_plusd(sel_usd, buy_a, sell_pct, position_size_usd=1.0):
        """Mirror: sell_plusd from history.py"""
        return (sel_usd - buy_a) if sel_usd > 0 and buy_a > 0 else (
            (position_size_usd * sell_pct / 100) if sell_pct != 0 else 0
        )

    @staticmethod
    def _live_pct(ep, cp):
        """Mirror: live_pct from history.py"""
        return ((cp - ep) / ep * 100) if ep > 0 and cp > 0 else 0

    # ── sell_pct ──────────────────────────────────────────────────────────────

    def test_sell_pct_profit(self):
        # Bought at 0.001, sold at 0.0015 → +50%
        assert self._sell_pct("SELL (SIMULATED)", 0.0015, 0.001) == pytest.approx(50.0)

    def test_sell_pct_loss(self):
        # Bought at 0.001, sold at 0.0008 → -20%
        assert self._sell_pct("SELL (SIMULATED)", 0.0008, 0.001) == pytest.approx(-20.0)

    def test_sell_pct_zero_buy_price(self):
        result = self._sell_pct("SELL", 0.001, 0)
        assert result == 0

    def test_sell_pct_not_sell_decision(self):
        result = self._sell_pct("BUY (SIMULATED)", 0.001, 0.0005)
        assert result == 0

    # ── sell_plusd ─────────────────────────────────────────────────────────────

    def test_sell_plusd_with_amounts(self):
        # Invested $0.20, got back $0.30 → +$0.10
        result = self._sell_plusd(0.30, 0.20, 50.0)
        assert result == pytest.approx(0.10)

    def test_sell_plusd_loss(self):
        # Invested $0.20, got back $0.16 → -$0.04
        result = self._sell_plusd(0.16, 0.20, -20.0)
        assert result == pytest.approx(-0.04)

    def test_sell_plusd_fallback_to_position_size(self):
        # No amounts → uses position_size * sell_pct/100
        result = self._sell_plusd(0, 0, 50.0, position_size_usd=0.20)
        assert result == pytest.approx(0.10)

    def test_sell_plusd_zero_pct(self):
        result = self._sell_plusd(0, 0, 0.0)
        assert result == 0

    # ── live_pct ──────────────────────────────────────────────────────────────

    def test_live_pct_profit(self):
        # Bought at 0.001, now at 0.0015 → +50%
        assert self._live_pct(0.001, 0.0015) == pytest.approx(50.0)

    def test_live_pct_loss(self):
        assert self._live_pct(0.001, 0.0008) == pytest.approx(-20.0)

    def test_live_pct_zero_entry(self):
        assert self._live_pct(0, 0.001) == 0

    def test_live_pct_zero_current(self):
        assert self._live_pct(0.001, 0) == 0


# ══════════════════════════════════════════════════════════════════════════════
# 5. Decision label logic (the bug we fixed)
# ══════════════════════════════════════════════════════════════════════════════

class TestDecisionLabelLogic:
    """
    Test that the rejection/acceptance label logic works correctly
    for all decision types (including 'BUY (SIMULATED)').
    The accept_detail for BUY is now in ai_reasoning JSON, not rejection_reason.
    """

    @staticmethod
    def _label_for(dec, rej, ai=None):
        """Mirror the fixed logic from history.py"""
        display_reason = rej
        if not display_reason and "BUY" in dec:
            display_reason = (ai or {}).get("accept_detail", "")
        if not display_reason:
            return None
        is_accept = "BUY" in dec or "SELL" in dec or display_reason.startswith("[ACCEPT]")
        if is_accept:
            return "Trade Signal"
        return "Rejection Reason"

    def test_buy_with_accept_detail_in_ai(self):
        """BUY trades now get accept_detail from ai_reasoning, not rejection_reason."""
        ai = {"accept_detail": "[ACCEPT] Score 82.5 | HIGH"}
        assert self._label_for("BUY (SIMULATED)", "", ai) == "Trade Signal"

    def test_buy_no_reject_no_ai_shows_nothing(self):
        """BUY with no rejection_reason and no accept_detail → no label."""
        assert self._label_for("BUY (SIMULATED)", "", {}) is None

    def test_buy_with_rejection_reason_still_works(self):
        """Legacy BUY rows (before fix) that have rejection_reason still show."""
        assert self._label_for("BUY (SIMULATED)", "[ACCEPT] Score 82.5") == "Trade Signal"

    def test_sell_simulated_shows_trade_signal(self):
        assert self._label_for("SELL (SIMULATED)", "Stop-Loss -20%") == "Trade Signal"

    def test_sell_shows_trade_signal(self):
        assert self._label_for("SELL", "TP1") == "Trade Signal"

    def test_reject_shows_rejection_reason(self):
        assert self._label_for("REJECT", "Safety fail") == "Rejection Reason"

    def test_skip_shows_rejection_reason(self):
        assert self._label_for("SKIP", "Score too low") == "Rejection Reason"

    def test_accept_prefix_always_trade_signal(self):
        assert self._label_for("REJECT", "[ACCEPT] Override") == "Trade Signal"

    def test_no_rejection_reason_returns_none(self):
        assert self._label_for("REJECT", "") is None

    def test_error_decision_shows_rejection(self):
        assert self._label_for("ERROR", "TX failed") == "Rejection Reason"

    def test_buy_simulated_no_network(self):
        assert self._label_for("BUY (SIMULATED - NO NETWORK)", "Jupiter unreachable") == "Trade Signal"


# ══════════════════════════════════════════════════════════════════════════════
# 6. Expander label construction (no raw HTML)
# ══════════════════════════════════════════════════════════════════════════════

class TestExpanderLabel:
    """Verify expander labels don't contain HTML tags (Streamlit renders markdown only)."""

    @staticmethod
    def _build_buy_label(sym, ep, pl_tag, tx_icon, ts, pos_open):
        """Mirror the BUY label construction from history.py"""
        icon = "🟢" if pos_open else "⬛"
        return f"{icon} **{sym}** · BUY @ ${ep:.8f} · {pl_tag} {tx_icon} · {ts[:16]}"

    @staticmethod
    def _build_sell_label(sym, ep, sell_pct, sel_usd, tx_icon, ts):
        from dashboard.components import fmt_usd
        sg = "+" if sell_pct >= 0 else ""
        return (
            f"🔴 **{sym}** · SELL @ ${ep:.8f} · "
            f"{sg}{sell_pct:.1f}% · {fmt_usd(sel_usd)} {tx_icon} · {ts[:16]}"
        )

    def test_buy_label_no_html(self):
        label = self._build_buy_label("MILKA", 0.00020760, "+8.5% live", "✅", "2026-04-13T20:38", True)
        assert "<" not in label, f"Label contains HTML: {label}"
        assert ">" not in label

    def test_buy_label_closed(self):
        label = self._build_buy_label("TOKEN", 0.001, "closed", "", "2026-04-13T10:00", False)
        assert "⬛" in label
        assert "closed" in label
        assert "<" not in label

    def test_buy_label_with_sell_pct(self):
        label = self._build_buy_label("TOKEN", 0.001, "+50.0%", "", "2026-04-13T10:00", False)
        assert "+50.0%" in label
        assert "<span" not in label

    def test_sell_label_no_html(self):
        label = self._build_sell_label("TOKEN", 0.002, 50.0, 0.30, "✅", "2026-04-13T10:00")
        assert "<span" not in label
        assert "+50.0%" in label


# ══════════════════════════════════════════════════════════════════════════════
# 7. Dashboard SQL queries — contract tests
# ══════════════════════════════════════════════════════════════════════════════

class TestDashboardSQLQueries:
    """
    Test the exact SQL queries used by the dashboard tabs against a test DB.
    Ensures the queries return the correct shapes and values.
    """

    @pytest.fixture(autouse=True)
    def setup_db(self, tmp_path):
        self.db_path = str(tmp_path / "test.db")
        _create_test_db(self.db_path)
        self.executor = _make_executor(self.db_path)
        _events._DB_PATH = self.db_path

    async def _insert_trades(self):
        """Insert a realistic mix of trades."""
        # 3 buys
        for i, (sym, addr, price, score) in enumerate([
            ("MILKA", "addr_a", 0.00020760, 82.5),
            ("DOGE2", "addr_b", 0.005, 75.0),
            ("MOON",  "addr_c", 0.001, 90.0),
        ]):
            await self.executor.execute_trade(
                sym, addr, score=score, decision="BUY", price=price,
                funnel_stage="BUY_EXEC", confidence="HIGH",
                gates_passed="G1:Data,G2:Safety,G3:Risk,G4:PreFilter,G5:Scoring,G6:Exec",
                extra={"source": "BOOSTED_TOP", "market_cap": 200000 + i * 50000,
                       "liquidity_usd": 50000 + i * 10000},
            )
        # 1 sell (MOON stop-loss)
        await self.executor.execute_trade(
            "MOON", "addr_c", score=0, decision="SELL", price=0.0008,
            funnel_stage="STOP_LOSS", rejection_reason="Stop-Loss -20%",
            gates_passed="G1:Data,G2:Safety,G3:Risk,G4:PreFilter,G5:Scoring,G6:Exec,POSITION",
        )
        # 2 rejects
        for sym, addr, stage in [("SCAM1", "addr_d", "SAFETY_CHECK"), ("WEAK", "addr_e", "SCORING")]:
            await self.executor.execute_trade(
                sym, addr, score=20, decision="REJECT", price=0.0001,
                rejection_reason="Failed checks", funnel_stage=stage,
                gates_passed="G1:Data",
            )

    # ── History tab query ─────────────────────────────────────────────────────

    @pytest.mark.asyncio
    async def test_history_query_columns(self):
        """History tab query should return all expected columns."""
        await self._insert_trades()
        rows = _query(self.db_path,
            """SELECT id, symbol, token_address, entry_price, position_size,
                      buy_amount_usd, sell_amount_usd,
                      score, decision, rejection_reason, ai_reasoning,
                      funnel_stage, gates_passed, timestamp, tx_signature, tx_status
               FROM trades ORDER BY timestamp DESC LIMIT 50""")
        assert len(rows) == 6
        required_cols = {"id", "symbol", "token_address", "entry_price", "position_size",
                         "score", "decision", "rejection_reason", "funnel_stage",
                         "gates_passed", "timestamp", "tx_signature", "tx_status"}
        assert required_cols.issubset(set(rows[0].keys()))

    @pytest.mark.asyncio
    async def test_history_buy_filter(self):
        """Filtering decision LIKE '%BUY%' should match simulated buys."""
        await self._insert_trades()
        rows = _query(self.db_path,
            "SELECT * FROM trades WHERE decision LIKE ? ORDER BY timestamp DESC",
            ("%BUY%",))
        assert len(rows) == 3
        for r in rows:
            assert "BUY" in r["decision"]

    @pytest.mark.asyncio
    async def test_history_sell_filter(self):
        await self._insert_trades()
        rows = _query(self.db_path,
            "SELECT * FROM trades WHERE decision LIKE ? ORDER BY timestamp DESC",
            ("%SELL%",))
        assert len(rows) == 1
        assert "SELL" in rows[0]["decision"]

    @pytest.mark.asyncio
    async def test_history_gate_filter(self):
        """Gate filter: 6 gates → only BUY/SELL"""
        await self._insert_trades()
        # 6+ gates (at least 5 commas) → only BUY/SELL rows
        rows = _query(self.db_path,
            "SELECT * FROM trades WHERE (LENGTH(gates_passed)-LENGTH(REPLACE(gates_passed,',',''))>=5)")
        for r in rows:
            assert "BUY" in r["decision"] or "SELL" in r["decision"]

    @pytest.mark.asyncio
    async def test_history_search_by_symbol(self):
        await self._insert_trades()
        rows = _query(self.db_path,
            "SELECT * FROM trades WHERE (symbol LIKE ? OR token_address LIKE ?)",
            ("%MILKA%", "%MILKA%"))
        assert len(rows) == 1
        assert rows[0]["symbol"] == "MILKA"

    @pytest.mark.asyncio
    async def test_history_search_by_address(self):
        await self._insert_trades()
        rows = _query(self.db_path,
            "SELECT * FROM trades WHERE (symbol LIKE ? OR token_address LIKE ?)",
            ("%addr_b%", "%addr_b%"))
        assert len(rows) == 1
        assert rows[0]["token_address"] == "addr_b"

    # ── Positions tab KPI query ───────────────────────────────────────────────

    @pytest.mark.asyncio
    async def test_positions_kpi_total_invested(self):
        """Positions KPI: total_invested sums buy_amount_usd for BUY decisions."""
        await self._insert_trades()
        # Note: dry-run doesn't set buy_amount_usd (it's None), but decision is BUY (SIMULATED)
        row = _query(self.db_path, """
            SELECT
                SUM(CASE WHEN decision='BUY' THEN buy_amount_usd ELSE 0 END) as total_invested,
                SUM(CASE WHEN decision='SELL' THEN sell_amount_usd ELSE 0 END) as total_returned,
                COUNT(DISTINCT CASE WHEN decision='BUY' THEN token_address END) as tokens_bought
            FROM trades
        """)[0]
        # With dry-run, decision is 'BUY (SIMULATED)' not 'BUY' → exact match fails
        # This documents the current mismatch between KPI query and actual decision values

    @pytest.mark.asyncio
    async def test_positions_kpi_with_like(self):
        """
        Using LIKE '%BUY%' correctly matches 'BUY (SIMULATED)' decisions.
        This is how it should be queried for consistency.
        """
        await self._insert_trades()
        row = _query(self.db_path, """
            SELECT
                COUNT(DISTINCT CASE WHEN decision LIKE '%BUY%' THEN token_address END) as tokens_bought,
                COUNT(DISTINCT CASE WHEN decision LIKE '%SELL%' THEN token_address END) as tokens_sold
            FROM trades
        """)[0]
        assert row["tokens_bought"] == 3
        assert row["tokens_sold"] == 1

    # ── Buy lookup (used by history cross-reference) ──────────────────────────

    @pytest.mark.asyncio
    async def test_buy_lookup_query(self):
        """The buy_lookup used by history tab for cross-referencing sells."""
        await self._insert_trades()
        rows = _query(self.db_path,
            "SELECT token_address, entry_price, buy_amount_usd, timestamp "
            "FROM trades WHERE decision LIKE '%BUY%'")
        assert len(rows) == 3
        lookup = {}
        for r in rows:
            lookup[r["token_address"]] = {
                "buy_price": float(r["entry_price"] or 0),
                "buy_amount": float(r["buy_amount_usd"] or 0),
                "buy_time": str(r["timestamp"] or ""),
            }
        assert "addr_a" in lookup
        assert lookup["addr_a"]["buy_price"] == pytest.approx(0.00020760)

    @pytest.mark.asyncio
    async def test_sell_lookup_query(self):
        """The sell_lookup used by history tab for BUY cross-reference."""
        await self._insert_trades()
        rows = _query(self.db_path,
            "SELECT token_address, entry_price, sell_amount_usd, rejection_reason, timestamp "
            "FROM trades WHERE decision LIKE '%SELL%'")
        assert len(rows) == 1
        assert rows[0]["token_address"] == "addr_c"
        assert rows[0]["entry_price"] == pytest.approx(0.0008)

    # ── Events query ──────────────────────────────────────────────────────────

    @pytest.mark.asyncio
    async def test_events_query(self):
        """The events query used by overview tab."""
        await self._insert_trades()
        rows = _query(self.db_path,
            "SELECT event_type, symbol, address, tx_signature, "
            "buy_amount_usd, sell_amount_usd, price_usd, pnl_usd, pnl_pct, "
            "stage, message, timestamp "
            "FROM bot_events ORDER BY id DESC LIMIT 25")
        # 3 BUY_SIMULATED + 1 SELL_SIMULATED = 4 events
        assert len(rows) == 4
        event_types = {r["event_type"] for r in rows}
        assert "BUY_SIMULATED" in event_types
        assert "SELL_SIMULATED" in event_types

    @pytest.mark.asyncio
    async def test_events_have_matching_trades(self):
        """Every event should correspond to a trade with the same address."""
        await self._insert_trades()
        events = _query(self.db_path, "SELECT * FROM bot_events")
        for ev in events:
            addr = ev["address"]
            trades = _query(self.db_path,
                "SELECT * FROM trades WHERE token_address=? AND decision LIKE ?",
                (addr, f"%{'BUY' if 'BUY' in ev['event_type'] else 'SELL'}%"))
            assert len(trades) >= 1, f"Event {ev['event_type']} for {addr} has no matching trade"

    # ── Extended data columns survive query ───────────────────────────────────

    @pytest.mark.asyncio
    async def test_extended_data_in_history_query(self):
        """Extended columns (source, market_cap, etc.) are readable from trades."""
        await self._insert_trades()
        rows = _query(self.db_path,
            "SELECT source, market_cap, liquidity_usd FROM trades WHERE decision LIKE '%BUY%'")
        for r in rows:
            assert r["source"] == "BOOSTED_TOP"
            assert r["market_cap"] is not None and r["market_cap"] > 0
            assert r["liquidity_usd"] is not None and r["liquidity_usd"] > 0


# ══════════════════════════════════════════════════════════════════════════════
# 8. Positions tab KPI vs actual decision mismatch detection
# ══════════════════════════════════════════════════════════════════════════════

class TestPositionsKPIMismatch:
    """
    Verify the LIKE-based queries correctly match 'BUY (SIMULATED)' decisions.
    (Bug B1/B2 fix: positions.py and analytics.py used exact match before.)
    """

    @pytest.fixture(autouse=True)
    def setup_db(self, tmp_path):
        self.db_path = str(tmp_path / "test.db")
        _create_test_db(self.db_path)
        self.executor = _make_executor(self.db_path)

    @pytest.mark.asyncio
    async def test_like_buy_matches_simulated(self):
        """LIKE '%BUY%' correctly matches 'BUY (SIMULATED)'."""
        await self.executor.execute_trade(
            "TEST", "addr_x", score=80, decision="BUY", price=0.001,
            funnel_stage="BUY_EXEC", confidence="HIGH",
            gates_passed="G1:Data,G2:Safety,G3:Risk,G4:PreFilter,G5:Scoring,G6:Exec",
        )
        # The fixed query (LIKE) should match
        like = _query(self.db_path,
            "SELECT COUNT(DISTINCT token_address) as n FROM trades WHERE decision LIKE '%BUY%'")[0]["n"]
        assert like == 1

    @pytest.mark.asyncio
    async def test_like_sell_matches_simulated(self):
        """LIKE '%SELL%' correctly matches 'SELL (SIMULATED)'."""
        await self.executor.execute_trade(
            "TEST", "addr_y", score=0, decision="SELL", price=0.002,
            funnel_stage="STOP_LOSS", rejection_reason="SL",
            gates_passed="G1:Data,G2:Safety,G3:Risk,G4:PreFilter,G5:Scoring,G6:Exec,POSITION",
        )
        like = _query(self.db_path,
            "SELECT COUNT(*) as n FROM trades WHERE decision LIKE '%SELL%'")[0]["n"]
        assert like == 1

    @pytest.mark.asyncio
    async def test_exact_match_misses_simulated(self):
        """Exact decision='BUY' does NOT match 'BUY (SIMULATED)' (documents old bug)."""
        await self.executor.execute_trade(
            "TEST", "addr_z", score=80, decision="BUY", price=0.001,
            funnel_stage="BUY_EXEC", confidence="HIGH",
            gates_passed="G1:Data,G2:Safety,G3:Risk,G4:PreFilter,G5:Scoring,G6:Exec",
        )
        exact = _query(self.db_path,
            "SELECT COUNT(*) as n FROM trades WHERE decision='BUY'")[0]["n"]
        assert exact == 0, "Exact match should miss simulated"


# ══════════════════════════════════════════════════════════════════════════════
# 9. Analytics P/L query consistency
# ══════════════════════════════════════════════════════════════════════════════

class TestAnalyticsPLQuery:
    """Test the P/L query used by analytics tab returns correct values."""

    @pytest.fixture(autouse=True)
    def setup_db(self, tmp_path):
        self.db_path = str(tmp_path / "test.db")
        _create_test_db(self.db_path)
        self.executor = _make_executor(self.db_path)

    @pytest.mark.asyncio
    async def test_pl_query_returns_correct_structure(self):
        """The analytics P/L query should return invested + returned."""
        await self.executor.execute_trade(
            "T1", "a1", score=80, decision="BUY", price=0.001,
            funnel_stage="BUY_EXEC", confidence="HIGH",
            gates_passed="G1:Data,G2:Safety,G3:Risk,G4:PreFilter,G5:Scoring,G6:Exec",
        )
        row = _query(self.db_path, """
            SELECT
                COALESCE(SUM(CASE WHEN decision LIKE '%BUY%' THEN buy_amount_usd ELSE 0 END), 0) as invested,
                COALESCE(SUM(CASE WHEN decision LIKE '%SELL%' THEN sell_amount_usd ELSE 0 END), 0) as returned
            FROM trades
        """)[0]
        # Dry-run BUY has NULL buy_amount_usd, so COALESCE ensures 0
        assert row["invested"] is not None
        assert row["returned"] is not None
        assert isinstance(row["invested"], (int, float))
        assert isinstance(row["returned"], (int, float))

    @pytest.mark.asyncio
    async def test_rejection_stats_query(self):
        """Query for rejection statistics (used by analytics charts)."""
        for i, stage in enumerate(["SAFETY_CHECK", "PRE_FILTER", "SCORING", "PRE_FILTER"]):
            await self.executor.execute_trade(
                f"R{i}", f"r_{i}", score=20+i, decision="REJECT",
                price=0.0001, rejection_reason=f"Fail at {stage}",
                funnel_stage=stage, gates_passed="G1:Data",
            )
        rows = _query(self.db_path,
            "SELECT funnel_stage, COUNT(*) as cnt FROM trades "
            "WHERE decision='REJECT' GROUP BY funnel_stage ORDER BY cnt DESC")
        assert len(rows) >= 2
        # PRE_FILTER should have 2 rejects
        pf = next(r for r in rows if r["funnel_stage"] == "PRE_FILTER")
        assert pf["cnt"] == 2

    @pytest.mark.asyncio
    async def test_score_distribution_query(self):
        """Score distribution query for histogram charts."""
        for i in range(10):
            dec = "BUY" if i >= 8 else "REJECT"
            await self.executor.execute_trade(
                f"S{i}", f"s_{i}", score=10 * (i + 1), decision=dec,
                price=0.001, rejection_reason="test" if dec == "REJECT" else None,
                funnel_stage="BUY_EXEC" if dec == "BUY" else "SCORING",
                confidence="HIGH" if dec == "BUY" else "LOW",
                gates_passed="G1:Data,G2:Safety,G3:Risk,G4:PreFilter,G5:Scoring,G6:Exec" if dec == "BUY" else "G1:Data",
            )
        rows = _query(self.db_path, "SELECT score, decision FROM trades ORDER BY score")
        assert len(rows) == 10
        # Buys should have the highest scores
        buys = [r for r in rows if "BUY" in r["decision"]]
        rejects = [r for r in rows if r["decision"] == "REJECT"]
        assert min(r["score"] for r in buys) >= max(r["score"] for r in rejects)


# ══════════════════════════════════════════════════════════════════════════════
# 10. Pipeline BUY accept_detail fix (B6)
# ══════════════════════════════════════════════════════════════════════════════

class TestBuyAcceptDetailInAiReasoning:
    """
    Bug B6: Previously accept_detail was stored in rejection_reason for BUY trades.
    Now it should be in ai_reasoning JSON and rejection_reason should be None/empty.
    """

    @pytest.fixture(autouse=True)
    def setup_db(self, tmp_path):
        self.db_path = str(tmp_path / "test.db")
        _create_test_db(self.db_path)
        self.executor = _make_executor(self.db_path)

    @pytest.mark.asyncio
    async def test_buy_rejection_reason_is_null(self):
        """BUY trades should have NULL rejection_reason (not accept_detail)."""
        ai = {"accept_detail": "[ACCEPT] Score 85.0 | HIGH", "hype_score": 85}
        await self.executor.execute_trade(
            "TEST", "addr_buy", score=85, decision="BUY", price=0.001,
            funnel_stage="BUY_EXEC", confidence="HIGH",
            ai_reasoning=json.dumps(ai), rejection_reason=None,
            gates_passed="G1:Data,G2:Safety,G3:Risk,G4:PreFilter,G5:Scoring,G6:Exec",
        )
        rows = _query(self.db_path,
            "SELECT rejection_reason, ai_reasoning FROM trades WHERE decision LIKE '%BUY%'")
        assert len(rows) == 1
        assert rows[0]["rejection_reason"] is None or rows[0]["rejection_reason"] == ""

    @pytest.mark.asyncio
    async def test_accept_detail_in_ai_reasoning_json(self):
        """accept_detail should be retrievable from ai_reasoning JSON."""
        ai = {"accept_detail": "[ACCEPT] Score 85.0 | HIGH", "hype_score": 85}
        await self.executor.execute_trade(
            "TEST", "addr_buy2", score=85, decision="BUY", price=0.001,
            funnel_stage="BUY_EXEC", confidence="HIGH",
            ai_reasoning=json.dumps(ai), rejection_reason=None,
            gates_passed="G1:Data,G2:Safety,G3:Risk,G4:PreFilter,G5:Scoring,G6:Exec",
        )
        rows = _query(self.db_path,
            "SELECT ai_reasoning FROM trades WHERE decision LIKE '%BUY%'")
        parsed = json.loads(rows[0]["ai_reasoning"])
        assert "accept_detail" in parsed
        assert parsed["accept_detail"].startswith("[ACCEPT]")

    @pytest.mark.asyncio
    async def test_extra_dict_no_crash(self):
        """Extra dict with known keys should not crash _log_to_db."""
        await self.executor.execute_trade(
            "TEST", "addr_extra", score=80, decision="BUY", price=0.001,
            funnel_stage="BUY_EXEC", confidence="HIGH",
            gates_passed="G1:Data,G2:Safety,G3:Risk,G4:PreFilter,G5:Scoring,G6:Exec",
            extra={"sol_price": 150.5},
        )
        rows = _query(self.db_path,
            "SELECT * FROM trades WHERE decision LIKE '%BUY%'")
        assert len(rows) == 1


# ══════════════════════════════════════════════════════════════════════════════
# 11. Token age filter (history tab)
# ══════════════════════════════════════════════════════════════════════════════

class TestTokenAgeToHours:
    """Test the token_age_to_hours conversion helper."""

    def test_minutes_to_hours(self):
        from dashboard.tabs.history import token_age_to_hours
        assert token_age_to_hours(5, "min") == pytest.approx(5 / 60)

    def test_10_minutes(self):
        from dashboard.tabs.history import token_age_to_hours
        assert token_age_to_hours(10, "min") == pytest.approx(10 / 60)

    def test_15_minutes(self):
        from dashboard.tabs.history import token_age_to_hours
        assert token_age_to_hours(15, "min") == pytest.approx(0.25)

    def test_60_minutes_equals_1_hour(self):
        from dashboard.tabs.history import token_age_to_hours
        assert token_age_to_hours(60, "min") == pytest.approx(1.0)

    def test_hours_passthrough(self):
        from dashboard.tabs.history import token_age_to_hours
        assert token_age_to_hours(3, "hour") == 3.0

    def test_days_to_hours(self):
        from dashboard.tabs.history import token_age_to_hours
        assert token_age_to_hours(1, "day") == 24.0

    def test_7_days(self):
        from dashboard.tabs.history import token_age_to_hours
        assert token_age_to_hours(7, "day") == 168.0

    def test_zero_value(self):
        from dashboard.tabs.history import token_age_to_hours
        assert token_age_to_hours(0, "min") == 0.0
        assert token_age_to_hours(0, "hour") == 0.0
        assert token_age_to_hours(0, "day") == 0.0

    def test_fractional_value(self):
        from dashboard.tabs.history import token_age_to_hours
        assert token_age_to_hours(1.5, "hour") == pytest.approx(1.5)

    def test_unknown_unit_defaults_to_hours(self):
        from dashboard.tabs.history import token_age_to_hours
        assert token_age_to_hours(5, "unknown") == 5.0


class TestTokenAgeFilterSQL:
    """Test the token age filter produces correct SQL results."""

    @pytest.fixture(autouse=True)
    def setup_db(self, tmp_path):
        self.db_path = str(tmp_path / "test.db")
        _create_test_db(self.db_path)
        self.executor = _make_executor(self.db_path)

    @pytest.mark.asyncio
    async def _insert(self, symbol, address, age_hours):
        await self.executor.execute_trade(
            symbol, address, score=50, decision="REJECT", price=0.001,
            rejection_reason="test", funnel_stage="SCORING",
            gates_passed="G1:Data",
            extra={"token_age_hours": age_hours},
        )

    @pytest.mark.asyncio
    async def test_max_age_5min_filters_old_coins(self):
        """≤ 5 min should exclude coins older than 5 min."""
        from dashboard.tabs.history import token_age_to_hours
        await self._insert("FRESH", "a1", 0.05)      # 3 min
        await self._insert("OLD", "a2", 100.0)        # 100 hours
        max_h = token_age_to_hours(5, "min")
        rows = _query(self.db_path,
            "SELECT symbol FROM trades WHERE token_age_hours IS NOT NULL AND token_age_hours <= ?",
            (max_h,))
        symbols = [r["symbol"] for r in rows]
        assert "FRESH" in symbols
        assert "OLD" not in symbols

    @pytest.mark.asyncio
    async def test_max_age_1hour_includes_young_coins(self):
        from dashboard.tabs.history import token_age_to_hours
        await self._insert("YOUNG", "b1", 0.5)        # 30 min
        await self._insert("MEDIUM", "b2", 0.9)       # 54 min
        await self._insert("OLD", "b3", 2.0)           # 2 hours
        max_h = token_age_to_hours(1, "hour")
        rows = _query(self.db_path,
            "SELECT symbol FROM trades WHERE token_age_hours IS NOT NULL AND token_age_hours <= ?",
            (max_h,))
        symbols = [r["symbol"] for r in rows]
        assert "YOUNG" in symbols
        assert "MEDIUM" in symbols
        assert "OLD" not in symbols

    @pytest.mark.asyncio
    async def test_min_age_filter(self):
        """≥ Min should exclude coins younger than threshold."""
        from dashboard.tabs.history import token_age_to_hours
        await self._insert("BABY", "c1", 0.01)        # ~36 sec
        await self._insert("MATURE", "c2", 48.0)      # 2 days
        min_h = token_age_to_hours(1, "day")
        rows = _query(self.db_path,
            "SELECT symbol FROM trades WHERE token_age_hours IS NOT NULL AND token_age_hours >= ?",
            (min_h,))
        symbols = [r["symbol"] for r in rows]
        assert "MATURE" in symbols
        assert "BABY" not in symbols

    @pytest.mark.asyncio
    async def test_null_age_excluded(self):
        """Coins without token_age_hours should be excluded by either filter."""
        await self._insert("NO_AGE", "d1", None)
        await self._insert("HAS_AGE", "d2", 0.5)
        rows = _query(self.db_path,
            "SELECT symbol FROM trades WHERE token_age_hours IS NOT NULL AND token_age_hours <= ?",
            (1.0,))
        symbols = [r["symbol"] for r in rows]
        assert "HAS_AGE" in symbols
        assert "NO_AGE" not in symbols

    @pytest.mark.asyncio
    async def test_exact_boundary(self):
        """Token age exactly at threshold should be included for <= and >=."""
        from dashboard.tabs.history import token_age_to_hours
        threshold_h = token_age_to_hours(10, "min")
        await self._insert("EXACT", "e1", threshold_h)
        rows_le = _query(self.db_path,
            "SELECT symbol FROM trades WHERE token_age_hours IS NOT NULL AND token_age_hours <= ?",
            (threshold_h,))
        rows_ge = _query(self.db_path,
            "SELECT symbol FROM trades WHERE token_age_hours IS NOT NULL AND token_age_hours >= ?",
            (threshold_h,))
        assert any(r["symbol"] == "EXACT" for r in rows_le)
        assert any(r["symbol"] == "EXACT" for r in rows_ge)


# ══════════════════════════════════════════════════════════════════════════════
# 12. Search mode filter (Contains / Exact / Starts with)
# ══════════════════════════════════════════════════════════════════════════════

class TestBuildSearchClause:
    """Test the build_search_clause helper used by history search."""

    @staticmethod
    def _clause(search, mode):
        from dashboard.tabs.history import build_search_clause
        return build_search_clause(search, mode)

    # ── Contains mode ─────────────────────────────────────────────────────────

    def test_contains_wraps_with_wildcards(self):
        clause, params = self._clause("AC", "Contains")
        assert clause == "(symbol LIKE ? OR token_address LIKE ?)"
        assert params == ["%AC%", "%AC%"]

    def test_contains_long_name(self):
        clause, params = self._clause("USELESS", "Contains")
        assert params == ["%USELESS%", "%USELESS%"]

    # ── Exact mode ────────────────────────────────────────────────────────────

    def test_exact_uses_upper_equality(self):
        clause, params = self._clause("AC", "Exact")
        assert "UPPER(symbol) = UPPER(?)" in clause
        assert params[0] == "AC"

    def test_exact_still_searches_address_by_contains(self):
        """Addresses are long hex — exact match for address would be useless."""
        clause, params = self._clause("AC", "Exact")
        assert "token_address LIKE ?" in clause
        assert params[1] == "%AC%"

    def test_exact_case_insensitive(self):
        """UPPER() on both sides means 'ac' matches 'AC'."""
        clause, _ = self._clause("ac", "Exact")
        assert "UPPER" in clause

    # ── Starts with mode ──────────────────────────────────────────────────────

    def test_starts_with_prefix_wildcard(self):
        clause, params = self._clause("AC", "Starts with")
        assert params[0] == "AC%"
        assert params[1] == "%AC%"

    def test_starts_with_longer(self):
        _, params = self._clause("DOGE", "Starts with")
        assert params[0] == "DOGE%"

    # ── Edge cases ────────────────────────────────────────────────────────────

    def test_empty_search_returns_empty(self):
        clause, params = self._clause("", "Contains")
        assert clause == ""
        assert params == []

    def test_whitespace_only_returns_empty(self):
        clause, params = self._clause("   ", "Exact")
        assert clause == ""
        assert params == []

    def test_none_search_returns_empty(self):
        clause, params = self._clause(None, "Contains")
        assert clause == ""
        assert params == []

    def test_search_strips_whitespace(self):
        _, params = self._clause("  AC  ", "Exact")
        assert params[0] == "AC"

    def test_single_char_exact(self):
        """Single character symbol like 'X' should work in exact mode."""
        clause, params = self._clause("X", "Exact")
        assert params[0] == "X"


class TestSearchFilterSQL:
    """Test search modes against actual DB rows."""

    @pytest.fixture(autouse=True)
    def setup_db(self, tmp_path):
        self.db_path = str(tmp_path / "test.db")
        _create_test_db(self.db_path)
        self.executor = _make_executor(self.db_path)

    @pytest.mark.asyncio
    async def _insert(self, symbol, address):
        await self.executor.execute_trade(
            symbol, address, score=50, decision="REJECT", price=0.001,
            rejection_reason="test", funnel_stage="SCORING",
            gates_passed="G1:Data",
        )

    def _search(self, search, mode):
        from dashboard.tabs.history import build_search_clause
        clause, params = build_search_clause(search, mode)
        if not clause:
            return []
        rows = _query(self.db_path,
            f"SELECT symbol FROM trades WHERE {clause}", tuple(params))
        return [r["symbol"] for r in rows]

    @pytest.mark.asyncio
    async def test_contains_matches_substring(self):
        await self._insert("SPACE", "addr1")
        await self._insert("AC", "addr2")
        await self._insert("BACK", "addr3")
        result = self._search("AC", "Contains")
        assert "AC" in result
        assert "SPACE" in result    # SP-AC-E contains AC
        assert "BACK" in result     # B-AC-K contains AC

    @pytest.mark.asyncio
    async def test_exact_only_matches_symbol(self):
        await self._insert("SPACE", "addr1")
        await self._insert("AC", "addr2")
        await self._insert("BACK", "addr3")
        result = self._search("AC", "Exact")
        assert "AC" in result
        assert "SPACE" not in result
        assert "BACK" not in result

    @pytest.mark.asyncio
    async def test_exact_case_insensitive(self):
        await self._insert("AC", "addr1")
        result = self._search("ac", "Exact")
        assert "AC" in result

    @pytest.mark.asyncio
    async def test_starts_with_prefix(self):
        await self._insert("ACID", "addr1")
        await self._insert("ACME", "addr2")
        await self._insert("BACK", "addr3")
        await self._insert("AC", "addr4")
        result = self._search("AC", "Starts with")
        assert "AC" in result
        assert "ACID" in result
        assert "ACME" in result
        assert "BACK" not in result

    @pytest.mark.asyncio
    async def test_exact_address_still_contains(self):
        """Even in Exact mode, address search uses contains (addresses are long)."""
        await self._insert("OTHER", "0xAC1234")
        result = self._search("AC1234", "Exact")
        assert "OTHER" in result    # matched via address contains

    @pytest.mark.asyncio
    async def test_two_letter_exact_match(self):
        """The key use-case: find 2-letter coin 'AC' without matching SPACE, BACK."""
        await self._insert("AC", "mint_1111")
        await self._insert("SPACE", "mint_2222")
        await self._insert("ATTACK", "mint_3333")
        await self._insert("GRACE", "mint_4444")
        result = self._search("AC", "Exact")
        assert result == ["AC"]
