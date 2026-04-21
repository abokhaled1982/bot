import time
import aiohttp
from loguru import logger

# ── In-memory TTL cache (avoids re-fetching the same token within a scan) ────
_TOKEN_CACHE: dict[str, tuple[float, dict]] = {}
_CACHE_TTL = 25  # seconds — slightly less than the 30s scan loop


def _cache_get(address: str) -> dict | None:
    entry = _TOKEN_CACHE.get(address)
    if entry and (time.time() - entry[0]) < _CACHE_TTL:
        return entry[1]
    return None


def _cache_set(address: str, data: dict) -> None:
    _TOKEN_CACHE[address] = (time.time(), data)
    if len(_TOKEN_CACHE) > 500:
        now = time.time()
        stale = [k for k, (ts, _) in _TOKEN_CACHE.items() if now - ts > _CACHE_TTL]
        for k in stale:
            del _TOKEN_CACHE[k]


class DexScreenerAdapter:
    def __init__(self):
        self.base_url  = "https://api.dexscreener.com/latest/dex/tokens/"
        self.search_url = "https://api.dexscreener.com/latest/dex/search"
        self._timeout  = aiohttp.ClientTimeout(total=10)
        self._session: aiohttp.ClientSession | None = None

    async def _get_session(self) -> aiohttp.ClientSession:
        """Reuse a single aiohttp session for connection pooling."""
        if self._session is None or self._session.closed:
            self._session = aiohttp.ClientSession(timeout=self._timeout)
        return self._session

    async def close(self) -> None:
        if self._session and not self._session.closed:
            await self._session.close()

    # ── Boosted tokens ────────────────────────────────────────────────────────

    async def get_boosted_tokens(self) -> list:
        """Get Solana tokens with most active boosts (top + latest)."""
        candidates = []
        try:
            session = await self._get_session()
            async with session.get("https://api.dexscreener.com/token-boosts/top/v1") as r:
                if r.status == 200:
                    data = await r.json()
                    if isinstance(data, list):
                        for item in data:
                            if item.get("chainId") != "solana":
                                continue
                            addr = item.get("tokenAddress")
                            if addr:
                                candidates.append({"symbol": addr[:8], "address": addr, "source": "boosted_top"})

            async with session.get("https://api.dexscreener.com/token-boosts/latest/v1") as r:
                if r.status == 200:
                    data = await r.json()
                    seen = {c["address"] for c in candidates}
                    if isinstance(data, list):
                        for item in data:
                            if item.get("chainId") != "solana":
                                continue
                            addr = item.get("tokenAddress")
                            if addr and addr not in seen:
                                seen.add(addr)
                                candidates.append({"symbol": addr[:8], "address": addr, "source": "boosted_latest"})
        except Exception as e:
            logger.error(f"Error loading boosted tokens: {e}")

        logger.info(f"[DEX] Boosted: {len(candidates)} Solana tokens")
        return candidates

    # ── Trending metas ────────────────────────────────────────────────────────

    async def get_trending_tokens(self) -> list:
        """Discover trending Solana memecoins via DexScreener trending metas."""
        candidates = []
        seen = set()
        metas = []
        try:
            session = await self._get_session()
            async with session.get("https://api.dexscreener.com/metas/trending/v1") as r:
                if r.status != 200:
                    return []
                metas = await r.json()

            for meta in metas[:5]:
                slug = meta.get("slug", "")
                if not slug:
                    continue
                try:
                    async with session.get(f"https://api.dexscreener.com/metas/meta/v1/{slug}") as r:
                        if r.status != 200:
                            continue
                        meta_data = await r.json()
                        for pair in meta_data.get("pairs", []):
                            if pair.get("chainId") != "solana":
                                continue
                            addr = pair.get("baseToken", {}).get("address")
                            if not addr or addr in seen:
                                continue
                            vol_h24 = float(pair.get("volume", {}).get("h24", 0) or 0)
                            liq = float(pair.get("liquidity", {}).get("usd", 0) or 0)
                            if vol_h24 > 5_000 and liq > 3_000:
                                seen.add(addr)
                                candidates.append({
                                    "symbol":  pair.get("baseToken", {}).get("symbol", addr[:8]),
                                    "address": addr,
                                    "source":  f"trending_meta_{slug}",
                                })
                except Exception as e:
                    logger.warning(f"[DEX] Meta {slug} error: {e}")
        except Exception as e:
            logger.error(f"Error loading trending metas: {e}")

        logger.info(f"[DEX] Trending metas: {len(candidates)} Solana tokens from {min(len(metas), 5)} categories")
        return candidates[:30]

    # ── Token profiles ────────────────────────────────────────────────────────

    async def get_new_profiles(self) -> list:
        """Get recently created/updated token profiles on Solana."""
        candidates = []
        try:
            session = await self._get_session()
            for url in [
                "https://api.dexscreener.com/token-profiles/latest/v1",
                "https://api.dexscreener.com/token-profiles/recent-updates/v1",
            ]:
                async with session.get(url) as r:
                    if r.status == 200:
                        data = await r.json()
                        if isinstance(data, list):
                            for item in data:
                                if item.get("chainId") != "solana":
                                    continue
                                addr = item.get("tokenAddress")
                                if addr:
                                    candidates.append({"symbol": addr[:8], "address": addr, "source": "new_profile"})
        except Exception as e:
            logger.error(f"Error loading token profiles: {e}")

        logger.info(f"[DEX] New profiles: {len(candidates)} Solana tokens")
        return candidates

    # ── Community takeovers ───────────────────────────────────────────────────

    async def get_community_takeovers(self) -> list:
        """Get latest community takeover tokens on Solana (CTO = strong community signal)."""
        candidates = []
        try:
            session = await self._get_session()
            async with session.get("https://api.dexscreener.com/community-takeovers/latest/v1") as r:
                if r.status == 200:
                    data = await r.json()
                    if isinstance(data, list):
                        for item in data:
                            if item.get("chainId") != "solana":
                                continue
                            addr = item.get("tokenAddress")
                            if addr:
                                candidates.append({"symbol": addr[:8], "address": addr, "source": "dex_cto"})
        except Exception as e:
            logger.error(f"[DEX] CTO error: {e}")

        logger.info(f"[DEX] Community takeovers: {len(candidates)} Solana tokens")
        return candidates

    # ── Ad tokens ─────────────────────────────────────────────────────────────

    async def get_ad_tokens(self) -> list:
        """Get tokens currently paying for DexScreener ads."""
        candidates = []
        try:
            session = await self._get_session()
            async with session.get("https://api.dexscreener.com/ads/latest/v1") as r:
                if r.status == 200:
                    data = await r.json()
                    if isinstance(data, list):
                        for item in data:
                            if item.get("chainId") != "solana":
                                continue
                            addr = item.get("tokenAddress")
                            if addr:
                                candidates.append({"symbol": addr[:8], "address": addr, "source": "dex_ad"})
        except Exception as e:
            logger.error(f"[DEX] Ads error: {e}")

        logger.info(f"[DEX] Ad tokens: {len(candidates)} Solana tokens")
        return candidates

    # ── Combined candidates ───────────────────────────────────────────────────

    async def get_all_candidates(self) -> list:
        """
        Combine all DexScreener sources, deduplicated.
        Priority: boosted > CTO > ads > trending metas > new profiles.
        """
        boosted  = await self.get_boosted_tokens()
        cto      = await self.get_community_takeovers()
        ads      = await self.get_ad_tokens()
        trending = await self.get_trending_tokens()
        profiles = await self.get_new_profiles()

        seen   = set()
        result = []
        for token in boosted + cto + ads + trending + profiles:
            addr = token.get("address")
            if addr and addr not in seen:
                seen.add(addr)
                result.append(token)

        logger.info(
            f"[DEX] Candidates: {len(boosted)} boosted + {len(cto)} CTO + "
            f"{len(ads)} ads + {len(trending)} trending + {len(profiles)} profiles = "
            f"{len(result)} unique"
        )
        return result

    # ── Token data fetch ──────────────────────────────────────────────────────

    async def get_token_data(self, token_address: str) -> dict:
        """Fetch pair data with price and metrics. Cached for 25s."""
        cached = _cache_get(token_address)
        if cached is not None:
            return cached

        url = f"{self.base_url}{token_address}"
        try:
            session = await self._get_session()
            async with session.get(url) as response:
                if response.status == 200:
                    data = await response.json()
                    if "pairs" in data and data["pairs"]:
                        pair = data["pairs"][0]

                        price_usd = 0.0
                        try:
                            price_usd = float(pair.get("priceUsd") or 0)
                        except (TypeError, ValueError):
                            price_usd = 0.0

                        vol_h1  = float(pair.get("volume", {}).get("h1",  0) or 0)
                        vol_h24 = float(pair.get("volume", {}).get("h24", 0) or 0)
                        avg_h   = vol_h24 / 24 if vol_h24 > 0 else 0
                        spike   = vol_h1 / avg_h if avg_h > 0 else 0

                        liquidity_usd = float(pair.get("liquidity", {}).get("usd", 0) or 0)

                        price_change = pair.get("priceChange", {})
                        change_5m  = float(price_change.get("m5",  0) or 0)
                        change_1h  = float(price_change.get("h1",  0) or 0)
                        change_24h = float(price_change.get("h24", 0) or 0)

                        market_cap = float(pair.get("marketCap", 0) or 0)
                        fdv        = float(pair.get("fdv", 0) or 0)

                        pair_created_at = pair.get("pairCreatedAt", 0)
                        vol_mcap_ratio = (vol_h24 / market_cap) if market_cap > 0 else 0

                        txns = pair.get("txns", {})
                        buys_h1   = int(txns.get("h1", {}).get("buys", 0) or 0)
                        sells_h1  = int(txns.get("h1", {}).get("sells", 0) or 0)
                        buys_h24  = int(txns.get("h24", {}).get("buys", 0) or 0)
                        sells_h24 = int(txns.get("h24", {}).get("sells", 0) or 0)

                        result = {
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
                        _cache_set(token_address, result)
                        return result
            return None
        except Exception as e:
            logger.error(f"Error loading token {token_address}: {e}")
            return None
