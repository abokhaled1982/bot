"""
src/bot/filters.py — Token evaluation filters and scoring.

All scoring / filtering logic lives here.  Nothing in this module
makes network calls or writes to the database.
"""
from __future__ import annotations
import time

# ── Permanently blocked mints (stablecoins, wrapped SOL, etc.) ───────────────
BLOCKED_TOKENS: frozenset[str] = frozenset({
    "So11111111111111111111111111111111111111112",    # Wrapped SOL
    "Es9vMFrzaCERmJfrF4H2FYD4KCoNkY11McCe8BenwNYB",  # USDT
    "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v",  # USDC
    "mSoLzYCxHdYgdzU16g5QSh3i5K3z3KZK7ytfqcJm7So",   # mSOL
    "7dHbWXmci3dT8UFYWYZweBLXgycu7Y3iL6trKn1Y7ARj",  # stSOL
    "bSo13r4TkiE4KumL71LsHTPpL2euBYLFx6h9HP3piy1",   # bSOL
    "J1toso1uCk3RLmjorhTtrVwY9HJ7X8V9yYac6Y7kGCPn",  # JitoSOL
    "JUPyiwrYJFskUPiHa7hkeR8VUtAeFoSYbKedZNsDvCN",    # JUP
    "7vfCXTUXx5WJV5JADk17DUJ4ksgau7utNKj4b963voxs",  # RAY
})


# ── Helpers ───────────────────────────────────────────────────────────────────

def get_token_age_hours(token_data: dict) -> float:
    """Return how old the trading pair is in hours. -1 if unknown."""
    created_at = token_data.get("pair_created_at", 0)
    if not created_at:
        return -1.0
    age_ms = (time.time() * 1_000) - float(created_at)
    return max(0.0, age_ms / (1_000 * 60 * 60))


# ── Hype Score (0–100) ────────────────────────────────────────────────────────

def calculate_hype_score(token_data: dict) -> int:
    """Score a token on pure momentum signals.  Returns 0–100."""
    score     = 0
    spike     = float(token_data.get("volume_spike", 0))
    change_1h = float(token_data.get("change_1h",    0))
    change_5m = float(token_data.get("change_5m",    0))
    liq       = float(token_data.get("liquidity_usd", 0))
    buys_h1   = int(token_data.get("buys_h1",   0))
    sells_h1  = int(token_data.get("sells_h1",  0))

    # Volume Spike (30 pts)
    if   spike >= 10:  score += 30
    elif spike >= 5:   score += 22
    elif spike >= 3:   score += 15
    elif spike >= 1.5: score += 8

    # 1 h change (25 pts)
    if   change_1h >= 50:  score += 25
    elif change_1h >= 20:  score += 18
    elif change_1h >= 10:  score += 12
    elif change_1h >= 5:   score += 8
    elif change_1h >= 0:   score += 3
    elif change_1h < -10:  score -= 20
    elif change_1h < 0:    score -= 10

    # 5 m change (15 pts)
    if   change_5m >= 20: score += 15
    elif change_5m >= 10: score += 12
    elif change_5m >= 5:  score += 8
    elif change_5m >= 2:  score += 4
    elif change_5m < -5:  score -= 10

    # Liquidity (15 pts)
    if   liq >= 100_000: score += 15
    elif liq >= 50_000:  score += 12
    elif liq >= 20_000:  score += 8
    elif liq >= 10_000:  score += 5
    elif liq >= 5_000:   score += 3
    elif liq < 1_000:    score -= 15

    # Buy/Sell pressure (15 pts)
    total_txns = buys_h1 + sells_h1
    if total_txns > 10:
        buy_ratio = buys_h1 / total_txns
        if   buy_ratio >= 0.70: score += 15
        elif buy_ratio >= 0.60: score += 10
        elif buy_ratio >= 0.50: score += 5
        elif buy_ratio < 0.35:  score -= 10

    return max(0, min(100, score))


# ── Risk Flags ────────────────────────────────────────────────────────────────

_CRITICAL_FLAGS = frozenset({
    "Low_Liquidity", "Rugpull_Hint", "Falling_Fast",
    "Dumping_Now", "Too_New", "Heavy_Selling", "Already_Mooned",
    "Wash_Trading_Suspect", "Liquidity_Drain", "Sell_Accelerating",
})


def get_risk_flags(
    token_data:   dict,
    top_10_pct:   float,
    is_migration: bool = False,
) -> list[str]:
    """Return a list of risk flag strings.  Returns ['No_Risk_Flags'] if clean."""
    flags:   list[str] = []
    liq      = float(token_data.get("liquidity_usd", 0))
    spike    = float(token_data.get("volume_spike",  0))
    ch_1h    = float(token_data.get("change_1h",     0))
    ch_24h   = float(token_data.get("change_24h",    0))
    ch_5m    = float(token_data.get("change_5m",     0))
    mcap     = float(token_data.get("market_cap",    0))
    buys_h1  = int(token_data.get("buys_h1",  0))
    sells_h1 = int(token_data.get("sells_h1", 0))
    vol_h24  = float(token_data.get("volume_24h", 0))
    age_h    = get_token_age_hours(token_data)

    liq_min  = 3_000 if is_migration else 5_000
    if liq < liq_min:
        flags.append("Low_Liquidity")
    if mcap > 0 and liq > 0 and (liq / mcap) < 0.03:
        flags.append("Thin_Liquidity_Ratio")
    if top_10_pct > 60:
        flags.append("Whale_Concentration")
    if ch_1h < -20:
        flags.append("Falling_Fast")
    if ch_5m < -10:
        flags.append("Dumping_Now")
    if ch_24h < -50:
        flags.append("Rugpull_Hint")
    if ch_1h > 200:
        flags.append("Extreme_Pump")
    if ch_24h > 500:
        flags.append("Already_Mooned")
    if spike > 20 and ch_1h > 100:
        flags.append("Pump_Suspicion")

    total_txns = buys_h1 + sells_h1
    if vol_h24 > 50_000 and total_txns < 20:
        flags.append("Wash_Trading_Suspect")
    if sells_h1 > buys_h1 * 2 and sells_h1 > 20:
        flags.append("Heavy_Selling")

    buys_24h  = int(token_data.get("buys_h24",  0))
    sells_24h = int(token_data.get("sells_h24", 0))
    if buys_24h > 0 and sells_24h > 0 and total_txns > 20:
        recent_sell_ratio = sells_h1 / max(buys_h1, 1)
        daily_sell_ratio  = sells_24h / max(buys_24h, 1)
        if recent_sell_ratio > daily_sell_ratio * 1.5 and recent_sell_ratio > 1.2:
            flags.append("Sell_Accelerating")

    if not is_migration and 0 <= age_h < 1:
        flags.append("Too_New")

    if ch_1h < -30 and liq > 0 and vol_h24 > liq * 3:
        flags.append("Liquidity_Drain")

    return flags if flags else ["No_Risk_Flags"]


