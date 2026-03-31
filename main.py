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


# ── BTC Preis direkt von CoinGecko (kein Binance Key nötig) ──────────────────
async def get_btc_change() -> float:
    """Hole echte BTC 1h Preisänderung von CoinGecko."""
    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(
                "https://api.coingecko.com/api/v3/simple/price"
                "?ids=bitcoin&vs_currencies=usd&include_24hr_change=true",
                timeout=aiohttp.ClientTimeout(total=8)
            ) as r:
                if r.status == 200:
                    data = await r.json()
                    # 24h change als Näherung
                    change = float(data["bitcoin"].get("usd_24h_change", 0))
                    # Auf 1h runterrechnen (grobe Näherung)
                    return round(change / 24, 4)
    except Exception as e:
        logger.warning(f"BTC Preis Fehler: {e} — nutze 0.0")
    return 0.0


# ── Hype Score aus Token-Metriken berechnen ───────────────────────────────────
def calculate_hype_score(token_data: dict) -> int:
    """
    Echter Hype Score basierend auf DexScreener Metriken.
    Skala: 0-100
    """
    score = 0

    # Volume Spike (max 40 Punkte)
    spike = float(token_data.get("volume_spike", 0))
    if   spike >= 10: score += 40
    elif spike >= 5:  score += 30
    elif spike >= 3:  score += 20
    elif spike >= 1:  score += 10

    # 1h Preisänderung (max 30 Punkte)
    change_1h = float(token_data.get("change_1h", 0))
    if   change_1h >= 50:  score += 30
    elif change_1h >= 20:  score += 20
    elif change_1h >= 10:  score += 15
    elif change_1h >= 5:   score += 10
    elif change_1h >= 0:   score += 5
    elif change_1h < -20:  score -= 10

    # 5min Preisänderung (max 20 Punkte) — frischer Pump
    change_5m = float(token_data.get("change_5m", 0))
    if   change_5m >= 20: score += 20
    elif change_5m >= 10: score += 15
    elif change_5m >= 5:  score += 10
    elif change_5m >= 2:  score += 5

    # Liquidität (max 10 Punkte) — mehr Liquidität = sicherer
    liq = float(token_data.get("liquidity_usd", 0))
    if   liq >= 100_000: score += 10
    elif liq >= 50_000:  score += 8
    elif liq >= 20_000:  score += 5
    elif liq >= 5_000:   score += 3
    elif liq < 1_000:    score -= 10  # zu wenig Liquidität — gefährlich

    return max(0, min(100, score))


# ── Risk Flags aus Token-Metriken ─────────────────────────────────────────────
def get_risk_flags(token_data: dict, top_10_pct: float) -> list:
    flags = []

    liq     = float(token_data.get("liquidity_usd", 0))
    spike   = float(token_data.get("volume_spike",  0))
    ch_1h   = float(token_data.get("change_1h",     0))
    ch_24h  = float(token_data.get("change_24h",    0))

    if liq < 5_000:              flags.append("Low_Liquidity")
    if top_10_pct > 60:          flags.append("Whale_Concentration")
    if spike > 20 and ch_1h > 100: flags.append("Pump_Suspicion")
    if ch_24h < -50:             flags.append("Rugpull_Hint")
    if ch_1h > 200:              flags.append("Extreme_Pump")

    if not flags:
        flags.append("No_Risk_Flags")

    return flags


