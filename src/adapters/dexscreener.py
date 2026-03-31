import aiohttp
from loguru import logger


class DexScreenerAdapter:
    def __init__(self):
        self.base_url    = "https://api.dexscreener.com/latest/dex/tokens/"
        self.boosted_url = "https://api.dexscreener.com/token-profiles/latest/v1"
        self.search_url  = "https://api.dexscreener.com/latest/dex/search"

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
                                    "source":  "boosted",
                                }
                                for item in data
                            ]
        except Exception as e:
            logger.error(f"Fehler beim Laden der Boosted Tokens: {e}")
        return []

    async def get_trending_tokens(self) -> list:
        """
        Hole trending Solana tokens via DexScreener search.
        Looks for high-volume, recently active Solana pairs.
        """
        try:
            async with aiohttp.ClientSession() as session:
                # Search for trending Solana pairs sorted by volume
                async with session.get(
                    f"{self.search_url}?q=solana",
                    timeout=aiohttp.ClientTimeout(total=10)
                ) as response:
                    if response.status == 200:
                        data = await response.json()
                        pairs = data.get("pairs", [])
                        # Filter: Solana only, has volume, reasonable liquidity
                        trending = []
                        seen = set()
                        for pair in pairs:
                            if pair.get("chainId") != "solana":
                                continue
                            addr = pair.get("baseToken", {}).get("address")
                            if not addr or addr in seen:
                                continue
                            seen.add(addr)
                            vol_h24 = float(pair.get("volume", {}).get("h24", 0) or 0)
                            liq = float(pair.get("liquidity", {}).get("usd", 0) or 0)
                            if vol_h24 > 10_000 and liq > 5_000:
                                trending.append({
                                    "symbol":  pair.get("baseToken", {}).get("symbol"),
                                    "address": addr,
                                    "source":  "trending",
                                })
                        return trending[:15]
        except Exception as e:
            logger.error(f"Fehler beim Laden der Trending Tokens: {e}")
        return []

    async def get_all_candidates(self) -> list:
        """
        Combine boosted + trending tokens, deduplicated by address.
        Trending tokens are prioritized (real momentum) over boosted (paid).
        """
        boosted  = await self.get_boosted_tokens()
        trending = await self.get_trending_tokens()

        seen   = set()
        result = []
        # Trending first — real momentum signal
        for token in trending + boosted:
            addr = token.get("address")
            if addr and addr not in seen:
                seen.add(addr)
                result.append(token)

        logger.info(f"[DEX] Candidates: {len(trending)} trending + {len(boosted)} boosted = {len(result)} unique")
        return result

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

                            # Market Cap & FDV
                            market_cap = float(pair.get("marketCap", 0) or 0)
                            fdv        = float(pair.get("fdv", 0) or 0)

                            # Pair creation time (token age)
                            pair_created_at = pair.get("pairCreatedAt", 0)  # ms timestamp

                            # Volume / Market Cap ratio (momentum indicator)
                            vol_mcap_ratio = (vol_h24 / market_cap) if market_cap > 0 else 0

                            # Transaction counts (buy/sell pressure)
                            txns = pair.get("txns", {})
                            buys_h1   = int(txns.get("h1", {}).get("buys", 0) or 0)
                            sells_h1  = int(txns.get("h1", {}).get("sells", 0) or 0)
                            buys_h24  = int(txns.get("h24", {}).get("buys", 0) or 0)
                            sells_h24 = int(txns.get("h24", {}).get("sells", 0) or 0)

                            return {
                                "symbol":          pair.get("baseToken", {}).get("symbol", "UNKNOWN"),
                                "address":         token_address,
                                "price_usd":       price_usd,
                                "volume_spike":    spike,
                                "volume_h1":       vol_h1,
                                "volume_h24":      vol_h24,
                                "liquidity_usd":   liquidity_usd,
                                "change_5m":       change_5m,
                                "change_1h":       change_1h,
                                "change_24h":      change_24h,
                                "market_cap":      market_cap,
                                "fdv":             fdv,
                                "pair_created_at": pair_created_at,
                                "vol_mcap_ratio":  round(vol_mcap_ratio, 4),
                                "buys_h1":         buys_h1,
                                "sells_h1":        sells_h1,
                                "buys_h24":        buys_h24,
                                "sells_h24":       sells_h24,
                                "dex_url":         pair.get("url", ""),
                                "info":            pair.get("info", {}),
                            }
            return None
        except Exception as e:
            logger.error(f"Fehler beim Laden von Token {token_address}: {e}")
            return None
