"""
Raydium V3 API Adapter — Free, no API key required.

Discovers trending Solana memecoins via Raydium's public pool API.
Returns top pools by 24h volume, filtering for SOL-paired meme pools.

Endpoints used:
  - GET api-v3.raydium.io/pools/info/list  (pool list with volume, TVL, burn%)

Rate limits: undocumented, but generous for read-only queries.
"""
from __future__ import annotations

import aiohttp
from loguru import logger

# Base mints to filter out (we want the OTHER side of the pair)
_BASE_MINTS = {
    "So11111111111111111111111111111111111111112",    # WSOL
    "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v",  # USDC
    "Es9vMFrzaCERmJfrF4H2FYD4KCoNkY11McCe8BenwNYB",  # USDT
}

POOL_LIST_URL = "https://api-v3.raydium.io/pools/info/list"


class RaydiumAdapter:

    def __init__(self):
        self._timeout = aiohttp.ClientTimeout(total=12)

    async def get_top_volume_tokens(self, limit: int = 20) -> list:
        """
        Fetch Raydium standard pools sorted by 24h volume.
        Returns memecoin candidates (the non-SOL/USDC side of each pair).
        """
        candidates = []
        seen = set()
        try:
            async with aiohttp.ClientSession() as session:
                params = {
                    "poolType":      "standard",
                    "poolSortField": "volume24h",
                    "sortType":      "desc",
                    "pageSize":      str(min(limit * 2, 50)),  # over-fetch to account for filtering
                    "page":          "1",
                }
                async with session.get(
                    POOL_LIST_URL, params=params, timeout=self._timeout,
                ) as r:
                    if r.status != 200:
                        logger.warning(f"[RAYDIUM] Pool list failed: HTTP {r.status}")
                        return []
                    data = await r.json()

                pools = data.get("data", {}).get("data", [])
                for pool in pools:
                    mint_a = pool.get("mintA", {}) if isinstance(pool.get("mintA"), dict) else {}
                    mint_b = pool.get("mintB", {}) if isinstance(pool.get("mintB"), dict) else {}

                    addr_a = mint_a.get("address", "")
                    addr_b = mint_b.get("address", "")

                    # Find the memecoin side (not SOL/USDC/USDT)
                    if addr_a in _BASE_MINTS and addr_b and addr_b not in _BASE_MINTS:
                        token_addr = addr_b
                        token_sym  = mint_b.get("symbol", addr_b[:8])
                    elif addr_b in _BASE_MINTS and addr_a and addr_a not in _BASE_MINTS:
                        token_addr = addr_a
                        token_sym  = mint_a.get("symbol", addr_a[:8])
                    else:
                        continue  # skip non-SOL pairs (USDC/USDT, etc.)

                    if token_addr in seen:
                        continue
                    seen.add(token_addr)

                    vol_24h = float(pool.get("day", {}).get("volume", 0) or 0)
                    tvl     = float(pool.get("tvl", 0) or 0)
                    burn    = float(pool.get("burnPercent", 0) or 0)

                    # Filter: needs real activity
                    if vol_24h < 10_000 or tvl < 5_000:
                        continue

                    candidates.append({
                        "symbol":      token_sym,
                        "address":     token_addr,
                        "source":      "raydium_top_vol",
                        # Extra metadata for scoring
                        "raydium_vol_24h": vol_24h,
                        "raydium_tvl":     tvl,
                        "raydium_burn":    burn,
                        "pool_id":         pool.get("id", ""),
                    })

                    if len(candidates) >= limit:
                        break

        except Exception as e:
            logger.error(f"[RAYDIUM] Error fetching top volume pools: {e}")

        logger.info(f"[RAYDIUM] Top volume: {len(candidates)} memecoin candidates")
        return candidates

    async def get_candidates(self) -> list:
        """Main entry point — returns deduplicated candidates."""
        return await self.get_top_volume_tokens(limit=20)
