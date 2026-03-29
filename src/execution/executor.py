import os
import base58
from loguru import logger
from dotenv import load_dotenv
from solders.keypair import Keypair
from solana.rpc.async_api import AsyncClient

load_dotenv()

class TradeExecutor:
    def __init__(self):
        self.dry_run = os.getenv("DRY_RUN", "True").lower() == "true"
        self.max_position_usd = float(os.getenv("TRADE_MAX_POSITION_USD", "50.0"))
        self.rpc_url = os.getenv("SOLANA_RPC_URL", "https://api.mainnet-beta.solana.com")
        self.client = AsyncClient(self.rpc_url)
        self.private_key = os.getenv("SOLANA_PRIVATE_KEY")
        if self.private_key:
            # Try to interpret as base58 string
            try:
                decoded = base58.b58decode(self.private_key.strip())
                # If length is 64, it's [privkey + pubkey], if 32, it's just privkey
                if len(decoded) == 64:
                    self.keypair = Keypair.from_bytes(decoded)
                else:
                    self.keypair = Keypair.from_seed(decoded)
            except Exception as e:
                logger.error(f"Error initializing Keypair: {e}")
                self.keypair = None
        else:
            self.keypair = None
        
    def calculate_position_size(self, score: float) -> float:
        if score >= 90:
            return self.max_position_usd
        elif score >= 80:
            return self.max_position_usd * 0.75
        elif score >= 72:
            return self.max_position_usd * 0.50
        else:
            return 0.0

    async def execute_trade(self, token_symbol: str, token_address: str, score: float, decision: str) -> dict:
        position_size = self.calculate_position_size(score)
        
        if decision != "BUY" or position_size <= 0:
            logger.info(f"[{token_symbol}] No trade executed. Decision: {decision}, Score: {score}")
            return None

        if self.dry_run:
            logger.warning(f"[DRY-RUN] Would buy {position_size} USD of {token_symbol} ({token_address})")
            return {"dry_run": True}
        else:
            if not self.keypair:
                logger.error(f"Cannot execute live trade: No private key configured.")
                return None
            logger.info(f"[LIVE] Attempting to buy {position_size} USD of {token_symbol}...")
            # Here: Integrate Jupiter/Raydium swap logic
            return {"status": "success", "tx": "mock_tx_hash"}
