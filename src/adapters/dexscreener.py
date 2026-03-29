import aiohttp
from loguru import logger

class DexScreenerAdapter:
    def __init__(self):
        self.base_url = "https://api.dexscreener.com/latest/dex/tokens/"
        self.boosted_url = "https://api.dexscreener.com/token-profiles/latest/v1"

    async def get_boosted_tokens(self) -> list:
        """Fetch latest boosted tokens. Returns a list of token dicts."""
        try:
            async with aiohttp.ClientSession() as session:
                async with session.get(self.boosted_url) as response:
                    if response.status == 200:
                        data = await response.json()
                        # The API returns list of dictionaries
                        if isinstance(data, list):
                            return [{"symbol": item.get('token', {}).get('symbol'), "address": item.get('tokenAddress')} for item in data]
                        return []

        except Exception as e:
            logger.error(f"Error fetching boosted tokens: {e}")
            return []

    async def get_token_data(self, token_address: str) -> dict:
        """Fetch pair data and calculate metrics."""
        url = f"{self.base_url}{token_address}"
        try:
            async with aiohttp.ClientSession() as session:
                async with session.get(url) as response:
                    if response.status == 200:
                        data = await response.json()
                        if 'pairs' in data and data['pairs']:
                            pair = data['pairs'][0]
                            # Extracting metrics
                            vol_h1 = float(pair.get('volume', {}).get('h1', 0))
                            vol_h24 = float(pair.get('volume', {}).get('h24', 0))
                            avg_h = vol_h24 / 24 if vol_h24 > 0 else 0
                            spike = vol_h1 / avg_h if avg_h > 0 else 0
                            
                            return {
                                "symbol": pair.get('baseToken', {}).get('symbol'),
                                "address": token_address,
                                "volume_spike": spike,
                                "liquidity_usd": float(pair.get('liquidity', {}).get('usd', 0)),
                                "info": pair.get('info', {}) # Contains social links
                            }
                    return None
        except Exception as e:
            logger.error(f"Error fetching token {token_address}: {e}")
            return None
