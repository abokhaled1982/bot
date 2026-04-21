"""
Unit tests for the bot's buy/sell filter logic.
These test ALL the decision functions without needing network access or a real wallet.

Run:
    cd /home/alghobariw/.openclaw/workspace/memecoin_bot
    source venv/bin/activate
    python -m pytest tests/test_filters.py -v
"""
import sys
import os
sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

from src.bot.filters import (
    pre_buy_filter,
    pre_buy_filter_migration,
    get_risk_flags,
    calculate_hype_score,
)


# ── Helpers ─────────────────────────────────────────────────────────────────

def _token(
    liquidity_usd=50_000,
    change_1h=15.0,
    change_5m=3.0,
    change_24h=30.0,
    volume_spike=4.0,
    market_cap=500_000,
    buys_h1=80,
    sells_h1=40,
    volume_24h=100_000,
    buys_h24=400,
    sells_h24=200,
    pair_created_at=None,
    price_usd=0.000010,
):
    """Return a token_data dict that passes all filters by default."""
    import time
    if pair_created_at is None:
        pair_created_at = int((time.time() - 3600 * 5) * 1000)   # 5h ago
    return {
        "liquidity_usd": liquidity_usd,
        "change_1h":     change_1h,
        "change_5m":     change_5m,
        "change_24h":    change_24h,
        "volume_spike":  volume_spike,
        "market_cap":    market_cap,
        "buys_h1":       buys_h1,
        "sells_h1":      sells_h1,
        "volume_24h":    volume_24h,
        "buys_h24":      buys_h24,
        "sells_h24":     sells_h24,
        "pair_created_at": pair_created_at,
        "price_usd":     price_usd,
        "vol_mcap_ratio": volume_24h / market_cap if market_cap > 0 else 0,
    }


# ── pre_buy_filter ───────────────────────────────────────────────────────────

class TestPreBuyFilter:

    def test_good_token_passes(self):
        ok, reason = pre_buy_filter(_token(), ["No_Risk_Flags"])
        assert ok, f"Good token should pass: {reason}"

    def test_low_liquidity_rejected(self):
        ok, reason = pre_buy_filter(_token(liquidity_usd=3_000), ["Low_Liquidity"])
        assert not ok
        assert "Liq" in reason

    def test_falling_1h_rejected(self):
        ok, reason = pre_buy_filter(_token(change_1h=-5.0), ["No_Risk_Flags"])
        assert not ok
        assert "fällt" in reason

    def test_dumping_5m_rejected(self):
        ok, reason = pre_buy_filter(_token(change_5m=-8.0), ["No_Risk_Flags"])
        assert not ok
        assert "dumpt" in reason

    def test_already_mooned_rejected(self):
        ok, reason = pre_buy_filter(_token(change_24h=600.0), ["Already_Mooned"])
        assert not ok

    def test_low_volume_spike_rejected(self):
        ok, reason = pre_buy_filter(_token(volume_spike=0.8), ["No_Risk_Flags"])
        assert not ok
        assert "Spike" in reason

    def test_too_new_rejected(self):
        import time
        fresh = int(time.time() * 1000)   # just now
        ok, reason = pre_buy_filter(_token(pair_created_at=fresh), ["Too_New"])
        assert not ok

    def test_heavy_selling_flag_rejected(self):
        # buys=10, sells=80 → buy ratio 11% → rejected by buy-pressure check first
        ok, reason = pre_buy_filter(_token(buys_h1=10, sells_h1=80), ["Heavy_Selling"])
        assert not ok
        assert "Verkauf" in reason or "Heavy_Selling" in reason

    def test_too_small_mcap_rejected(self):
        ok, reason = pre_buy_filter(_token(market_cap=5_000), ["No_Risk_Flags"])
        assert not ok
        assert "MCap" in reason

    def test_sell_pressure_rejected(self):
        ok, reason = pre_buy_filter(_token(buys_h1=10, sells_h1=60), ["No_Risk_Flags"])
        assert not ok
        assert "Verkauf" in reason or "buy" in reason.lower()

    def test_dead_token_rejected(self):
        import time
        old = int((time.time() - 3600 * 100) * 1000)  # 100h ago
        ok, reason = pre_buy_filter(_token(
            pair_created_at=old, change_24h=1.0, volume_spike=1.0, buys_h1=5, sells_h1=3
        ), ["No_Risk_Flags"])
        assert not ok


# ── pre_buy_filter_migration ─────────────────────────────────────────────────

