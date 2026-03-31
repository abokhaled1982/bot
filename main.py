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
from src.adapters.safety import SafetyAdapter
from src.adapters.solana_chain import SolanaAdapter
from src.analysis.fusion import SignalFusion
from src.execution.executor import TradeExecutor
from src.execution.monitor import PositionMonitor
from notify_whatsapp import send_whatsapp_update

# ── Gekaufte Adressen in dieser Session (Duplikat-Schutz) ─────────────────────
BOUGHT_THIS_SESSION: set = set()


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


def get_risk_flags(token_data: dict, top_10_pct: float) -> list:
    flags   = []
    liq     = float(token_data.get("liquidity_usd", 0))
    spike   = float(token_data.get("volume_spike",  0))
    ch_1h   = float(token_data.get("change_1h",     0))
    ch_24h  = float(token_data.get("change_24h",    0))
    ch_5m   = float(token_data.get("change_5m",     0))
    mcap    = float(token_data.get("market_cap",     0))
    buys_h1 = int(token_data.get("buys_h1",  0))
    sells_h1= int(token_data.get("sells_h1", 0))
    age_h   = get_token_age_hours(token_data)

    if liq < 5_000:                    flags.append("Low_Liquidity")
    if top_10_pct > 60:                flags.append("Whale_Concentration")
    if spike > 20 and ch_1h > 100:     flags.append("Pump_Suspicion")
    if ch_24h < -50:                   flags.append("Rugpull_Hint")
    if ch_1h > 200:                    flags.append("Extreme_Pump")
    if ch_1h < -20:                    flags.append("Falling_Fast")
    if ch_5m < -10:                    flags.append("Dumping_Now")
    if ch_24h > 500:                   flags.append("Already_Mooned")
    if 0 <= age_h < 1:                 flags.append("Too_New")
    if sells_h1 > buys_h1 * 2 and sells_h1 > 20:
        flags.append("Heavy_Selling")
    if mcap > 0 and liq > 0 and (liq / mcap) < 0.03:
        flags.append("Thin_Liquidity_Ratio")

    if not flags:
        flags.append("No_Risk_Flags")
    return flags


# ── STRENGERE FILTER vor dem Kauf ─────────────────────────────────────────────
def pre_buy_filter(token_data: dict, risk_flags: list) -> tuple[bool, str]:
    """
    Gibt (True, '') zurück wenn OK zum Kaufen.
    Gibt (False, 'Grund') zurück wenn SKIP.
    """
    liq    = float(token_data.get("liquidity_usd", 0))
    ch_1h  = float(token_data.get("change_1h",    0))
    ch_5m  = float(token_data.get("change_5m",    0))
    ch_24h = float(token_data.get("change_24h",   0))
    spike  = float(token_data.get("volume_spike",  0))
    mcap   = float(token_data.get("market_cap",    0))
    age_h  = get_token_age_hours(token_data)

    # 1: Liquidität muss mindestens $5.000 sein
    if liq < 5_000:
        return False, f"Liquidität zu niedrig: ${liq:,.0f} (min $5.000)"

    # 2: 1h Preis darf nicht negativ sein — kaufe nicht beim Fallen
    if ch_1h < 0:
        return False, f"Token fällt: 1h {ch_1h:+.1f}%"

    # 3: Aktuell nicht am Dumpen
    if ch_5m < -5:
        return False, f"Token dumpt gerade: 5m {ch_5m:+.1f}%"

    # 4: Nicht 24h um mehr als 80% gefallen
    if ch_24h < -80:
        return False, f"24h Crash: {ch_24h:+.1f}%"

    # 5: Mindest-Volume Spike
    if spike < 2:
        return False, f"Volume Spike zu niedrig: {spike:.1f}x (min 2x)"

    # 6: Token age — avoid tokens younger than 1 hour (high rug risk)
    if 0 <= age_h < 1:
        return False, f"Token zu neu: {age_h:.1f}h alt (min 1h)"

    # 7: Token age — avoid dead tokens older than 72 hours with no momentum
    if age_h > 72 and ch_24h < 5 and spike < 3:
        return False, f"Token alt und kein Momentum: {age_h:.0f}h | 24h {ch_24h:+.1f}%"

    # 8: Market cap sanity — skip if too small or too big
    if mcap > 0 and mcap < 10_000:
        return False, f"Market Cap zu klein: ${mcap:,.0f} (min $10k)"
    if mcap > 50_000_000:
        return False, f"Market Cap zu hoch: ${mcap:,.0f} (kein Memecoin)"

    # 9: Anti-FOMO — don't buy tokens that already pumped 500%+ in 24h
    if ch_24h > 500:
        return False, f"Bereits gemooned: 24h {ch_24h:+.1f}% (Anti-FOMO)"

    # 10: Critical Risk Flags
    critical = [
        "Low_Liquidity", "Rugpull_Hint", "Falling_Fast",
        "Dumping_Now", "Too_New", "Heavy_Selling", "Already_Mooned",
    ]
    for flag in critical:
        if flag in risk_flags:
            return False, f"Critical Flag: {flag}"

    return True, ""


