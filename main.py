import asyncio
import sys
import os
import sqlite3
import json
import aiohttp
from datetime import datetime
from loguru import logger

sys.path.append(os.path.join(os.path.dirname(__file__), "src"))

def log_to_db(msg):
    level   = msg.record["level"].name
    message = msg.record["message"]
    conn = sqlite3.connect("memecoin_bot.db")
    c = conn.cursor()
    c.execute(
        "INSERT INTO bot_logs (level, message, timestamp) VALUES (?, ?, ?)",
        (level, message, datetime.now())
    )
    conn.commit()
    conn.close()

logger.add(log_to_db)

from src.adapters.dexscreener import DexScreenerAdapter
from src.adapters.pumpfun import PumpFunAdapter
from src.adapters.safety import SafetyAdapter
from src.adapters.solana_chain import SolanaAdapter
from src.analysis.fusion import SignalFusion
from src.execution.executor import TradeExecutor
from src.execution.monitor import PositionMonitor
from notify_whatsapp import send_whatsapp_update

# ── Gekaufte Adressen in dieser Session (Duplikat-Schutz) ─────────────────────
BOUGHT_THIS_SESSION: set = set()

# ── Migration Watch List ─────────────────────────────────────────────────────
# Tokens die bei der ersten DexScreener-Abfrage keine Daten hatten.
# Werden alle 30s erneut geprüft, bis DexScreener sie indexiert hat (max 10 Min).
import time as _time

MIGRATION_WATCHLIST: dict = {}   # {address: {token_data, added_at, retries}}
WATCHLIST_MAX_AGE_SEC = 600      # 10 Minuten max warten
WATCHLIST_MAX_RETRIES = 20       # 20 × 30s = 10 Min

# ── Token-Blocklist (nie kaufen) ─────────────────────────────────────────────
BLOCKED_TOKENS: set = {
    "So11111111111111111111111111111111111111112",   # SOL (Wrapped)
    "Es9vMFrzaCERmJfrF4H2FYD4KCoNkY11McCe8BenwNYB", # USDT
    "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v", # USDC
    "mSoLzYCxHdYgdzU16g5QSh3i5K3z3KZK7ytfqcJm7So",  # mSOL
    "7dHbWXmci3dT8UFYWYZweBLXgycu7Y3iL6trKn1Y7ARj", # stSOL
    "bSo13r4TkiE4KumL71LsHTPpL2euBYLFx6h9HP3piy1",  # bSOL
    "J1toso1uCk3RLmjorhTtrVwY9HJ7X8V9yYac6Y7kGCPn", # JitoSOL
    "DezXAZ8z7PnrnRJjz3wXBoRgixCa6xjnB7YaB1pPB263", # BONK
    "JUPyiwrYJFskUPiHa7hkeR8VUtAeFoSYbKedZNsDvCN",   # JUP
    "7vfCXTUXx5WJV5JADk17DUJ4ksgau7utNKj4b963voxs", # RAY (Raydium)
}


async def get_btc_change() -> float:
    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(
                "https://api.coingecko.com/api/v3/simple/price"
                "?ids=bitcoin&vs_currencies=usd&include_24hr_change=true",
                timeout=aiohttp.ClientTimeout(total=8)
            ) as r:
                if r.status == 200:
                    data   = await r.json()
                    change = float(data["bitcoin"].get("usd_24h_change", 0))
                    return round(change / 24, 4)
    except Exception as e:
        logger.warning(f"BTC Preis Fehler: {e}")
    return 0.0