# ── Haupt Loop ────────────────────────────────────────────────────────────────
async def main_loop():
    logger.info("=" * 50)
    logger.info("Memecoin Trading Bot gestartet")
    logger.info(f"DRY_RUN: {os.getenv('DRY_RUN')} | Position: ${os.getenv('TRADE_MAX_POSITION_USD')}")
    logger.info("=" * 50)

    dex     = DexScreenerAdapter()
    safety  = SafetyAdapter()
    chain   = SolanaAdapter()
    fusion  = SignalFusion()
    executor= TradeExecutor()
    monitor = PositionMonitor()

    asyncio.create_task(monitor.monitor())
    await asyncio.sleep(2)

    while True:
        if os.path.exists("STOP_BOT"):
            logger.warning("STOP_BOT erkannt — Bot stoppt.")
            break

        try:
            logger.info("── Scanne Markt ──────────────────────────────")

            # 1. Echte BTC Marktlage
            btc_change = await get_btc_change()
            logger.info(f"BTC 1h Change: {btc_change:+.2f}%")

            # 2. Boosted Tokens holen
            boosted = await dex.get_boosted_tokens()
            logger.info(f"Gefundene Tokens: {len(boosted)}")

            for token in boosted[:10]:
                address = token.get("address")
                if not address:
                    continue

                # Duplikat Check
                if address in monitor.positions:
                    continue

                symbol     = token.get("symbol") or "UNKNOWN"
                token_data = await dex.get_token_data(address)
                if not token_data:
                    continue

                symbol    = token_data.get("symbol", symbol)
                price_usd = token_data.get("price_usd", 0)

                logger.info(
                    f"[{symbol}] Preis: ${price_usd:.8f} | "
                    f"Vol-Spike: {token_data.get('volume_spike',0):.1f}x | "
                    f"1h: {token_data.get('change_1h',0):+.1f}% | "
                    f"Liq: ${token_data.get('liquidity_usd',0):,.0f}"
                )

                # 3. Safety Check (Mint Authority)
                safety_data = await safety.get_safety_details(address)
                if not safety_data or not safety_data.get("is_safe"):
                    reason = safety_data.get("mint_authority", "Unknown") if safety_data else "Scam"
                    logger.warning(f"[{symbol}] ❌ Safety FAIL: {reason}")
                    await executor.execute_trade(
                        symbol, address, 0, "HOLD",
                        price=price_usd,
                        rejection_reason=f"Safety: {reason}",
                        ai_reasoning=json.dumps(safety_data) if safety_data else "{}",
                        funnel_stage="SAFETY_CHECK",
                    )
                    continue

                logger.info(f"[{symbol}] ✅ Safety OK")

                # 4. Echte Chain Daten (Top-10 Holder %)
                chain_data = await chain.get_chain_data(address)
                top_10_pct = chain_data.get("top_10_holder_percent", 100)
                logger.info(f"[{symbol}] Top-10 Holder: {top_10_pct:.1f}%")

                # 5. Echten Hype Score berechnen
                hype_score = calculate_hype_score(token_data)
                risk_flags = get_risk_flags(token_data, top_10_pct)
                logger.info(f"[{symbol}] Hype Score: {hype_score} | Flags: {risk_flags}")

                # 6. Echte market_data zusammenbauen
                market_data = {
                    "btc_1h_change": btc_change,
                    "volume_spike":  token_data.get("volume_spike", 0),
                }

                claude_result = {
                    "hype_score":  hype_score,
                    "risk_flags":  risk_flags,
                    "sentiment":   "Bullish" if hype_score >= 50 else "Neutral",
                    "key_signals": [
                        f"Vol-Spike {token_data.get('volume_spike',0):.1f}x",
                        f"1h {token_data.get('change_1h',0):+.1f}%",
                        f"Liq ${token_data.get('liquidity_usd',0):,.0f}",
                    ],
                }

                # 7. Score berechnen
                fusion_result = fusion.calculate_score(
                    claude_result, chain_data, token_data, market_data, unique_channels_5m=0
                )
                score    = fusion_result["score"]
                decision = fusion_result["decision"]

                logger.info(
                    f"[{symbol}] Score: {score:.1f} | "
                    f"Entscheidung: {decision} | "
                    f"Breakdown: {fusion_result.get('breakdown', {}).get('override_reason', 'OK')}"
                )

                # 8. Trade ausführen
                if decision == "BUY":
                    if len(monitor.positions) >= 20:
                        logger.warning(f"Max 20 Positionen — {symbol} übersprungen")
                        continue

                    res = await executor.execute_trade(
                        symbol, address, score, "BUY",
                        price=price_usd,
                        ai_reasoning=json.dumps(claude_result),
                        funnel_stage="BUY_EXEC",
                    )
                    if res and res.get("status") == "success":
                        await monitor.add_position(address, price_usd, symbol=symbol)
                        if not executor.dry_run:
                            send_whatsapp_update(
                                f"🚀 KAUF: {symbol} @ ${price_usd:.8f} | Score: {score:.0f}"
                            )
                        logger.info(f"[{symbol}] ✅ Position eröffnet @ ${price_usd}")
                else:
                    await executor.execute_trade(
                        symbol, address, score, "HOLD",
                        price=price_usd,
                        rejection_reason=f"{decision} | Score {score:.1f}",
                        ai_reasoning=json.dumps(claude_result),
                        funnel_stage="SCORING",
                    )

            logger.info("── Warte 60s ─────────────────────────────────")
            await asyncio.sleep(60)

        except Exception as e:
            logger.error(f"Fehler im Loop: {e}")
            await asyncio.sleep(60)


if __name__ == "__main__":
    asyncio.run(main_loop())
