import asyncio
from loguru import logger
from adapters.dexscreener import DexScreenerAdapter
from adapters.telegram_mirror import TelegramAlphaMirror
from analysis.claude_client import ClaudeAnalyzer
from analysis.fusion import SignalFusion
from execution.executor import TradeExecutor
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
                address = token["address"]
                symbol = token.get('symbol', 'UNKNOWN')
                token_data = await dex.get_token_data(address)
                if not token_data: continue
                
                messages = await tg.get_recent_mentions(symbol, address, minutes=30)
                
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
                
                claude_result = await analyzer.analyze_token(messages)
                fusion_result = fusion.calculate_score(claude_result, chain_data, token_data, market_data)
                
                # Finale Nachricht
                final_msg = f"🚀 *{symbol} ANALYSIERT*\nStatus: {fusion_result['decision']} (Score: {fusion_result['score']})\nSentiment: {claude_result['sentiment']}"
                send_whatsapp_update(final_msg)
                
                if fusion_result['decision'] == "BUY":
                    await executor.execute_trade(symbol, address, fusion_result['score'], "BUY")

            await asyncio.sleep(60)
        except Exception as e:
            logger.error(f"Error: {e}")
            await asyncio.sleep(60)

if __name__ == "__main__":
    asyncio.run(main_loop())