# ── Pre-Buy Filters ───────────────────────────────────────────────────────────

def pre_buy_filter(
    token_data: dict,
    risk_flags: list[str],
) -> tuple[bool, str]:
    """
    Strict filter for standard (non-migration) tokens.
    Returns (pass, reject_reason).
    """
    liq    = float(token_data.get("liquidity_usd", 0))
    ch_1h  = float(token_data.get("change_1h",    0))
    ch_5m  = float(token_data.get("change_5m",    0))
    ch_24h = float(token_data.get("change_24h",   0))
    spike  = float(token_data.get("volume_spike",  0))
    mcap   = float(token_data.get("market_cap",    0))
    buys   = int(token_data.get("buys_h1",  0))
    sells  = int(token_data.get("sells_h1", 0))
    age_h  = get_token_age_hours(token_data)

    if liq < 5_000:
        return False, f"Liq zu niedrig: ${liq:,.0f} (min $5k)"
    if ch_1h < 0:
        return False, f"Token fällt: 1h {ch_1h:+.1f}%"
    if ch_5m < -5:
        return False, f"Token dumpt: 5m {ch_5m:+.1f}%"
    if ch_24h < -80:
        return False, f"24h Crash: {ch_24h:+.1f}%"

    min_spike = 1.3 if age_h > 168 else 2.0
    if spike < min_spike:
        return False, f"Volume Spike zu niedrig: {spike:.1f}x (min {min_spike}x)"
    if 0 <= age_h < 1:
        return False, f"Token zu neu: {age_h:.1f}h (min 1h)"
    if age_h > 72 and ch_24h < 5 and spike < 1.5 and (buys + sells) < 20:
        return False, f"Token tot: {age_h:.0f}h alt, kein Momentum"
    if mcap > 0 and mcap < 10_000:
        return False, f"MCap zu klein: ${mcap:,.0f} (min $10k)"
    if mcap > 200_000_000:
        return False, f"MCap zu hoch: ${mcap:,.0f} (kein Memecoin)"
    if ch_24h > 500:
        return False, f"Bereits gemooned: 24h {ch_24h:+.1f}%"

    total_txns = buys + sells
    if total_txns < 10:
        return False, f"Zu wenig Aktivität: {total_txns} txns (min 10)"
    if total_txns > 20:
        buy_ratio = buys / total_txns
        if buy_ratio < 0.35:
            return False, f"Zu viel Verkaufsdruck: {buy_ratio:.0%} buys ({buys}/{sells})"

    for flag in _CRITICAL_FLAGS:
        if flag in risk_flags:
            return False, f"Critical Flag: {flag}"

    return True, ""


def pre_buy_filter_migration(
    token_data: dict,
    risk_flags: list[str],
) -> tuple[bool, str]:
    """
    Relaxed filter for Pump.fun migration tokens (brand-new graduates).
    Returns (pass, reject_reason).
    """
    liq    = float(token_data.get("liquidity_usd", 0))
    ch_5m  = float(token_data.get("change_5m",    0))
    ch_1h  = float(token_data.get("change_1h",    0))
    mcap   = float(token_data.get("market_cap",    0))
    buys   = int(token_data.get("buys_h1",  0))
    sells  = int(token_data.get("sells_h1", 0))

    if liq < 3_000:
        return False, f"Migration Liq zu niedrig: ${liq:,.0f} (min $3k)"
    if ch_5m < -15:
        return False, f"Migration dumpt: 5m {ch_5m:+.1f}%"
    if ch_1h < -30:
        return False, f"Migration fällt: 1h {ch_1h:+.1f}%"

    total_txns = buys + sells
    if total_txns < 5:
        return False, f"Zu wenig Aktivität: {total_txns} txns (min 5)"
    if total_txns > 10:
        buy_ratio = buys / total_txns
        if buy_ratio < 0.40:
            return False, f"Schwacher Kaufdruck: {buy_ratio:.0%} buys ({buys}/{sells})"
    if sells > buys * 2 and sells > 15:
        return False, f"Heavy selling: {buys} buys / {sells} sells"
    if mcap > 0 and mcap > 10_000_000:
        return False, f"MCap zu hoch für Migration: ${mcap:,.0f}"

    migration_critical = {
        "Rugpull_Hint", "Dumping_Now", "Heavy_Selling",
        "Wash_Trading_Suspect", "Liquidity_Drain",
    }
    for flag in migration_critical:
        if flag in risk_flags:
            return False, f"Critical Flag: {flag}"

    return True, ""
