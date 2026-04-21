"""
Tests for database schema and buy/sell amount recording.
Verifies the trades table has the expected columns and that
buy_amount_usd / sell_amount_usd are properly stored.

Run:
    cd /home/alghobariw/.openclaw/workspace/memecoin_bot
    source venv/bin/activate
    python -m pytest tests/test_db_schema.py -v
"""
import sys, os, sqlite3, tempfile
sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

from src.execution.executor import TradeExecutor


def _make_tmp_executor(tmp_path: str) -> TradeExecutor:
    """Create an executor that writes to a temporary DB."""
    ex = TradeExecutor.__new__(TradeExecutor)
    ex.db_path = tmp_path
    return ex


def _columns(db_path: str) -> list:
    conn = sqlite3.connect(db_path)
    cols = [r[1] for r in conn.execute("PRAGMA table_info(trades)").fetchall()]
    conn.close()
    return cols


class TestDBSchema:

    def test_live_db_has_buy_sell_columns(self):
        """The production DB must have the new money-tracking columns."""
        cols = _columns("memecoin_bot.db")
        assert "buy_amount_usd" in cols, f"buy_amount_usd missing from trades. cols={cols}"
        assert "sell_amount_usd" in cols, f"sell_amount_usd missing from trades. cols={cols}"

    def test_live_db_has_core_columns(self):
        """Required columns for dashboard queries must exist."""
        cols = _columns("memecoin_bot.db")
        required = [
            "token_address", "symbol", "entry_price", "position_size",
            "score", "decision", "funnel_stage", "timestamp",
            "tx_signature", "tx_status", "gates_passed",
            "buy_amount_usd", "sell_amount_usd",
        ]
        missing = [c for c in required if c not in cols]
        assert not missing, f"Missing columns in trades table: {missing}"


class TestBuyAmountRecording:

    def test_buy_amount_stored_correctly(self):
        """_log_to_db() must persist buy_amount_usd to the trades table."""
        with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as f:
            tmp_path = f.name

        try:
            # Initialise schema via the first log call
            conn = sqlite3.connect(tmp_path)
            conn.execute("""
                CREATE TABLE trades (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    token_address TEXT, symbol TEXT, entry_price REAL,
                    position_size REAL, score REAL, decision TEXT,
                    rejection_reason TEXT, ai_reasoning TEXT,
                    funnel_stage TEXT, timestamp TEXT,
                    gates_passed TEXT, pair_created_at INTEGER,
                    tx_signature TEXT, tx_status TEXT
                )
            """)
            conn.commit()
            conn.close()

            ex = _make_tmp_executor(tmp_path)
            ex._log_to_db(
                symbol="TEST", address="ADDR123",
                price=0.000010, size=0.20,
                score=75, decision="BUY",
                buy_amount_usd=0.20,
                sell_amount_usd=None,
            )

            conn = sqlite3.connect(tmp_path)
            row = conn.execute(
                "SELECT buy_amount_usd, sell_amount_usd FROM trades WHERE symbol='TEST'"
            ).fetchone()
            conn.close()

            assert row is not None, "No row inserted"
            assert abs(row[0] - 0.20) < 1e-9, f"buy_amount_usd wrong: {row[0]}"
            assert row[1] is None, f"sell_amount_usd should be NULL for BUY: {row[1]}"
        finally:
            os.unlink(tmp_path)

    def test_sell_amount_stored_correctly(self):
        """_log_to_db() must persist sell_amount_usd to the trades table."""
        with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as f:
            tmp_path = f.name

        try:
            conn = sqlite3.connect(tmp_path)
            conn.execute("""
                CREATE TABLE trades (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    token_address TEXT, symbol TEXT, entry_price REAL,
                    position_size REAL, score REAL, decision TEXT,
                    rejection_reason TEXT, ai_reasoning TEXT,
                    funnel_stage TEXT, timestamp TEXT,
                    gates_passed TEXT, pair_created_at INTEGER,
                    tx_signature TEXT, tx_status TEXT
                )
            """)
            conn.commit()
            conn.close()

            ex = _make_tmp_executor(tmp_path)
            ex._log_to_db(
                symbol="TEST", address="ADDR123",
                price=0.000050, size=0.0,
                score=75, decision="SELL",
                buy_amount_usd=None,
                sell_amount_usd=0.35,
            )

            conn = sqlite3.connect(tmp_path)
            row = conn.execute(
                "SELECT buy_amount_usd, sell_amount_usd FROM trades WHERE symbol='TEST'"
            ).fetchone()
            conn.close()

            assert row is not None
            assert row[0] is None, f"buy_amount_usd should be NULL for SELL: {row[0]}"
            assert abs(row[1] - 0.35) < 1e-9, f"sell_amount_usd wrong: {row[1]}"
        finally:
            os.unlink(tmp_path)

    def test_profit_loss_calculation(self):
        """P/L = sell_amount_usd - buy_amount_usd (simple sanity)."""
        buy  = 0.20
        sell = 0.35
        pl   = sell - buy
        assert abs(pl - 0.15) < 1e-9

        buy2  = 0.20
        sell2 = 0.12
        pl2   = sell2 - buy2
        assert abs(pl2 - (-0.08)) < 1e-9
