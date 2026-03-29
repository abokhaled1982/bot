import os
import aiohttp
from loguru import logger
from dotenv import load_dotenv

load_dotenv()

class SolanaAdapter:
    def __init__(self):
        # We use standard RPC or fallback to public if not set
        self.rpc_url = os.getenv("SOLANA_RPC_URL", "https://api.mainnet-beta.solana.com")
        self.headers = {"Content-Type": "application/json"}

    async def _rpc_call(self, method: str, params: list) -> dict:
        """Helper to make JSON-RPC calls to Solana node"""
        payload = {
            "jsonrpc": "2.0",
            "id": 1,
            "method": method,
            "params": params
        }
        try:
            async with aiohttp.ClientSession() as session:
                async with session.post(self.rpc_url, json=payload, headers=self.headers) as resp:
                    if resp.status == 200:
                        data = await resp.json()
                        return data.get("result", None)
                    else:
                        logger.error(f"Solana RPC Error: {resp.status} - {await resp.text()}")
                        return None
        except Exception as e:
            logger.error(f"Error calling Solana RPC: {e}")
            return None

    async def get_top_10_holder_percent(self, token_address: str) -> float:
        """
        Fetch the top 10 token accounts and calculate what percentage 
        of the circulating supply they hold.
        """
        try:
            # 1. Get total supply
            supply_result = await self._rpc_call("getTokenSupply", [token_address])
            if not supply_result or "value" not in supply_result:
                logger.warning(f"Could not fetch supply for {token_address}")
                return 100.0
                
            total_supply = float(supply_result["value"]["uiAmount"])
            if total_supply == 0:
                return 100.0

            # 2. Get largest accounts
            largest_accounts_result = await self._rpc_call("getTokenLargestAccounts", [token_address])
            if not largest_accounts_result or "value" not in largest_accounts_result:
                logger.warning(f"Could not fetch largest accounts for {token_address}")
                return 100.0

            # Sum up top 10 holders (excluding Raydium/Dex pools if possible, 
            # but for simplicity we sum the top 10 raw accounts here)
            top_10 = largest_accounts_result["value"][:10]
            top_10_amount = sum(float(acc["uiAmount"]) for acc in top_10)

            percentage = (top_10_amount / total_supply) * 100
            return percentage
            
        except Exception as e:
            logger.error(f"Failed to calculate top 10 holder percent for {token_address}: {e}")
            return 100.0 # Fail safe: assume high risk

    async def check_liquidity_locked(self, token_address: str) -> bool:
        """
        Checking if liquidity is locked on Solana requires verifying the 
        LP token holders or checking specific lock contracts (like PinkSale, Unicrypt, etc.).
        This is complex to do purely via RPC without a dedicated indexer or API like Birdeye.
        
        For this MVP adapter, we implement a placeholder that returns True.
        In a production scenario, you would query an API like Birdeye or DexScreener's locked flags.
        """
        logger.info(f"Checking liquidity lock for {token_address} (Note: using fallback logic)")
        
        # In a real implementation:
        # 1. Find the Raydium Pool for this token
        # 2. Get the LP token mint address
        # 3. Check if the largest LP token accounts are burned (address 11111111111111111111111111111111) 
        #    or held by known lock contracts.
        
        # Temporary placeholder: We assume locked to allow the flow to continue, 
        # but log a warning.
        return True

    async def get_chain_data(self, token_address: str) -> dict:
        """Aggregates on-chain metrics for the fusion engine."""
        top_10_pct = await self.get_top_10_holder_percent(token_address)
        is_locked = await self.check_liquidity_locked(token_address)
        
        logger.info(f"[{token_address}] Top-10 Holders: {top_10_pct:.2f}% | Liq Locked: {is_locked}")
        
        return {
            "top_10_holder_percent": top_10_pct,
            "liquidity_locked": is_locked
        }

if __name__ == "__main__":
    import asyncio
    async def test():
        adapter = SolanaAdapter()
        # Test with BONK contract
        data = await adapter.get_chain_data("DezXAZ8z7PnrnRJjz3wXBoRgixCa6xjnB7YaB1pPB263")
        print(data)
    
    asyncio.run(test())