def calculate_hype_score(token_data: dict) -> int:
    score = 0
    spike     = float(token_data.get("volume_spike", 0))
    change_1h = float(token_data.get("change_1h",    0))
    change_5m = float(token_data.get("change_5m",    0))
    liq       = float(token_data.get("liquidity_usd",0))
    buys_h1   = int(token_data.get("buys_h1",  0))
    sells_h1  = int(token_data.get("sells_h1", 0))

    # Volume Spike (30 Pkt)
    if   spike >= 10: score += 30
    elif spike >= 5:  score += 22
    elif spike >= 3:  score += 15
    elif spike >= 1.5: score += 8

    # 1h Change (25 Pkt) — negativ = Punkte abziehen
    if   change_1h >= 50:  score += 25
    elif change_1h >= 20:  score += 18
    elif change_1h >= 10:  score += 12
    elif change_1h >= 5:   score += 8
    elif change_1h >= 0:   score += 3
    elif change_1h < -10:  score -= 20
    elif change_1h < 0:    score -= 10

    # 5m Change (15 Pkt)
    if   change_5m >= 20: score += 15
    elif change_5m >= 10: score += 12
    elif change_5m >= 5:  score += 8
    elif change_5m >= 2:  score += 4
    elif change_5m < -5:  score -= 10

    # Liquidität (15 Pkt)
    if   liq >= 100_000: score += 15
    elif liq >= 50_000:  score += 12
    elif liq >= 20_000:  score += 8
    elif liq >= 10_000:  score += 5
    elif liq >= 5_000:   score += 3
    elif liq < 1_000:    score -= 15

    # Buy/Sell Pressure (15 Pkt) — more buys than sells = bullish
    total_txns = buys_h1 + sells_h1
    if total_txns > 10:
        buy_ratio = buys_h1 / total_txns
        if   buy_ratio >= 0.70: score += 15
        elif buy_ratio >= 0.60: score += 10
        elif buy_ratio >= 0.50: score += 5
        elif buy_ratio < 0.35:  score -= 10  # heavy selling

    return max(0, min(100, score))


def get_token_age_hours(token_data: dict) -> float:
    """Calculate token age in hours from pair creation timestamp."""
    created_at = token_data.get("pair_created_at", 0)
    if not created_at:
        return -1  # unknown
    import time
    age_ms = (time.time() * 1000) - created_at
    return max(0, age_ms / (1000 * 60 * 60))


def get_risk_flags(token_data: dict, top_10_pct: float, is_migration: bool = False) -> list:
    flags   = []
    liq     = float(token_data.get("liquidity_usd", 0))
    spike   = float(token_data.get("volume_spike",  0))
    ch_1h   = float(token_data.get("change_1h",     0))
    ch_24h  = float(token_data.get("change_24h",    0))
    ch_5m   = float(token_data.get("change_5m",     0))
    mcap    = float(token_data.get("market_cap",     0))
    buys_h1 = int(token_data.get("buys_h1",  0))
    sells_h1= int(token_data.get("sells_h1", 0))
    vol_h24 = float(token_data.get("volume_24h", 0))
    age_h   = get_token_age_hours(token_data)

    # ── Liquidity checks ──────────────────────────────────────────────────────
    # Migration tokens get lower liquidity threshold (they're brand new)
    liq_min = 3_000 if is_migration else 5_000
    if liq < liq_min:
        flags.append("Low_Liquidity")

    if mcap > 0 and liq > 0 and (liq / mcap) < 0.03:
        flags.append("Thin_Liquidity_Ratio")

    # ── Holder / whale checks ─────────────────────────────────────────────────
    if top_10_pct > 60:
        flags.append("Whale_Concentration")

    # ── Price movement checks ─────────────────────────────────────────────────
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

    # ── Volume / manipulation checks ──────────────────────────────────────────
    if spike > 20 and ch_1h > 100:
        flags.append("Pump_Suspicion")

    # Wash trading detection — very high volume but very few transactions
    total_txns = buys_h1 + sells_h1
    if vol_h24 > 50_000 and total_txns < 20:
        flags.append("Wash_Trading_Suspect")

    # ── Sell pressure checks ──────────────────────────────────────────────────
    if sells_h1 > buys_h1 * 2 and sells_h1 > 20:
        flags.append("Heavy_Selling")

    # Sell acceleration — sells increasing faster than buys in recent window
    buys_24h  = int(token_data.get("buys_h24", 0))
    sells_24h = int(token_data.get("sells_h24", 0))
    if buys_24h > 0 and sells_24h > 0 and total_txns > 20:
        recent_sell_ratio = sells_h1 / max(buys_h1, 1)
        daily_sell_ratio  = sells_24h / max(buys_24h, 1)
        if recent_sell_ratio > daily_sell_ratio * 1.5 and recent_sell_ratio > 1.2:
            flags.append("Sell_Accelerating")

    # ── Age checks — skip for migrations (they're new by definition) ──────────
    if not is_migration:
        if 0 <= age_h < 1:
            flags.append("Too_New")

    # ── Liquidity drain detection ─────────────────────────────────────────────
    # If price is crashing AND liquidity is very low relative to volume,
    # someone might be pulling liquidity
    if ch_1h < -30 and liq > 0 and vol_h24 > liq * 3:
        flags.append("Liquidity_Drain")

    if not flags:
        flags.append("No_Risk_Flags")
    return flags


