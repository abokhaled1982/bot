import os
from loguru import logger
from binance.client import AsyncClient

class BinanceAdapter:
    def __init__(self):
        # We assume the user adds BINANCE_API_KEY and BINANCE_SECRET to .env
        self.api_key = os.getenv("BINANCE_API_KEY")
        self.api_secret = os.getenv("BINANCE_SECRET")
        self.client = None

    async def connect(self):
        if self.api_key and self.api_secret:
            self.client = await AsyncClient.create(self.api_key, self.api_secret)
            logger.info("Connected to Binance API")
        else:
            logger.warning("Binance API keys not found in .env. Trade execution will fail.")

    async def get_btc_price(self):
        """Fetch BTC price for the Market Context strategy."""
        if not self.client: return {"btc_1h_change": 0.0}
        try:
            # Get 24h ticker for BTC/USDT
            ticker = await self.client.get_ticker(symbol='BTCUSDT')
            price_change = float(ticker['priceChangePercent'])
            return {"btc_1h_change": price_change}
        except Exception as e:
            logger.error(f"Error fetching BTC price from Binance: {e}")
            return {"btc_1h_change": 0.0}

    async def close(self):
        if self.client:
            await self.client.close_connection()
