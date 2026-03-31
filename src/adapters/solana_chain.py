import os
from loguru import logger
from src.utils.rpc import rpc_call

# Known liquidity lock / burn addresses on Solana
KNOWN_LOCK_ADDRESSES = {
    "1111111111111111111111111111111111",              # System burn
    "1nc1nerator11111111111111111111111111111111",      # Incinerator
    "5Q544fKrFoe6tsEbD7S8EmxGTJYAKtTVhAW5Q5pge4j1",   # Raydium Authority
    "TokenkegQfeZyiNwAJbNbGKPFXCWuBvf9Ss623VQ5DA",     # Token Program (LP burn)
}


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

    async def get_holder_count(self, token_address: str) -> int:
        """Estimate holder count from largest accounts response."""
        try:
            largest = rpc_call("getTokenLargestAccounts", [token_address])
            if not largest:
                return 0
            # The RPC returns up to 20 largest holders.
            # If all 20 have a non-zero balance, there are likely many holders.
            holders = [a for a in largest["value"] if float(a.get("uiAmount", 0) or 0) > 0]
            return len(holders)
        except Exception as e:
            logger.error(f"Holder count Fehler: {e}")
            return 0

    async def check_liquidity_locked(self, token_address: str) -> bool:
        """
        Check if liquidity is locked/burned by examining the largest LP token holders.
        Returns True if >50% of LP tokens are held by known lock/burn addresses.
        """
        try:
            largest = rpc_call("getTokenLargestAccounts", [token_address])
            if not largest or not largest.get("value"):
                return False

            supply_result = rpc_call("getTokenSupply", [token_address])
            if not supply_result:
                return False
            total_supply = float(supply_result["value"]["uiAmount"] or 0)
            if total_supply == 0:
                return False

            locked_amount = 0.0
            for account in largest["value"][:10]:
                owner_address = account.get("address", "")
                amount = float(account.get("uiAmount", 0) or 0)
                # Check if any top holder is a known lock/burn address
                if owner_address in KNOWN_LOCK_ADDRESSES:
                    locked_amount += amount

            locked_pct = (locked_amount / total_supply) * 100 if total_supply > 0 else 0
            logger.info(f"[CHAIN] Liquidity locked: {locked_pct:.1f}% in known lock addresses")
            # Consider locked if >50% of supply is in lock addresses
            return locked_pct > 50

        except Exception as e:
            logger.error(f"Liquidity lock check Fehler: {e}")
            return False

    async def get_chain_data(self, token_address: str) -> dict:
        top_10_pct    = await self.get_top_10_holder_percent(token_address)
        is_locked     = await self.check_liquidity_locked(token_address)
        holder_count  = await self.get_holder_count(token_address)
        logger.info(
            f"[CHAIN] {token_address[:12]}... "
            f"Top-10: {top_10_pct:.1f}% | Locked: {is_locked} | Holders: {holder_count}"
        )
        return {
            "top_10_holder_percent": top_10_pct,
            "liquidity_locked":      is_locked,
            "holder_count":          holder_count,
        }