async def main_loop():
    logger.info("=" * 55)
    logger.info("Memecoin Trading Bot gestartet")
    logger.info(f"DRY_RUN: {os.getenv('DRY_RUN')} | Position: ${os.getenv('TRADE_MAX_POSITION_USD')}")
    logger.info("Filter: Liq>$5k | 1h>0% | 5m>-5% | Spike>2x | Age>1h | MCap $10k-$50M | Anti-FOMO")
    logger.info("=" * 55)

    dex      = DexScreenerAdapter()
    safety   = SafetyAdapter()
    chain    = SolanaAdapter()
    fusion   = SignalFusion()
    executor = TradeExecutor()
    monitor  = PositionMonitor()

    # FIX: Bereits vorhandene Positionen in Session-Set laden
    global BOUGHT_THIS_SESSION
    BOUGHT_THIS_SESSION = set(monitor.positions.keys())
    logger.info(f"Bestehende Positionen geladen: {len(BOUGHT_THIS_SESSION)}")

    asyncio.create_task(monitor.monitor())
    await asyncio.sleep(2)

    while True:
        if os.path.exists("STOP_BOT"):
            logger.warning("STOP_BOT erkannt — Bot stoppt.")
            break

        try:
            logger.info("── Scanne Markt ──────────────────────────────────────")

            btc_change = await get_btc_change()
            logger.info(f"BTC 1h Change: {btc_change:+.2f}%")

            candidates = await dex.get_all_candidates()
            logger.info(f"Gefundene Tokens: {len(candidates)}")

            for token in candidates[:15]:
                address = token.get("address")
                if not address:
                    continue

                # ── FIX 1: DUPLIKAT CHECK (session + positions) ────────────────
                if address in BOUGHT_THIS_SESSION:
                    logger.info(f"[SKIP] {token.get('symbol','?')} bereits gekauft (Duplikat-Schutz)")
                    continue
                if address in monitor.positions:
                    logger.info(f"[SKIP] {token.get('symbol','?')} bereits in Portfolio")
                    continue

                symbol     = token.get("symbol") or "UNKNOWN"
                token_data = await dex.get_token_data(address)
                if not token_data:
                    continue

                symbol    = token_data.get("symbol", symbol)
                price_usd = token_data.get("price_usd", 0)

                logger.info(
                    f"[{symbol}] ${price_usd:.8f} | "
                    f"Spike: {token_data.get('volume_spike',0):.1f}x | "
                    f"1h: {token_data.get('change_1h',0):+.1f}% | "
                    f"5m: {token_data.get('change_5m',0):+.1f}% | "
                    f"Liq: ${token_data.get('liquidity_usd',0):,.0f}"
                )

                # ── Safety Check ───────────────────────────────────────────────
                safety_data = await safety.get_safety_details(address)
                if not safety_data or not safety_data.get("is_safe"):
                    reason = safety_data.get("mint_authority","Unknown") if safety_data else "Scam"
                    logger.warning(f"[{symbol}] ❌ Safety FAIL: {reason}")
                    await executor.execute_trade(symbol, address, 0, "HOLD",
                        price=price_usd, rejection_reason=f"Safety: {reason}",
                        funnel_stage="SAFETY_CHECK")
                    continue

                # ── Chain Daten ────────────────────────────────────────────────
                chain_data = await chain.get_chain_data(address)
                top_10_pct = chain_data.get("top_10_holder_percent", 100)

                # ── Risk Flags & Hype Score ────────────────────────────────────
                hype_score = calculate_hype_score(token_data)
                risk_flags = get_risk_flags(token_data, top_10_pct)

                # ── FIX 2: STRENGER PRE-BUY FILTER ────────────────────────────
                ok, reason = pre_buy_filter(token_data, risk_flags)
                if not ok:
                    logger.warning(f"[{symbol}] ❌ Pre-Filter FAIL: {reason}")
                    await executor.execute_trade(symbol, address, 0, "HOLD",
                        price=price_usd, rejection_reason=reason,
                        funnel_stage="PRE_FILTER")
                    continue

                logger.info(f"[{symbol}] ✅ Pre-Filter OK | Hype: {hype_score} | Flags: {risk_flags}")

                # ── Scoring ────────────────────────────────────────────────────
                market_data   = {"btc_1h_change": btc_change, "volume_spike": token_data.get("volume_spike",0)}
                claude_result = {
                    "hype_score":  hype_score,
                    "risk_flags":  risk_flags,
                    "sentiment":   "Bullish" if hype_score >= 50 else "Neutral",
                    "key_signals": [
                        f"Vol-Spike {token_data.get('volume_spike',0):.1f}x",
                        f"1h {token_data.get('change_1h',0):+.1f}%",
                        f"5m {token_data.get('change_5m',0):+.1f}%",
                        f"Liq ${token_data.get('liquidity_usd',0):,.0f}",
                        f"MCap ${token_data.get('market_cap',0):,.0f}",
                        f"Buys/Sells 1h: {token_data.get('buys_h1',0)}/{token_data.get('sells_h1',0)}",
                    ],
                }

                fusion_result = fusion.calculate_score(
                    claude_result, chain_data, token_data, market_data
                )
                score      = fusion_result["score"]
                decision   = fusion_result["decision"]
                confidence = fusion_result.get("confidence", "LOW")

                logger.info(f"[{symbol}] Score: {score:.1f} | Entscheidung: {decision} | Confidence: {confidence}")

                # ── Trade ──────────────────────────────────────────────────────
                if decision == "BUY":
                    if len(monitor.positions) >= 20:
                        logger.warning(f"Max Positionen erreicht — {symbol} übersprungen")
                        continue

                    liq_usd = float(token_data.get("liquidity_usd", 0))
                    res = await executor.execute_trade(
                        symbol, address, score, "BUY",
                        price=price_usd,
                        ai_reasoning=json.dumps(claude_result),
                        funnel_stage="BUY_EXEC",
                        confidence=confidence,
                        liquidity_usd=liq_usd,
                    )
                    if res and res.get("status") == "success":
                        BOUGHT_THIS_SESSION.add(address)
                        await monitor.add_position(address, price_usd, symbol=symbol)
                        if not executor.dry_run:
                            send_whatsapp_update(
                                f"🚀 KAUF: {symbol} @ ${price_usd:.8f} | Score: {score:.0f} | {confidence}"
                            )
                        logger.success(f"[{symbol}] ✅ GEKAUFT @ ${price_usd:.8f} | {confidence}")
                else:
                    await executor.execute_trade(symbol, address, score, "HOLD",
                        price=price_usd,
                        rejection_reason=f"{decision} | Score {score:.1f}",
                        ai_reasoning=json.dumps(claude_result),
                        funnel_stage="SCORING")

            logger.info("── Warte 60s ──────────────────────────────────────────")
            await asyncio.sleep(60)

        except Exception as e:
            logger.error(f"Fehler im Loop: {e}")
            await asyncio.sleep(60)


if __name__ == "__main__":
    asyncio.run(main_loop())
