import asyncio
import sys
import os

# Add src to path
sys.path.append(os.path.join(os.path.dirname(__file__), 'src'))

from loguru import logger
# Fix imports after restructuring
from src.adapters.dexscreener import DexScreenerAdapter
from src.adapters.telegram_mirror import TelegramAlphaMirror
from src.analysis.claude_client import ClaudeAnalyzer
from src.analysis.fusion import SignalFusion
from src.execution.executor import TradeExecutor
from notify_whatsapp import send_whatsapp_update

async def main_loop():
    logger.info("Memecoin Trading Bot starting up (Strategy Chain Active)...")
    
    dex = DexScreenerAdapter()
    tg = TelegramAlphaMirror()
    analyzer = ClaudeAnalyzer()
    fusion = SignalFusion()
    executor = TradeExecutor()
    
    asyncio.create_task(tg.start_listening())
    await asyncio.sleep(2)
    
    while True:
        try:
            logger.info("Scanning new opportunities & Boosted List...")
            boosted = await dex.get_boosted_tokens()
            candidates = boosted[:10] 
            
            for token in candidates:
                address = token.get("address")
                if not address: continue
                symbol = token.get('symbol') or 'UNKNOWN'
                token_data = await dex.get_token_data(address)
                if not token_data: continue
                # Update symbol from reliable token_data if available
                symbol = token_data.get('symbol', symbol)
                
                messages = tg.get_recent_mentions(symbol, address, minutes=30)
                
                # Health Check / Update Logik
                msg = (
                    f"📊 *Status Update: {symbol}*\n"
                    f"Hype: {len(messages)} Telegram-Alpha-Mentions in 30m\n"
                    f"Vol-Spike: {token_data.get('volume_spike', 0):.2f}x\n"
                    f"Liq: {token_data.get('liquidity_usd', 0):,.0f} USD\n"
                    f"Model: Gemini Flash Lite\n"
                    f"Decision: [Scan läuft...]"
                )
                send_whatsapp_update(msg)
                
                # Weiter mit Filter/Analyse...
                if not token_data.get("info", {}).get("socials"): continue
                
                chain_data = {"liquidity_locked": True, "top_10_holder_percent": 30}
                market_data = {"btc_1h_change": 0.5}
                
                if not fusion.apply_prefilter(token_data, chain_data, market_data, messages): continue
                
                # Analyse-Daten vorbereiten (Fallback für fehlende Mentions)
                if not messages:
                    claude_result = {"hype_score": 20, "risk_flags": ["No_Telegram_Data"], "sentiment": "Neutral", "key_signals": ["No_recent_Telegram_mentions"]}
                else:
                    claude_result = await analyzer.analyze_token(messages)
                
                fusion_result = fusion.calculate_score(claude_result, chain_data, token_data, market_data, unique_channels_5m=0)
                
                # Finale Nachricht für WhatsApp
                final_msg = (
                    f"🚀 *{symbol} ANALYSIERT*\n"
                    f"Status: {fusion_result['decision']} (Score: {fusion_result['score']})\n"
                    f"Sentiment: {claude_result.get('sentiment', 'N/A')}\n"
                    f"Hype: {claude_result.get('hype_score', 0)}\n"
                    f"Risks: {', '.join(claude_result.get('risk_flags', []))}\n"
                    f"Signals: {', '.join(claude_result.get('key_signals', []))}"
                )
                send_whatsapp_update(final_msg)
                
                if fusion_result['decision'] == "BUY":
                    await executor.execute_trade(symbol, address, fusion_result['score'], "BUY")

            await asyncio.sleep(60)
        except Exception as e:
            logger.error(f"Error: {e}")
            await asyncio.sleep(60)

if __name__ == "__main__":
    asyncio.run(main_loop())