class TestPreBuyFilterMigration:

    def _mig_token(self, **kwargs):
        """Migration tokens are brand new — no age check."""
        import time
        kwargs.setdefault("pair_created_at", int(time.time() * 1000))
        kwargs.setdefault("volume_spike", 1.0)      # spike not required for migrations
        kwargs.setdefault("change_24h", 5.0)
        return _token(**kwargs)

    def test_good_migration_passes(self):
        ok, reason = pre_buy_filter_migration(self._mig_token(), ["No_Risk_Flags"])
        assert ok, f"Good migration should pass: {reason}"

    def test_low_liq_rejected(self):
        ok, reason = pre_buy_filter_migration(self._mig_token(liquidity_usd=1_000), [])
        assert not ok
        assert "Liq" in reason

    def test_heavy_dump_rejected(self):
        ok, reason = pre_buy_filter_migration(self._mig_token(change_5m=-20.0), [])
        assert not ok

    def test_high_mcap_rejected(self):
        ok, reason = pre_buy_filter_migration(self._mig_token(market_cap=15_000_000), [])
        assert not ok
        assert "MCap" in reason

    def test_critical_flag_rejected(self):
        ok, reason = pre_buy_filter_migration(self._mig_token(), ["Rugpull_Hint"])
        assert not ok
        assert "Rugpull_Hint" in reason


# ── get_risk_flags ───────────────────────────────────────────────────────────

class TestGetRiskFlags:

    def test_clean_token_no_flags(self):
        flags = get_risk_flags(_token(), top_10_pct=30)
        assert "No_Risk_Flags" in flags

    def test_low_liq_flagged(self):
        flags = get_risk_flags(_token(liquidity_usd=2_000), top_10_pct=30)
        assert "Low_Liquidity" in flags

    def test_whale_concentration(self):
        flags = get_risk_flags(_token(), top_10_pct=75)
        assert "Whale_Concentration" in flags

    def test_falling_fast(self):
        flags = get_risk_flags(_token(change_1h=-30), top_10_pct=30)
        assert "Falling_Fast" in flags

    def test_dumping_now(self):
        flags = get_risk_flags(_token(change_5m=-15), top_10_pct=30)
        assert "Dumping_Now" in flags

    def test_rugpull_hint(self):
        flags = get_risk_flags(_token(change_24h=-60), top_10_pct=30)
        assert "Rugpull_Hint" in flags

    def test_pump_suspicion(self):
        flags = get_risk_flags(_token(volume_spike=25, change_1h=150), top_10_pct=30)
        assert "Pump_Suspicion" in flags

    def test_heavy_selling(self):
        flags = get_risk_flags(_token(buys_h1=10, sells_h1=60), top_10_pct=30)
        assert "Heavy_Selling" in flags

    def test_already_mooned(self):
        flags = get_risk_flags(_token(change_24h=600), top_10_pct=30)
        assert "Already_Mooned" in flags

    def test_migration_skips_age_check(self):
        import time
        fresh = int(time.time() * 1000)
        flags = get_risk_flags(_token(pair_created_at=fresh), top_10_pct=30, is_migration=True)
        assert "Too_New" not in flags

    def test_non_migration_too_new(self):
        import time
        fresh = int(time.time() * 1000)
        flags = get_risk_flags(_token(pair_created_at=fresh), top_10_pct=30, is_migration=False)
        assert "Too_New" in flags


# ── calculate_hype_score ─────────────────────────────────────────────────────

class TestHypeScore:

    def test_high_spike_high_score(self):
        score = calculate_hype_score(_token(volume_spike=15, change_1h=60, change_5m=25,
                                             liquidity_usd=80_000, buys_h1=80, sells_h1=20))
        assert score >= 70, f"High momentum should score >= 70, got {score}"

    def test_bad_token_low_score(self):
        score = calculate_hype_score(_token(volume_spike=0.5, change_1h=-15, change_5m=-8,
                                             liquidity_usd=500, buys_h1=3, sells_h1=20))
        assert score < 30, f"Bad token should score < 30, got {score}"

    def test_score_clamped_0_100(self):
        s1 = calculate_hype_score(_token(volume_spike=100, change_1h=500, change_5m=200,
                                          liquidity_usd=1_000_000, buys_h1=1000, sells_h1=1))
        s2 = calculate_hype_score(_token(volume_spike=0, change_1h=-100, change_5m=-100,
                                          liquidity_usd=10, buys_h1=0, sells_h1=1000))
        assert 0 <= s1 <= 100
        assert 0 <= s2 <= 100
