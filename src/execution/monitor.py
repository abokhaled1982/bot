import asyncio
import json
import os
from loguru import logger
from src.adapters.dexscreener import DexScreenerAdapter
from src.execution.executor import TradeExecutor


class PositionMonitor:
    def __init__(self, state_file="positions.json"):
        self.state_file    = state_file
        self.lock          = asyncio.Lock()
        self.executor      = TradeExecutor()
        self.dex           = DexScreenerAdapter()
        self.stop_loss_pct = float(os.getenv("STOP_LOSS_PCT", "0.15"))  # 15%
        self.positions     = self._load_positions()

    # ── Persistenz ─────────────────────────────────────────────────────────────
    def _load_positions(self) -> dict:
        if os.path.exists(self.state_file):
            with open(self.state_file, "r") as f:
                return json.load(f)
        return {}

    async def _save_positions(self):
        async with self.lock:
            with open(self.state_file, "w") as f:
                json.dump(self.positions, f, indent=2)

    # ── Position hinzufügen (jetzt mit Symbol!) ─────────────────────────────
    async def add_position(self, token_address: str, entry_price: float, symbol: str = "UNKNOWN"):
        async with self.lock:
            self.positions[token_address] = {
                "symbol":      symbol,
                "entry_price": entry_price,
                "timestamp":   asyncio.get_event_loop().time(),
            }
        await self._save_positions()
        logger.info(f"[MONITOR] Position hinzugefuegt: {symbol} @ ${entry_price}")

    # ── Stop-Loss Loop ──────────────────────────────────────────────────────────
    async def monitor(self):
        while True:
            await asyncio.sleep(30)
            if not self.positions:
                continue

            logger.info(f"[MONITOR] Pruefe {len(self.positions)} aktive Positionen...")

            for address, data in list(self.positions.items()):
                symbol = data.get("symbol", "UNKNOWN")
                try:
                    token_data    = await self.dex.get_token_data(address)
                    if not token_data:
                        continue

                    current_price = token_data.get("price_usd", 0)
                    entry_price   = data.get("entry_price", 0)

                    if current_price == 0 or entry_price == 0:
                        continue

                    loss = (entry_price - current_price) / entry_price

                    if loss >= self.stop_loss_pct:
                        logger.warning(
                            f"[MONITOR] Stop-Loss ausgeloest fuer {symbol}! "
                            f"Verlust: {loss:.2%}"
                        )
                        await self.executor.execute_trade(
                            token_symbol=symbol,
                            token_address=address,
                            score=0,
                            decision="SELL",
                            price=current_price,
                            rejection_reason=f"Stop-Loss {loss:.2%}",
                            funnel_stage="SELL_EXEC",
                        )
                        async with self.lock:
                            del self.positions[address]
                        await self._save_positions()
                        logger.info(f"[MONITOR] {symbol} aus Positionen entfernt.")

                except Exception as e:
                    logger.error(f"[MONITOR] Fehler bei {symbol} ({address}): {e}")
