import asyncio
import json
import os
from loguru import logger
from src.adapters.dexscreener import DexScreenerAdapter
from src.execution.executor import TradeExecutor

class PositionMonitor:
    def __init__(self, state_file="positions.json"):
        self.state_file = state_file
        self.lock = asyncio.Lock()
        self.executor = TradeExecutor()
        self.dex = DexScreenerAdapter()
        self.stop_loss_pct = float(os.getenv("STOP_LOSS_PCT", "0.15")) # 15% loss
        self.positions = self._load_positions()

    def _load_positions(self):
        if os.path.exists(self.state_file):
            with open(self.state_file, 'r') as f:
                return json.load(f)
        return {}

    async def _save_positions(self):
        async with self.lock:
            with open(self.state_file, 'w') as f:
                json.dump(self.positions, f)

    async def add_position(self, token_address, entry_price):
        async with self.lock:
            self.positions[token_address] = {
                "entry_price": entry_price,
                "timestamp": asyncio.get_event_loop().time()
            }
        await self._save_positions()

    async def monitor(self):
        while True:
            await asyncio.sleep(30) # Efficient check interval
            if not self.positions:
                continue
                
            logger.info("Monitoring active positions for stop-loss...")
            
            # Use a copy to avoid mutation during iteration
            for address, data in list(self.positions.items()):
                try:
                    token_data = await self.dex.get_token_data(address)
                    if not token_data: continue
                    
                    current_price = token_data.get("price_usd", 0)
                    if current_price == 0: continue
                    
                    loss = (data["entry_price"] - current_price) / data["entry_price"]
                    
                    if loss >= self.stop_loss_pct:
                        logger.warning(f"Stop-loss triggered for {address}! Loss: {loss:.2%}")
                        # Trigger sell logic
                        await self.executor.execute_trade(address, "SELL")
                        
                        # Remove from monitor
                        async with self.lock:
                            del self.positions[address]
                        await self._save_positions()
                except Exception as e:
                    logger.error(f"Error monitoring {address}: {e}")
