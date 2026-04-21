"""
Unit tests for stop-loss, trailing-stop, and take-profit logic.
Tests the PURE MATH of the decision rules from monitor.py constants.
No class instantiation required — no network, no DB, no wallet.

Run:
    cd /home/alghobariw/.openclaw/workspace/memecoin_bot
    source venv/bin/activate
    python -m pytest tests/test_monitor_logic.py -v
"""
import sys
import os
sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

# Import only the module-level constants — no class instantiation needed
from src.execution.monitor import (
    STOP_LOSS_PCT, TRAILING_STOP_PCT, TRAILING_ACTIVATE,
    TP1_PCT, TP2_PCT, TP3_PCT,
    TP1_SELL_PCT, TP2_SELL_PCT,
)


def _decide(entry, current, highest, trailing_active,
            tp1_hit=False, tp2_hit=False, tp3_hit=False, remaining=1.0):
    """
    Pure Python replica of the monitor's sell-decision block.
    Returns (sell_fraction, stage) or (0.0, None) if no sell.
    """
    change_pct     = (current - entry) / entry
    drop_from_ath  = (current - highest) / highest if highest > 0 else 0

    # Stop-Loss
    if change_pct <= -STOP_LOSS_PCT:
        return 1.0, "STOP_LOSS"

    # Trailing stop
    if change_pct >= TRAILING_ACTIVATE:
        trailing_active = True
    if trailing_active and drop_from_ath <= -TRAILING_STOP_PCT:
        return 1.0, "TRAILING_STOP"

    # TP3
    if not tp3_hit and change_pct >= TP3_PCT:
        return 1.0, "TP3"

    # TP2
    if not tp2_hit and change_pct >= TP2_PCT:
        frac = min(TP2_SELL_PCT / remaining if remaining > 0 else 1.0, 1.0)
        return frac, "TP2"

    # TP1
    if not tp1_hit and change_pct >= TP1_PCT:
        frac = min(TP1_SELL_PCT / remaining if remaining > 0 else 1.0, 1.0)
        return frac, "TP1"

    return 0.0, None


# ── Stop-Loss ────────────────────────────────────────────────────────────────

class TestStopLoss:

    def test_stop_loss_triggers_full_sell(self):
        """A drop beyond STOP_LOSS_PCT must trigger 100% sell."""
        fraction, stage = _decide(entry=1.00, current=0.75, highest=1.00,
                                   trailing_active=False)
        assert fraction == 1.0
        assert stage == "STOP_LOSS"

    def test_stop_loss_exact_boundary(self):
        """Exactly at the stop-loss threshold price must trigger."""
        price_at_sl = 1.00 * (1 - STOP_LOSS_PCT)
        fraction, stage = _decide(entry=1.00, current=price_at_sl, highest=1.00,
                                   trailing_active=False)
        assert fraction == 1.0
        assert stage == "STOP_LOSS"

    def test_no_stop_loss_small_drop(self):
        """A small drop well above the stop-loss threshold should NOT fire."""
        current = 1.00 * (1 - STOP_LOSS_PCT / 2)   # half way to SL
        fraction, stage = _decide(entry=1.00, current=current, highest=1.00,
                                   trailing_active=False)
        assert fraction == 0.0 and stage is None


# ── Trailing Stop ─────────────────────────────────────────────────────────────

