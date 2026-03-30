import aiohttp
from loguru import logger


class DexScreenerAdapter:
    def __init__(self):
        self.base_url    = "https://api.dexscreener.com/latest/dex/tokens/"
        self.boosted_url = "https://api.dexscreener.com/token-profiles/latest/v1"

    async def get_boosted_tokens(self) -> list:
        """Hole die neuesten geboosten Tokens von DexScreener."""
        try:
            async with aiohttp.ClientSession() as session:
                async with session.get(self.boosted_url, timeout=aiohttp.ClientTimeout(total=10)) as response:
                    if response.status == 200:
                        data = await response.json()
                        if isinstance(data, list):
                            return [
                                {
                                    "symbol":  item.get("token", {}).get("symbol"),
                                    "address": item.get("tokenAddress"),
                                }
                                for item in data
                            ]
        except Exception as e:
            logger.error(f"Fehler beim Laden der Boosted Tokens: {e}")
        return []

    async def get_token_data(self, token_address: str) -> dict:
        """Hole Pair-Daten inkl. Preis und berechne Metriken."""
        url = f"{self.base_url}{token_address}"
        try:
            async with aiohttp.ClientSession() as session:
                async with session.get(url, timeout=aiohttp.ClientTimeout(total=10)) as response:
                    if response.status == 200:
                        data = await response.json()
                        if "pairs" in data and data["pairs"]:
                            pair = data["pairs"][0]

                            # Preis
                            price_usd = 0.0
                            try:
                                price_usd = float(pair.get("priceUsd") or 0)
                            except (TypeError, ValueError):
                                price_usd = 0.0

                            # Volumen & Spike
                            vol_h1  = float(pair.get("volume", {}).get("h1",  0) or 0)
                            vol_h24 = float(pair.get("volume", {}).get("h24", 0) or 0)
                            avg_h   = vol_h24 / 24 if vol_h24 > 0 else 0
                            spike   = vol_h1 / avg_h if avg_h > 0 else 0

                            # Liquidität
                            liquidity_usd = float(pair.get("liquidity", {}).get("usd", 0) or 0)

                            # Preisänderungen
                            price_change = pair.get("priceChange", {})
                            change_5m  = float(price_change.get("m5",  0) or 0)
                            change_1h  = float(price_change.get("h1",  0) or 0)
                            change_24h = float(price_change.get("h24", 0) or 0)

                            return {
                                "symbol":        pair.get("baseToken", {}).get("symbol", "UNKNOWN"),
                                "address":       token_address,
                                "price_usd":     price_usd,
                                "volume_spike":  spike,
                                "volume_h1":     vol_h1,
                                "volume_h24":    vol_h24,
                                "liquidity_usd": liquidity_usd,
                                "change_5m":     change_5m,
                                "change_1h":     change_1h,
                                "change_24h":    change_24h,
                                "dex_url":       pair.get("url", ""),
                                "info":          pair.get("info", {}),
                            }
            return None
        except Exception as e:
            logger.error(f"Fehler beim Laden von Token {token_address}: {e}")
            return None
