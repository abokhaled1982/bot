import asyncio
import os
import sqlite3
from datetime import datetime
from loguru import logger
from solana.rpc.async_api import AsyncClient
from solders.pubkey import Pubkey
from spl.token.client import Token
from spl.token.constants import TOKEN_PROGRAM_ID

class SafetyAdapter:
    def __init__(self):
        self.rpc_url = os.getenv("SOLANA_RPC_URL", "https://api.mainnet-beta.solana.com")
        self.client = AsyncClient(self.rpc_url)

    async def get_safety_details(self, token_address: str) -> dict:
        try:
            if token_address.startswith("0x"):
                return {"is_safe": False, "reason": "EVM Address"}
            pubkey = Pubkey.from_string(token_address)
            resp = await self.client.get_account_info(pubkey)
            if not resp.value or not resp.value.data:
                return {"is_safe": False, "reason": "No Account Info", "address": token_address}

            data = resp.value.data
            mint_authority = data[4:36]
            is_revoked = all(b == 0 for b in mint_authority)
            
            return {
                "is_safe": is_revoked,
                "mint_authority": "Revoked" if is_revoked else "Active",
                "authority_hex": mint_authority.hex(),
                "total_supply": int.from_bytes(data[36:44], "little")
            }
        except Exception as e:
            logger.error(f"Safety check error: {e}")
            return {"is_safe": False, "reason": str(e), "address": token_address}

    async def is_safe(self, token_address: str) -> bool:
        details = await self.get_safety_details(token_address)
        return details.get("is_safe", False)
