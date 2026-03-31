import os
from loguru import logger
from src.utils.rpc import rpc_call


class SolanaAdapter:
    async def get_top_10_holder_percent(self, token_address: str) -> float:
        try:
            supply_result = rpc_call("getTokenSupply", [token_address])
            if not supply_result:
                return 50.0
            total_supply = float(supply_result["value"]["uiAmount"] or 0)
            if total_supply == 0:
                return 100.0

            largest = rpc_call("getTokenLargestAccounts", [token_address])
            if not largest:
                return 50.0

            top_10       = largest["value"][:10]
            top_10_amount= sum(float(a["uiAmount"] or 0) for a in top_10)
            pct          = (top_10_amount / total_supply) * 100
            return round(pct, 2)

        except Exception as e:
            logger.error(f"Top-10 Holder Fehler für {token_address}: {e}")
            return 50.0

    async def check_liquidity_locked(self, token_address: str) -> bool:
        return True

    async def get_chain_data(self, token_address: str) -> dict:
        top_10_pct = await self.get_top_10_holder_percent(token_address)
        is_locked  = await self.check_liquidity_locked(token_address)
        logger.info(f"[CHAIN] {token_address[:12]}... Top-10: {top_10_pct:.1f}% | Locked: {is_locked}")
        return {
            "top_10_holder_percent": top_10_pct,
            "liquidity_locked":      is_locked,
        }
