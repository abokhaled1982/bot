import asyncio
from loguru import logger
from adapters.dexscreener import DexScreenerAdapter
from adapters.telegram_mirror import TelegramAlphaMirror
from analysis.claude_client import ClaudeAnalyzer
from analysis.fusion import SignalFusion
from execution.executor import TradeExecutor

# Mock-Klassen für den Testlauf, um Web-Quellen zu simulieren
class MockTelegramMirror(TelegramAlphaMirror):
    async def get_recent_mentions(self, symbol, address, minutes=30):
        # Simulation von hochwertigen Alpha-Mentions aus Tier-1 Kanälen
        return [
            {"source": "Telegram-Tier1:12345", "sentiment_weight": 3.0, "content": f"Huge alpha on {symbol}, CA: {address}, devs locking pool!"},
            {"source": "Telegram-Tier1:67890", "sentiment_weight": 3.0, "content": f"Just aped into {symbol}, massive volume incoming"},
            {"source": "Telegram-Tier2:11111", "sentiment_weight": 1.0, "content": f"Is {symbol} the next 100x?"}
        ]

async def run_test():
    logger.info("Starting INTEGRATION TEST RUN (Dry-Run)...")
    
    # Init components
    dex = DexScreenerAdapter()
    tg = MockTelegramMirror()
    claude = ClaudeAnalyzer()
    fusion = SignalFusion()
    exec = TradeExecutor()
    
    token = {"symbol": "BONK", "address": "DezXAZ8z7PnrnRJjz3wXBoRgixCa6xjnB7YaB1pPB263"}
    
    # 1. Simulate DexScreener Data
    dex_data = {"symbol": "BONK", "volume_spike": 4.5, "liquidity_usd": 50000}
    
    # 2. Simulate Chain/Market Data
    chain_data = {"liquidity_locked": True, "top_10_holder_percent": 30}
    market_data = {"btc_1h_change": 0.5}
    
    # 3. Simulate News (via our Mock Telegram)
    messages = await tg.get_recent_mentions("BONK", token["address"])
    
    # 4. Prefilter
    if not fusion.apply_prefilter(dex_data, chain_data, market_data, messages):
        logger.error("Test Failed: Prefilter blocked the token.")
        return

    # 5. Claude Analysis
    formatted_msgs = [{"source": m["source"], "weight": m["sentiment_weight"], "content": m["content"]} for m in messages]
    claude_result = await claude.analyze_token(formatted_msgs, use_haiku=True)
    logger.info(f"Claude Analysis: {claude_result}")
    
    # 6. Fusion
    fusion_result = fusion.calculate_score(claude_result, chain_data, dex_data, market_data, unique_channels_5m=2)
    logger.info(f"Fusion Decision: {fusion_result['decision']} (Score: {fusion_result['score']})")
    
    # 7. Final Output
    logger.info("Test Run complete. No real trades were executed.")

if __name__ == "__main__":
    asyncio.run(run_test())
