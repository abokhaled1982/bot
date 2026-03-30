import asyncio
import sys
import os
import sqlite3
import json
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
from src.analysis.fusion import SignalFusion
from src.execution.executor import TradeExecutor
from src.execution.monitor import PositionMonitor
from notify_whatsapp import send_whatsapp_update


async def main_loop():
    logger.info("Memecoin Trading Bot gestartet...")

    dex      = DexScreenerAdapter()
    safety   = SafetyAdapter()
    fusion   = SignalFusion()
    executor = TradeExecutor()
    monitor  = PositionMonitor()

    asyncio.create_task(monitor.monitor())
    await asyncio.sleep(2)

    while True:
        if os.path.exists("STOP_BOT"):
            logger.warning("STOP_BOT Signal erkannt. Bot haelt an.")
            break

        try:
            logger.info("Scanne neue Opportunities...")
            boosted = await dex.get_boosted_tokens()

            for token in boosted[:10]:
                address = token.get("address")
                if not address:
                    continue

                # ── DUPLIKAT CHECK ────────────────────────────────────────────
                if address in monitor.positions:
                    logger.info(f"[SKIP] {token.get('symbol','?')} bereits in Portfolio.")
                    continue

                symbol     = token.get("symbol") or "UNKNOWN"
                token_data = await dex.get_token_data(address)
                if not token_data:
                    continue

                symbol    = token_data.get("symbol", symbol)
                price_usd = token_data.get("price_usd", 0)

                # ── Safety Check ──────────────────────────────────────────────
                safety_data = await safety.get_safety_details(address)
                if not safety_data or not safety_data.get("is_safe"):
                    reason = (
                        safety_data.get("mint_authority", "Unknown")
                        if safety_data and isinstance(safety_data, dict)
                        else "Scam"
                    )
                    logger.warning(f"Safety FAIL fuer {symbol}: {reason}")
                    await executor.execute_trade(
                        symbol, address, 0, "HOLD",
                        price=price_usd,
                        rejection_reason=f"Safety: {reason}",
                        ai_reasoning=json.dumps(safety_data) if safety_data else "{}",
                        funnel_stage="SAFETY_CHECK",
                    )
                    continue

                # ── Scoring ───────────────────────────────────────────────────
                chain_data  = {"liquidity_locked": True, "top_10_holder_percent": 30}
                market_data = {
                    "btc_1h_change": 0.5,
                    "volume_spike":  token_data.get("volume_spike", 1),
                }
                claude_result = {
                    "hype_score":  60,
                    "risk_flags":  ["Safety_Check_Passed"],
                    "sentiment":   "Bullish",
                    "key_signals": ["High_Vol_Spike"],
                }

                fusion_result = fusion.calculate_score(
                    claude_result, chain_data, token_data, market_data, unique_channels_5m=0
                )

                # ── Entscheidung ──────────────────────────────────────────────
                if fusion_result["score"] >= 60:
                    if len(monitor.positions) >= 20:
                        logger.warning(f"Max 20 Positionen. {symbol} uebersprungen.")
                        continue

                    res = await executor.execute_trade(
                        symbol, address, fusion_result["score"], "BUY",
                        price=price_usd,
                        ai_reasoning=json.dumps(claude_result),
                        funnel_stage="BUY_EXEC",
                    )
                    if res and res.get("status") == "success":
                        await monitor.add_position(address, price_usd, symbol=symbol)
                        if not executor.dry_run:
                            send_whatsapp_update(f"LIVE POSITION OPENED: {symbol}")
                        logger.info(f"Position geoeffnet: {symbol} @ ${price_usd}")
                else:
                    await executor.execute_trade(
                        symbol, address, fusion_result["score"], "HOLD",
                        price=price_usd,
                        rejection_reason="Score unter 60",
                        ai_reasoning=json.dumps(claude_result),
                        funnel_stage="SCORING",
                    )

            await asyncio.sleep(60)

        except Exception as e:
            logger.error(f"Fehler im Haupt-Loop: {e}")
            await asyncio.sleep(60)


if __name__ == "__main__":
    asyncio.run(main_loop())
