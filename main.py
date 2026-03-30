import asyncio
import sys
import os
import sqlite3
import json
from datetime import datetime
from loguru import logger

# Add src to path
sys.path.append(os.path.join(os.path.dirname(__file__), 'src'))

def log_to_db(msg):
    level = msg.record["level"].name
    message = msg.record["message"]
    conn = sqlite3.connect('memecoin_bot.db')
    c = conn.cursor()
    c.execute("INSERT INTO bot_logs (level, message, timestamp) VALUES (?, ?, ?)",
              (level, message, datetime.now()))
    conn.commit()
    conn.close()

logger.add(log_to_db)

from src.adapters.dexscreener import DexScreenerAdapter
from src.adapters.telegram_mirror import TelegramAlphaMirror
from src.adapters.safety import SafetyAdapter
from src.analysis.claude_client import ClaudeAnalyzer
from src.analysis.fusion import SignalFusion
from src.execution.executor import TradeExecutor
from src.execution.monitor import PositionMonitor
from notify_whatsapp import send_whatsapp_update

async def main_loop():
    logger.info("Memecoin Trading Bot starting up (Strategy Chain Active)...")
    
    dex = DexScreenerAdapter()
    tg = TelegramAlphaMirror()
    safety = SafetyAdapter()
    analyzer = ClaudeAnalyzer()
    fusion = SignalFusion()
    executor = TradeExecutor()
    monitor = PositionMonitor()
    
    # asyncio.create_task(tg.start_listening())
    asyncio.create_task(monitor.monitor())
    await asyncio.sleep(2)
    
    while True:
        try:
            logger.info("Scanning new opportunities...")
            boosted = await dex.get_boosted_tokens()
            for token in boosted[:10]:
                address = token.get("address")
                if not address: continue
                symbol = token.get('symbol') or 'UNKNOWN'
                token_data = await dex.get_token_data(address)
                if not token_data: continue
                symbol = token_data.get('symbol', symbol)
                messages = [] 
                
                # Rug-Pull Safety Check
                safety_data = await safety.get_safety_details(address)
                if not safety_data["is_safe"]:
                    # No longer logging every failed safety check to WhatsApp to reduce noise
                    logger.warning(f"Safety check FAILED for {symbol}: {safety_data.get('mint_authority', 'Unknown')}")
                    await executor.execute_trade(symbol, address, 0, "HOLD", price=token_data.get("price_usd", 0), rejection_reason=f"Safety: {safety_data.get('mint_authority', 'Scam')}", ai_reasoning=json.dumps(safety_data), funnel_stage="SAFETY_CHECK")
                    continue
                
                chain_data = {"liquidity_locked": True, "top_10_holder_percent": 30}
                market_data = {"btc_1h_change": 0.5, "volume_spike": token_data.get("volume_spike", 1)}
                
                # Scoring
                claude_result = {
                    "hype_score": 60, 
                    "risk_flags": ["Safety_Check_Passed"], 
                    "sentiment": "Bullish", 
                    "key_signals": ["High_Vol_Spike"]
                }
                
                fusion_result = fusion.calculate_score(claude_result, chain_data, token_data, market_data, unique_channels_5m=0)
                
                if fusion_result['score'] >= 60:
                    res = await executor.execute_trade(symbol, address, fusion_result['score'], "BUY", price=token_data.get("price_usd", 0), ai_reasoning=json.dumps(claude_result), funnel_stage="BUY_EXEC")
                    if res and res.get("status") == "success":
                        await monitor.add_position(address, token_data.get("price_usd", 0))
                        # Only send WhatsApp for REAL trades
                        if not executor.dry_run:
                            send_whatsapp_update(f"✅ *LIVE POSITION OPENED: {symbol}*")
                else:
                    await executor.execute_trade(symbol, address, fusion_result['score'], "HOLD", price=token_data.get("price_usd", 0), rejection_reason="Score below 60", ai_reasoning=json.dumps(claude_result), funnel_stage="SCORING")

            await asyncio.sleep(60)
        except Exception as e:
            logger.error(f"Error: {e}")
            await asyncio.sleep(60)

if __name__ == "__main__":
    asyncio.run(main_loop())
