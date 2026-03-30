import asyncio
import os
from loguru import logger
from solana.rpc.async_api import AsyncClient
from solders.pubkey import Pubkey

class SafetyAdapter:
    def __init__(self):
        self.rpc_url = os.getenv("SOLANA_RPC_URL", "https://api.mainnet-beta.solana.com")
        self.client = AsyncClient(self.rpc_url)

    async def is_safe(self, token_address: str) -> bool:
        """
        Check if a token has a revoked mint authority.
        """
        try:
            if token_address.startswith("0x"):
                return False
            pubkey = Pubkey.from_string(token_address)
            
            # Fetch the account info directly from the async client
            resp = await self.client.get_account_info(pubkey)
            
            if resp.value is None or resp.value.data is None:
                logger.warning(f"Could not fetch account data for {token_address}")
                return False
            
            # Data for a Mint account in SPL Token is 82 bytes.
            # The mint_authority is at bytes 4-36.
            # If all bytes are 0, it's considered revoked.
            data = resp.value.data
            if len(data) < 36:
                return False
                
            mint_authority = data[4:36]
            is_revoked = all(b == 0 for b in mint_authority)
            
            if is_revoked:
                logger.info(f"On-chain check PASSED: {token_address} has revoked mint authority.")
                return True
            else:
                logger.warning(f"On-chain check FAILED: {token_address} has active mint authority.")
                return False
            
        except Exception as e:
            logger.error(f"On-chain safety check failed for {token_address}: {e}. Failing closed.")
            return False
