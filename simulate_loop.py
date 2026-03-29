import asyncio
from loguru import logger
from analysis.claude_client import ClaudeAnalyzer
from analysis.fusion import SignalFusion
from execution.executor import TradeExecutor
from notify_whatsapp import send_whatsapp_update

async def simulation_loop():
    logger.info("Starting SIMULATION MODE (No Telegram required)...")
    
    analyzer = ClaudeAnalyzer()
    fusion = SignalFusion()
    executor = TradeExecutor()
    
    while True:
        try:
            # Simulate a "Boosted" Token
            symbol = "PEPE"
            address = "6SWctQS7s5dGj3wX8yT8m38y7576q21o6383187"
            
            # Simulate Data
            dex_data = {"symbol": symbol, "volume_spike": 5.5, "liquidity_usd": 200000}
            messages = [{"source": "Telegram-Tier1:123", "weight": 3.0, "content": "PEPE CA: 6SWctQS... LFG!"}]
            chain_data = {"liquidity_locked": True, "top_10_holder_percent": 25}
            market_data = {"btc_1h_change": 0.5}
            
            # 1. Analyze
            claude_result = await analyzer.analyze_token(messages)
            
            # 2. Fusion
            fusion_result = fusion.calculate_score(claude_result, chain_data, dex_data, market_data)
            
            # 3. Notification
            msg = f"🔍 {symbol} - Status: {fusion_result['decision']} (Score: {fusion_result['score']}) | Model: Claude Sonnet 4.6"
            send_whatsapp_update(msg)
            
            logger.info(f"Simulated {symbol}: {msg}")
            
            await asyncio.sleep(60) # Wait 1 minute per cycle
        except Exception as e:
            logger.error(f"Simulation Error: {e}")
            await asyncio.sleep(60)

if __name__ == "__main__":
    asyncio.run(simulation_loop())
