import os
from loguru import logger
from dotenv import load_dotenv

load_dotenv()

class TradeExecutor:
    def __init__(self):
        self.dry_run = os.getenv("DRY_RUN", "True").lower() == "true"
        self.max_position_usd = float(os.getenv("TRADE_MAX_POSITION_USD", "50.0"))
        
    def calculate_position_size(self, score: float) -> float:
        """
        Risk Management:
        - Bei Score 72–80: 1% Position
        - Bei Score 80–90: 1.5% Position
        - Bei Score 90+: 2% Position
        
        Note: The config defines a max position size in USD, we use that as the base 2%.
        """
        if score >= 90:
            return self.max_position_usd
        elif score >= 80:
            return self.max_position_usd * 0.75 # 1.5% is 75% of 2%
        elif score >= 72:
            return self.max_position_usd * 0.50 # 1% is 50% of 2%
        else:
            return 0.0

    async def execute_trade(self, token_symbol: str, token_address: str, score: float, decision: str) -> dict:
        """Execute a trade, respecting Dry-Run mode and Risk Management."""
        
        position_size = self.calculate_position_size(score)
        
        if decision != "BUY" or position_size <= 0:
            logger.info(f"[{token_symbol}] No trade executed. Decision: {decision}, Score: {score}")
            return None

        if self.dry_run:
            logger.warning(f"[DRY-RUN] Would buy {position_size} USD of {token_symbol} ({token_address})")
            return {
                "token_address": token_address,
                "amount_usd": position_size,
                "entry_price": 0.0, # Placeholder
                "dry_run": True
            }
        else:
            # Here we would actually call Solana/Ethereum DEX APIs
            logger.error(f"LIVE TRADING NOT IMPLEMENTED YET. Skipping buy of {token_symbol}.")
            # Require explicit confirmation before ever doing this!
            return None
