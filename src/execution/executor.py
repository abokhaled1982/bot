import os
import base58
import sqlite3
from datetime import datetime
from loguru import logger
from dotenv import load_dotenv
from solders.keypair import Keypair
from solana.rpc.async_api import AsyncClient

load_dotenv()

class TradeExecutor:
    def __init__(self):
        self.dry_run = os.getenv("DRY_RUN", "True").lower() == "true"
        self.max_position_usd = float(os.getenv("TRADE_MAX_POSITION_USD", "0.20"))
        self.rpc_url = os.getenv("SOLANA_RPC_URL", "https://api.mainnet-beta.solana.com")
        self.client = AsyncClient(self.rpc_url)
        self.db_path = 'memecoin_bot.db'
        self.private_key = os.getenv("SOLANA_PRIVATE_KEY")
        if self.private_key:
            try:
                decoded = base58.b58decode(self.private_key.strip())
                if len(decoded) == 64:
                    self.keypair = Keypair.from_bytes(decoded)
                else:
                    self.keypair = Keypair.from_seed(decoded)
            except Exception as e:
                logger.error(f"Error initializing Keypair: {e}")
                self.keypair = None
        else:
            self.keypair = None

    def _log_to_db(self, symbol, address, price, size, score, decision, rejection_reason=None, ai_reasoning=None, funnel_stage="FINAL"):
        conn = sqlite3.connect(self.db_path)
        c = conn.cursor()
        c.execute("INSERT INTO trades (token_address, symbol, entry_price, position_size, score, decision, rejection_reason, ai_reasoning, funnel_stage, timestamp) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                  (address, symbol, price, size, score, decision, rejection_reason, ai_reasoning, funnel_stage, datetime.now()))
        conn.commit()
        conn.close()

    def calculate_position_size(self, score: float) -> float:
        if score >= 90:
            return self.max_position_usd
        elif score >= 80:
            return self.max_position_usd * 0.75
        elif score >= 72:
            return self.max_position_usd * 0.50
        else:
            return 0.0

    async def execute_trade(self, token_symbol: str, token_address: str, score: float, decision: str, price: float = 0.0, rejection_reason: str = None, ai_reasoning: str = None, funnel_stage: str = "FINAL") -> dict:
        position_size = self.calculate_position_size(score)
        
        if decision != "BUY" or position_size <= 0:
            logger.info(f"[{token_symbol}] Decision: {decision}, Reason: {rejection_reason}")
            self._log_to_db(token_symbol, token_address, price, 0, score, decision, rejection_reason, ai_reasoning, funnel_stage)
            return None

        if self.dry_run:
            logger.warning(f"[DRY-RUN] Would buy {position_size} USD of {token_symbol} ({token_address})")
            self._log_to_db(token_symbol, token_address, price, position_size, score, "BUY_DRY", ai_reasoning=ai_reasoning, funnel_stage=funnel_stage)
            return {"status": "success", "dry_run": True}
        else:
            if not self.keypair:
                logger.error(f"Cannot execute live trade: No private key configured.")
                return None
            logger.info(f"[LIVE] Attempting to buy {position_size} USD of {token_symbol}...")
            self._log_to_db(token_symbol, token_address, price, position_size, score, "BUY", ai_reasoning=ai_reasoning, funnel_stage=funnel_stage)
            return {"status": "success", "tx": "mock_tx_hash"}
