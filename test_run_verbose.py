import asyncio
from loguru import logger
from adapters.dexscreener import DexScreenerAdapter
from adapters.telegram_mirror import TelegramAlphaMirror
from analysis.claude_client import ClaudeAnalyzer
from analysis.fusion import SignalFusion

async def verbose_test():
    logger.info("--- STARTING DETAILED LOGGING TEST ---")
    
    dex = DexScreenerAdapter()
    tg = TelegramAlphaMirror()
    analyzer = ClaudeAnalyzer()
    fusion = SignalFusion()
    
    # 1. Simulate finding a boosted token
    token = {"symbol": "PEPE", "address": "6SWctQS7s5dGj3wX8yT8m38y7576q21o6383187"}
    token_data = {"symbol": "PEPE", "volume_spike": 5.2, "liquidity_usd": 150000, "info": {"socials": ["twitter.com/pepe"]}}
    
    # 2. Get Messages
    messages = await tg.get_recent_mentions("PEPE", token["address"], minutes=30)
    # Simulate a few if none found in real DB
    if not messages:
        messages = [
            {"source": "Telegram-Alpha-1", "sentiment_weight": 3.0, "content": "PEPE CA: 6SWctQS7s5dGj3wX8yT8m38y7576q21o6383187 - BULLISH!"}
        ]

    # 3. Apply Prefilter & Detailed Log
    logger.info(f"EVALUATING: {token['symbol']} | Spike: {token_data['volume_spike']}x")
    
    chain_data = {"liquidity_locked": True, "top_10_holder_percent": 25}
    market_data = {"btc_1h_change": 1.2}
    
    if not fusion.apply_prefilter(token_data, chain_data, market_data, messages):
        logger.info("FILTER: PRE-FILTER FAILED.")
        return

    # 4. Analyze with Gemini
    claude_result = await analyzer.analyze_token(messages)
    logger.info(f"LLM ANALYSIS RESULT: {claude_result}")
    
    # 5. Fusion Calculation (The math)
    fusion_result = fusion.calculate_score(claude_result, chain_data, token_data, market_data)
    
    logger.info("--- CALCULATION BREAKDOWN ---")
    for k, v in fusion_result['breakdown'].items():
        logger.info(f"Factor: {k} | Weight contribution: {v}")
    
    logger.info(f"FINAL DECISION: {fusion_result['decision']} (Score: {fusion_result['score']})")

if __name__ == "__main__":
    asyncio.run(verbose_test())