class TestTrailingStop:

    def test_trailing_fires_after_big_drop_from_ath(self):
        """When trailing is active and price drops >TRAILING_STOP_PCT from ATH."""
        highest  = 1.00 * (1 + TRAILING_ACTIVATE + 0.10)  # peak well above activation
        current  = highest * (1 - TRAILING_STOP_PCT - 0.05)   # drop exceeds threshold
        fraction, stage = _decide(entry=1.00, current=current, highest=highest,
                                   trailing_active=True)
        assert fraction == 1.0
        assert stage == "TRAILING_STOP"

    def test_trailing_not_fire_small_pullback(self):
        """Small pullback from ATH should not trigger trailing stop."""
        highest = 1.00 * (1 + TRAILING_ACTIVATE + 0.10)
        current = highest * (1 - TRAILING_STOP_PCT / 2)   # half the drop needed
        fraction, stage = _decide(entry=1.00, current=current, highest=highest,
                                   trailing_active=True)
        # Could be TP1 if still profitable enough. Either way should NOT be TRAILING_STOP
        assert stage != "TRAILING_STOP"

    def test_trailing_activates_when_gain_reaches_threshold(self):
        """Passing TRAILING_ACTIVATE price should set trailing active internally."""
        entry   = 1.00
        current = entry * (1 + TRAILING_ACTIVATE + 0.05)
        highest = current
        # Even without trailing_active=True, the logic auto-activates it.
        # But then drop must also exceed threshold to sell — so no sell yet.
        fraction, stage = _decide(entry=entry, current=current, highest=highest,
                                   trailing_active=False)
        # No drop → no trailing sell yet
        assert stage != "TRAILING_STOP"

    def test_trailing_not_active_below_activation(self):
        """Trailing stop should never fire if price never passed TRAILING_ACTIVATE."""
        entry   = 1.00
        highest = entry * (1 + TRAILING_ACTIVATE - 0.05)   # just below activation
        current = highest * (1 - TRAILING_STOP_PCT - 0.10)  # big drop
        fraction, stage = _decide(entry=entry, current=current, highest=highest,
                                   trailing_active=False)
        # Should be stop-loss or nothing, NOT trailing stop
        assert stage != "TRAILING_STOP"


# ── Take-Profit ───────────────────────────────────────────────────────────────

class TestTakeProfit:

    def test_tp1_fires_at_threshold(self):
        """At TP1_PCT gain, should sell TP1_SELL_PCT of the position."""
        current = 1.00 * (1 + TP1_PCT + 0.05)
        fraction, stage = _decide(entry=1.00, current=current, highest=current,
                                   trailing_active=False)
        assert stage == "TP1"
        assert fraction == TP1_SELL_PCT   # remaining==1.0 so fraction == TP1_SELL_PCT

    def test_tp2_fires_after_tp1_hit(self):
        """After TP1 is hit (remaining=0.5), TP2 should sell TP2_SELL_PCT of original."""
        current   = 1.00 * (1 + TP2_PCT + 0.05)
        remaining = 1.0 - TP1_SELL_PCT
        fraction, stage = _decide(entry=1.00, current=current, highest=current,
                                   trailing_active=True,
                                   tp1_hit=True, remaining=remaining)
        assert stage == "TP2"
        expected = min(TP2_SELL_PCT / remaining, 1.0)
        assert abs(fraction - expected) < 1e-9

    def test_tp3_sells_all(self):
        """At TP3, 100% should be sold."""
        current = 1.00 * (1 + TP3_PCT + 0.10)
        fraction, stage = _decide(entry=1.00, current=current, highest=current,
                                   trailing_active=True,
                                   tp1_hit=True, tp2_hit=True, remaining=0.25)
        assert stage == "TP3"
        assert fraction == 1.0

    def test_no_tp_below_tp1_threshold(self):
        """No TP fires if gain is below TP1."""
        current = 1.00 * (1 + TP1_PCT - 0.05)   # just below TP1
        fraction, stage = _decide(entry=1.00, current=current, highest=current,
                                   trailing_active=False)
        assert stage is None
        assert fraction == 0.0


# ── Sell fraction math ────────────────────────────────────────────────────────

class TestSellFractionMath:

    def test_wallet_fraction_half(self):
        wallet_balance = 1_000_000
        sold = int(wallet_balance * 0.5)
        assert sold == 500_000

    def test_wallet_fraction_full(self):
        wallet_balance = 2_500_000
        sold = int(wallet_balance * 1.0)
        assert sold == 2_500_000

    def test_tp2_fraction_after_tp1(self):
        """After TP1 sold 50%, remaining is 50%. TP2 should sell 25%/50% = 50% of wallet."""
        remaining = 1.0 - TP1_SELL_PCT          # 0.5
        frac = min(TP2_SELL_PCT / remaining, 1.0)
        assert abs(frac - 0.5) < 1e-9, f"Expected 50% wallet sell, got {frac:.2%}"
