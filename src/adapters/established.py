import json
import os
import aiohttp
from loguru import logger


COINS_FILE = os.path.join(os.path.dirname(__file__), "..", "..", "established_coins.json")


class EstablishedAdapter:
    """
    Prüft etablierte Memecoins auf Kauf-Signale.
    Filtert nach Volume-Spike, positiver Preisentwicklung und Buy-Pressure.
    """

    def __init__(self):
        self.coins = self._load_coins()
        self.base_url = "https://api.dexscreener.com/latest/dex/tokens/"
        logger.info(f"[ESTABLISHED] {len(self.coins)} etablierte Coins geladen")

    def _load_coins(self) -> list:
        try:
            with open(COINS_FILE) as f:
                data = json.load(f)
                return data.get("coins", [])
        except Exception as e:
            logger.error(f"[ESTABLISHED] Fehler beim Laden: {e}")
            return []

    async def _fetch_token(self, session: aiohttp.ClientSession, coin: dict) -> dict | None:
        """Hole DexScreener-Daten für einen etablierten Coin und prüfe auf Signale."""
        address = coin["address"]
        symbol = coin["symbol"]
        try:
            async with session.get(
                f"{self.base_url}{address}",
                timeout=aiohttp.ClientTimeout(total=8),
            ) as resp:
                if resp.status != 200:
                    return None
                data = await resp.json()
                pairs = data.get("pairs", [])
                if not pairs:
                    return None

                pair = pairs[0]
                if pair.get("chainId") != "solana":
                    return None

                vol_h1 = float(pair.get("volume", {}).get("h1", 0) or 0)
                vol_h24 = float(pair.get("volume", {}).get("h24", 0) or 0)
                avg_h = vol_h24 / 24 if vol_h24 > 0 else 0
                spike = vol_h1 / avg_h if avg_h > 0 else 0

                ch_1h = float(pair.get("priceChange", {}).get("h1", 0) or 0)
                ch_5m = float(pair.get("priceChange", {}).get("m5", 0) or 0)
                liq = float(pair.get("liquidity", {}).get("usd", 0) or 0)

                txns = pair.get("txns", {})
                buys_h1 = int(txns.get("h1", {}).get("buys", 0) or 0)
                sells_h1 = int(txns.get("h1", {}).get("sells", 0) or 0)
                total_txns = buys_h1 + sells_h1
                buy_ratio = buys_h1 / total_txns if total_txns > 0 else 0.5

                # ── Signal-Filter: Nur Coins mit aktivem Momentum ────────
                # Etablierte Coins brauchen einen spürbaren Impuls
                has_signal = (
                    spike >= 1.3            # Volume-Spike vorhanden
                    and ch_1h >= -2         # Nicht stark fallend
                    and buy_ratio >= 0.40   # Mehr Käufer als Verkäufer
                    and total_txns >= 15    # Genug Aktivität
                    and liq >= 10_000       # Mindest-Liquidität
                )

                if has_signal:
                    logger.info(
                        f"[ESTABLISHED] SIGNAL: {symbol} | "
                        f"Spike: {spike:.1f}x | 1h: {ch_1h:+.1f}% | "
                        f"Buy: {buy_ratio:.0%} | Txns: {total_txns} | Liq: ${liq:,.0f}"
                    )
                    return {
                        "symbol": symbol,
                        "address": address,
                        "source": "established",
                        "spike": spike,
                        "change_1h": ch_1h,
                        "buy_ratio": buy_ratio,
                    }
                return None

        except Exception as e:
            logger.debug(f"[ESTABLISHED] Fehler bei {symbol}: {e}")
            return None

    async def get_candidates(self) -> list:
        """
        Prüfe alle etablierten Coins parallel auf Kauf-Signale.
        Gibt nur Coins mit aktivem Momentum zurück.
        """
        import asyncio

        candidates = []
        async with aiohttp.ClientSession() as session:
            # Parallel abfragen (max 10 gleichzeitig)
            semaphore = asyncio.Semaphore(10)

            async def check_with_limit(coin):
                async with semaphore:
                    return await self._fetch_token(session, coin)

            tasks = [check_with_limit(coin) for coin in self.coins]
            results = await asyncio.gather(*tasks, return_exceptions=True)

            for result in results:
                if isinstance(result, dict) and result is not None:
                    candidates.append(result)

        # Sortiere nach Spike (stärkstes Signal zuerst)
        candidates.sort(key=lambda x: x.get("spike", 0), reverse=True)

        logger.info(
            f"[ESTABLISHED] {len(candidates)}/{len(self.coins)} Coins mit Signal"
        )
        return candidates