# ── PRE-BUY FILTER: Standard tokens (DexScreener, older tokens) ──────────────
def pre_buy_filter(token_data: dict, risk_flags: list) -> tuple[bool, str]:
    """
    Strict filter for standard tokens (DexScreener trending/boosted).
    Returns (True, '') if OK to buy, (False, 'reason') if skip.
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

    # ── Hard filters (instant reject) ─────────────────────────────────────────

    # Minimum liquidity
    if liq < 5_000:
        return False, f"Liq zu niedrig: ${liq:,.0f} (min $5k)"

    # Don't buy while 1h is falling
    if ch_1h < 0:
        return False, f"Token fällt: 1h {ch_1h:+.1f}%"

    # Don't buy while actively dumping
    if ch_5m < -5:
        return False, f"Token dumpt: 5m {ch_5m:+.1f}%"

    # 24h crash
    if ch_24h < -80:
        return False, f"24h Crash: {ch_24h:+.1f}%"

    # Must have volume spike
    if spike < 2:
        return False, f"Volume Spike zu niedrig: {spike:.1f}x (min 2x)"

    # Token age — too new = rug risk
    if 0 <= age_h < 1:
        return False, f"Token zu neu: {age_h:.1f}h (min 1h)"

    # Dead token — old with no momentum
    if age_h > 72 and ch_24h < 5 and spike < 3:
        return False, f"Token tot: {age_h:.0f}h alt, kein Momentum"

    # Market cap sanity
    if mcap > 0 and mcap < 10_000:
        return False, f"MCap zu klein: ${mcap:,.0f} (min $10k)"
    if mcap > 50_000_000:
        return False, f"MCap zu hoch: ${mcap:,.0f} (kein Memecoin)"

    # Anti-FOMO
    if ch_24h > 500:
        return False, f"Bereits gemooned: 24h {ch_24h:+.1f}%"

    # ── Soft filters (buy pressure check) ─────────────────────────────────────

    # Must have minimum transaction count (avoid illiquid/dead tokens)
    total_txns = buys + sells
    if total_txns < 10:
        return False, f"Zu wenig Aktivität: {total_txns} txns (min 10)"

    # Buy pressure should be positive
    if total_txns > 20:
        buy_ratio = buys / total_txns
        if buy_ratio < 0.35:
            return False, f"Zu viel Verkaufsdruck: {buy_ratio:.0%} buys ({buys}/{sells})"

    # ── Critical risk flags ───────────────────────────────────────────────────
    critical = [
        "Low_Liquidity", "Rugpull_Hint", "Falling_Fast",
        "Dumping_Now", "Too_New", "Heavy_Selling", "Already_Mooned",
        "Wash_Trading_Suspect", "Liquidity_Drain", "Sell_Accelerating",
    ]
    for flag in critical:
        if flag in risk_flags:
            return False, f"Critical Flag: {flag}"

    return True, ""


# ── PRE-BUY FILTER: Migration tokens (Pump.fun graduates) ────────────────────
def pre_buy_filter_migration(token_data: dict, risk_flags: list) -> tuple[bool, str]:
    """
    Relaxed filter for Pump.fun migration tokens.
    These JUST graduated to Raydium — they're new by definition.
    We skip age/spike checks but keep safety and momentum checks tight.
    """
    liq    = float(token_data.get("liquidity_usd", 0))
    ch_5m  = float(token_data.get("change_5m",    0))
    ch_1h  = float(token_data.get("change_1h",    0))
    mcap   = float(token_data.get("market_cap",    0))
    buys   = int(token_data.get("buys_h1",  0))
    sells  = int(token_data.get("sells_h1", 0))

    # ── Hard filters ──────────────────────────────────────────────────────────

    # Must have minimum liquidity (lower threshold — they just launched)
    if liq < 3_000:
        return False, f"Migration Liq zu niedrig: ${liq:,.0f} (min $3k)"

    # Not actively dumping hard
    if ch_5m < -15:
        return False, f"Migration dumpt: 5m {ch_5m:+.1f}%"

    # 1h shouldn't be in free fall
    if ch_1h < -30:
        return False, f"Migration fällt: 1h {ch_1h:+.1f}%"

    # Must have some buy activity
    total_txns = buys + sells
    if total_txns < 5:
        return False, f"Zu wenig Aktivität: {total_txns} txns (min 5)"

    # ── Momentum checks (key for migrations) ─────────────────────────────────

    # Buy pressure must be positive — this is crucial for fresh migrations
    if total_txns > 10:
        buy_ratio = buys / total_txns
        if buy_ratio < 0.40:
            return False, f"Schwacher Kaufdruck: {buy_ratio:.0%} buys ({buys}/{sells})"

    # If there are enough transactions, check for sell acceleration
    if sells > buys * 2 and sells > 15:
        return False, f"Heavy selling: {buys} buys / {sells} sells"

    # ── Market cap sanity ─────────────────────────────────────────────────────
    if mcap > 0 and mcap > 10_000_000:
        return False, f"MCap zu hoch für Migration: ${mcap:,.0f}"

    # ── Critical flags ────────────────────────────────────────────────────────
    # Fewer critical flags for migrations — we expect some volatility
    critical = [
        "Rugpull_Hint", "Dumping_Now", "Heavy_Selling",
        "Wash_Trading_Suspect", "Liquidity_Drain",
    ]
    for flag in critical:
        if flag in risk_flags:
            return False, f"Critical Flag: {flag}"

    return True, ""


async def evaluate_token(
    token: dict,
    dex: DexScreenerAdapter,
    safety: SafetyAdapter,
    chain: SolanaAdapter,
    fusion: SignalFusion,
    executor: TradeExecutor,
    monitor: PositionMonitor,
    btc_change: float,
    is_migration: bool = False,
) -> bool:
    """
    Evaluate a single token through the full pipeline.
    Returns True if bought, False otherwise.
    """
    global BOUGHT_THIS_SESSION

    address = token.get("address")
    if not address:
        return False

    # Blocklist — SOL, USDC, stablecoins, base tokens sofort raus
    if address in BLOCKED_TOKENS:
        return False

    source = token.get("source", "unknown")

    # Duplicate check
    if address in BOUGHT_THIS_SESSION:
        return False
    if address in monitor.positions:
        return False

    symbol     = token.get("symbol") or "UNKNOWN"
    src_tag    = "MIGRATION" if is_migration else source.upper()
    gates      = []   # Track which gates this token passed

    # ── GATE 1: DexScreener Data ─────────────────────────────────────────
    token_data = await dex.get_token_data(address)
    if not token_data:
        if is_migration and address not in MIGRATION_WATCHLIST:
            # Migration-Token noch nicht indexiert → Watchlist statt Reject
            MIGRATION_WATCHLIST[address] = {
                "token":    token,
                "added_at": _time.time(),
                "retries":  0,
            }
            logger.info(
                f"[{symbol}] [{src_tag}] Gate 1 WAIT: DexScreener noch nicht indexiert "
                f"→ Watchlist ({len(MIGRATION_WATCHLIST)} tokens wartend)"
            )
            return False

        reason = "Keine DexScreener-Daten"
        if is_migration:
            retries = MIGRATION_WATCHLIST.get(address, {}).get("retries", 0)
            reason = f"Migration nach {retries} Retries immer noch nicht indexiert"
        logger.info(f"[{symbol}] [{src_tag}] Gate 1 FAIL: {reason}")
        await executor.execute_trade(symbol, address, 0, "SKIP",
            price=0, rejection_reason=f"[G1 Data] {reason}",
            funnel_stage="DATA_CHECK", gates_passed="")
        return False

    gates.append("G1:Data")
    # Aus Watchlist entfernen falls vorhanden (DexScreener hat jetzt Daten)
    if address in MIGRATION_WATCHLIST:
        wait_sec = _time.time() - MIGRATION_WATCHLIST[address]["added_at"]
        logger.info(f"[{symbol}] Watchlist → DexScreener indexiert nach {wait_sec:.0f}s")
        del MIGRATION_WATCHLIST[address]

    symbol    = token_data.get("symbol", symbol)
    price_usd = token_data.get("price_usd", 0)
    liq       = token_data.get("liquidity_usd", 0)
    spike     = token_data.get("volume_spike", 0)
    ch_1h     = token_data.get("change_1h", 0)
    ch_5m     = token_data.get("change_5m", 0)

    # ── Basis-Info (wird ab hier bei JEDEM Gate mitgegeben) ──────────────
    age_hours = get_token_age_hours(token_data)
    pair_created_ms = token_data.get("pair_created_at", 0)
    scan_ts = _time.time()
    pumpfun_detected = token.get("pumpfun_detected_at", 0)

    base_info = {
        "source": src_tag,
        "is_migration": is_migration,
        "pair_created_at": pair_created_ms,
        "token_age_hours": round(age_hours, 2),
        "scanned_at": scan_ts,
        "pumpfun_detected_at": pumpfun_detected,
        "market_data": {
            "liquidity_usd": liq,
            "market_cap": token_data.get("market_cap", 0),
            "volume_24h": token_data.get("volume_24h", 0),
            "change_5m": ch_5m,
            "change_1h": ch_1h,
            "change_24h": token_data.get("change_24h", 0),
            "buys_h1": token_data.get("buys_h1", 0),
            "sells_h1": token_data.get("sells_h1", 0),
        },
    }
    base_info_json = json.dumps(base_info)

    logger.info(
        f"[{symbol}] [{src_tag}] ${price_usd:.8f} | "
        f"Spike: {spike:.1f}x | 1h: {ch_1h:+.1f}% | 5m: {ch_5m:+.1f}% | "
        f"Liq: ${liq:,.0f}"
    )

    # ── GATE 2: Safety Check (RugCheck API) ──────────────────────────────
    safety_data = await safety.get_safety_details(address)
    if not safety_data or not safety_data.get("is_safe"):
        reason = safety_data.get("mint_authority", "Unknown") if safety_data else "Kein Safety-Daten / Scam"
        logger.warning(f"[{symbol}] Gate 2 FAIL: Safety — {reason}")
        await executor.execute_trade(symbol, address, 0, "REJECT",
            price=price_usd,
            rejection_reason=f"[G2 Safety] {reason}",
            ai_reasoning=base_info_json,
            funnel_stage="SAFETY_CHECK",
            gates_passed=",".join(gates))
        return False

    gates.append("G2:Safety")

    # ── GATE 3: Chain Data + Risk Assessment ─────────────────────────────
    chain_data = await chain.get_chain_data(address)
    top_10_pct = chain_data.get("top_10_holder_percent", 100)
    liq_locked = chain_data.get("liquidity_locked", False)

    hype_score = calculate_hype_score(token_data)
    risk_flags = get_risk_flags(token_data, top_10_pct, is_migration=is_migration)

    # Enrich base_info with chain data for G3+ rejects
    base_info["chain_data"] = {
        "top_10_pct": top_10_pct,
        "liq_locked": liq_locked,
        "holder_count": chain_data.get("holder_count", 0),
    }
    base_info["hype_score"] = hype_score
    base_info["risk_flags"] = risk_flags
    base_info_json = json.dumps(base_info)

    gates.append("G3:Risk")

    # ── GATE 4: Pre-Buy Filter ───────────────────────────────────────────
    filter_type = "Migration" if is_migration else "Standard"
    if is_migration:
        ok, reason = pre_buy_filter_migration(token_data, risk_flags)
    else:
        ok, reason = pre_buy_filter(token_data, risk_flags)

    if not ok:
        logger.warning(f"[{symbol}] Gate 4 FAIL ({filter_type}): {reason}")
        detail = (
            f"[G4 PreFilter/{filter_type}] {reason} | "
            f"Hype:{hype_score} Flags:{','.join(risk_flags)} "
            f"Liq:${liq:,.0f} Spike:{spike:.1f}x 1h:{ch_1h:+.1f}% "
            f"Top10:{top_10_pct:.0f}%"
        )
        await executor.execute_trade(symbol, address, 0, "REJECT",
            price=price_usd, rejection_reason=detail,
            ai_reasoning=base_info_json,
            funnel_stage="PRE_FILTER",
            gates_passed=",".join(gates))
        return False

    gates.append("G4:PreFilter")
    logger.info(f"[{symbol}] Gate 4 OK ({filter_type}) | Hype: {hype_score} | Flags: {risk_flags}")

    # ── GATE 5: Fusion Scoring ───────────────────────────────────────────
    market_data   = {"btc_1h_change": btc_change, "volume_spike": token_data.get("volume_spike", 0)}

    # Build full claude_result from base_info
    claude_result = dict(base_info)  # copy all base fields
    claude_result.update({
        "hype_score":  hype_score,
        "risk_flags":  risk_flags,
        "sentiment":   "Bullish" if hype_score >= 50 else "Neutral",
        "key_signals": [
            f"Source: {src_tag}",
            f"Vol-Spike {spike:.1f}x",
            f"1h {ch_1h:+.1f}%",
            f"5m {ch_5m:+.1f}%",
            f"Liq ${liq:,.0f}",
            f"MCap ${token_data.get('market_cap',0):,.0f}",
            f"Buys/Sells 1h: {token_data.get('buys_h1',0)}/{token_data.get('sells_h1',0)}",
            f"Top10: {top_10_pct:.0f}%",
            f"LiqLock: {liq_locked}",
            f"Age: {age_hours:.1f}h",
        ],
    })
    # Add extra fields for market_data
    claude_result["market_data"]["vol_mcap_ratio"] = token_data.get("vol_mcap_ratio", 0)
    claude_result["market_data"]["buys_h24"] = token_data.get("buys_h24", 0)
    claude_result["market_data"]["sells_h24"] = token_data.get("sells_h24", 0)

    if is_migration:
        claude_result["hype_score"] = min(100, hype_score + 15)
        claude_result["key_signals"].insert(0, "PUMP.FUN MIGRATION (bonus +15)")

    fusion_result = fusion.calculate_score(
        claude_result, chain_data, token_data, market_data
    )
    score      = fusion_result["score"]
    decision   = fusion_result["decision"]
    confidence = fusion_result.get("confidence", "LOW")
    breakdown  = fusion_result.get("breakdown", {})
    override   = breakdown.get("override_reason", "")

    logger.info(f"[{symbol}] Score: {score:.1f} | Decision: {decision} | Confidence: {confidence}")

    if decision != "BUY":
        reject_detail = (
            f"[G5 Scoring] Score {score:.1f} → {decision} | "
            f"Confidence:{confidence}"
        )
        if override:
            reject_detail += f" | Override: {override}"
        reject_detail += (
            f" | Breakdown: Hype={breakdown.get('hype_momentum',0):.1f} "
            f"BuySell={breakdown.get('buy_sell_pressure',0):.1f} "
            f"VolSpike={breakdown.get('volume_spike',0):.1f} "
            f"Wallet={breakdown.get('wallet_concentration',0):.1f} "
            f"LiqLock={breakdown.get('liquidity_lock',0):.1f} "
            f"VMR={breakdown.get('vol_mcap_ratio',0):.1f} "
            f"Risk={breakdown.get('risk_score',0):.1f} "
            f"BTC={breakdown.get('btc_market',0):.1f}"
        )
        await executor.execute_trade(symbol, address, score, "REJECT",
            price=price_usd,
            rejection_reason=reject_detail,
            ai_reasoning=json.dumps(claude_result),
            funnel_stage="SCORING",
            gates_passed=",".join(gates))
        return False

    # ── GATE 6: Trade Execution ──────────────────────────────────────────
    gates.append("G5:Scoring")

    if len(monitor.positions) >= 20:
        logger.warning(f"Max Positionen erreicht — {symbol} übersprungen")
        await executor.execute_trade(symbol, address, score, "REJECT",
            price=price_usd,
            rejection_reason=f"[G6 Exec] Max 20 Positionen erreicht",
            funnel_stage="EXEC_LIMIT",
            gates_passed=",".join(gates))
        return False

    gates.append("G6:Exec")
    accept_detail = (
        f"[ACCEPT] Score {score:.1f} | {confidence} | {src_tag} | "
        f"Hype:{hype_score} Liq:${liq:,.0f} Spike:{spike:.1f}x "
        f"1h:{ch_1h:+.1f}% 5m:{ch_5m:+.1f}% "
        f"Top10:{top_10_pct:.0f}% LiqLock:{liq_locked} "
        f"Flags:{','.join(risk_flags)}"
    )

    liq_usd = float(liq)
    res = await executor.execute_trade(
        symbol, address, score, "BUY",
        price=price_usd,
        rejection_reason=accept_detail,
        ai_reasoning=json.dumps(claude_result),
        funnel_stage="BUY_EXEC",
        confidence=confidence,
        liquidity_usd=liq_usd,
        gates_passed=",".join(gates),
    )
    if res and res.get("status") == "success":
        BOUGHT_THIS_SESSION.add(address)
        await monitor.add_position(address, price_usd, symbol=symbol)
        if not executor.dry_run:
            send_whatsapp_update(
                f"KAUF: {symbol} @ ${price_usd:.8f} | Score: {score:.0f} | {confidence} | {src_tag}"
            )
        logger.success(f"[{symbol}] GEKAUFT @ ${price_usd:.8f} | {confidence} | {src_tag}")
        return True

    return False


async def main_loop():
    logger.info("=" * 65)
    logger.info("Memecoin Trading Bot v2 — Multi-Source Discovery")
    logger.info(f"DRY_RUN: {os.getenv('DRY_RUN')} | Position: ${os.getenv('TRADE_MAX_POSITION_USD')}")
    logger.info("Sources: Pump.fun WebSocket (migrations + new) + DexScreener (trending + boosted)")
    logger.info("Filters: Safety > Pre-Filter > Fusion Score > BUY/SKIP")
    logger.info("=" * 65)

    dex      = DexScreenerAdapter()
    pumpfun  = PumpFunAdapter()
    safety   = SafetyAdapter()
    chain    = SolanaAdapter()
    fusion   = SignalFusion()
    executor = TradeExecutor()
    monitor  = PositionMonitor()

    global BOUGHT_THIS_SESSION
    BOUGHT_THIS_SESSION = set(monitor.positions.keys())
    logger.info(f"Bestehende Positionen geladen: {len(BOUGHT_THIS_SESSION)}")

    # Start background tasks
    asyncio.create_task(monitor.monitor())
    asyncio.create_task(pumpfun.start())        # WebSocket listener
    asyncio.create_task(pumpfun.cleanup_loop())  # Memory cleanup
    await asyncio.sleep(3)  # Give WebSocket time to connect

    scan_count = 0

    while True:
        if os.path.exists("STOP_BOT"):
            logger.warning("STOP_BOT erkannt — Bot stoppt.")
            break

        try:
            scan_count += 1
            pf_status = pumpfun.status()

            logger.info(
                f"── Scan #{scan_count} ──────────────────────────────────────────\n"
                f"  Pump.fun: {'CONNECTED' if pf_status['connected'] else 'DISCONNECTED'} | "
                f"Migrations: {pf_status['queue_migrations']} queued / {pf_status['total_migrations']} total | "
                f"New: {pf_status['queue_new']} queued / {pf_status['total_new_tokens']} total"
            )

            btc_change = await get_btc_change()
            logger.info(f"BTC 1h Change: {btc_change:+.2f}%")

            bought_this_scan = 0

            # ── PRIORITY 0: Migration Watchlist Retry ─────────────────────────
            # Tokens die beim letzten Scan keine DexScreener-Daten hatten
            if MIGRATION_WATCHLIST:
                now = _time.time()
                expired = []
                retry_list = []

                for addr, wl in list(MIGRATION_WATCHLIST.items()):
                    age = now - wl["added_at"]
                    if age > WATCHLIST_MAX_AGE_SEC or wl["retries"] >= WATCHLIST_MAX_RETRIES:
                        expired.append(addr)
                    else:
                        retry_list.append((addr, wl))

                # Abgelaufene entfernen
                for addr in expired:
                    sym = MIGRATION_WATCHLIST[addr]["token"].get("symbol", "?")
                    logger.warning(
                        f"[{sym}] Watchlist EXPIRED: DexScreener nie indexiert "
                        f"({MIGRATION_WATCHLIST[addr]['retries']} Retries, "
                        f"{now - MIGRATION_WATCHLIST[addr]['added_at']:.0f}s)"
                    )
                    del MIGRATION_WATCHLIST[addr]

                if retry_list:
                    logger.info(
                        f"── WATCHLIST RETRY: {len(retry_list)} migrations wartend ──"
                    )
                    for addr, wl in retry_list:
                        wl["retries"] += 1
                        token = wl["token"]
                        sym = token.get("symbol", "?")
                        logger.info(
                            f"[{sym}] Watchlist Retry #{wl['retries']} "
                            f"(wartend seit {now - wl['added_at']:.0f}s)"
                        )
                        bought = await evaluate_token(
                            token, dex, safety, chain, fusion, executor, monitor,
                            btc_change, is_migration=True,
                        )
                        if bought:
                            bought_this_scan += 1

            # ── PRIORITY 1: Pump.fun Migrations (HIGHEST PRIORITY) ────────────
            # These tokens JUST graduated to Raydium — earliest possible signal
            migrations = pumpfun.get_migration_candidates(limit=10)
            if migrations:
                logger.info(f"── PUMP.FUN MIGRATIONS: {len(migrations)} candidates ──")
                for token in migrations:
                    bought = await evaluate_token(
                        token, dex, safety, chain, fusion, executor, monitor,
                        btc_change, is_migration=True,
                    )
                    if bought:
                        bought_this_scan += 1

            # ── PRIORITY 2: DexScreener (trending + boosted) ──────────────────
            dex_candidates = await dex.get_all_candidates()
            logger.info(f"── DEXSCREENER: {len(dex_candidates)} candidates ──")
            for token in dex_candidates[:15]:
                bought = await evaluate_token(
                    token, dex, safety, chain, fusion, executor, monitor,
                    btc_change, is_migration=False,
                )
                if bought:
                    bought_this_scan += 1

            # ── PRIORITY 3: Pump.fun New Tokens (lowest priority) ─────────────
            # Only check these if we haven't found anything better
            if bought_this_scan == 0:
                new_tokens = pumpfun.get_new_token_candidates(limit=5)
                if new_tokens:
                    logger.info(f"── PUMP.FUN NEW TOKENS: {len(new_tokens)} candidates ──")
                    for token in new_tokens:
                        bought = await evaluate_token(
                            token, dex, safety, chain, fusion, executor, monitor,
                            btc_change, is_migration=False,
                        )
                        if bought:
                            bought_this_scan += 1

            logger.info(
                f"── Scan #{scan_count} done | Bought: {bought_this_scan} | "
                f"Positions: {len(monitor.positions)}/20 | "
                f"Watchlist: {len(MIGRATION_WATCHLIST)} | Warte 30s ──"
            )
            # Faster scan interval since PumpPortal gives us real-time data
            await asyncio.sleep(30)

        except Exception as e:
            logger.error(f"Fehler im Loop: {e}")
            await asyncio.sleep(30)


if __name__ == "__main__":
    asyncio.run(main_loop())
