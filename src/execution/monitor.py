import asyncio
import json
import os
import time
from loguru import logger
from src.adapters.dexscreener import DexScreenerAdapter
from src.execution.executor import TradeExecutor

# ── Sell-Strategie Konfiguration (via .env anpassbar) ─────────────────────────
STOP_LOSS_PCT       = float(os.getenv("STOP_LOSS_PCT",       "0.20"))  # -20%  → alles verkaufen
TRAILING_STOP_PCT   = float(os.getenv("TRAILING_STOP_PCT",   "0.25"))  # -25% vom Höchststand
TRAILING_ACTIVATE   = float(os.getenv("TRAILING_ACTIVATE",   "0.30"))  # Trailing ab +30% Gewinn
TP1_PCT             = float(os.getenv("TP1_PCT",             "0.50"))  # +50%  → 50% verkaufen
TP2_PCT             = float(os.getenv("TP2_PCT",             "1.00"))  # +100% → 25% verkaufen
TP3_PCT             = float(os.getenv("TP3_PCT",             "2.00"))  # +200% → alles verkaufen
TP1_SELL_PCT        = float(os.getenv("TP1_SELL_PCT",        "0.50"))  # bei TP1: 50% der Position
TP2_SELL_PCT        = float(os.getenv("TP2_SELL_PCT",        "0.25"))  # bei TP2: 25% der Position
CHECK_INTERVAL      = int(os.getenv("MONITOR_INTERVAL",      "30"))    # alle 30 Sekunden prüfen
MAX_HOLD_HOURS      = float(os.getenv("MAX_HOLD_HOURS",      "24"))    # Auto-close nach 24h
STALE_EXIT_MIN_LOSS = float(os.getenv("STALE_EXIT_MIN_LOSS", "0.05"))  # Close stale if < +5%


class PositionMonitor:
    def __init__(self, state_file="positions.json"):
        self.state_file = state_file
        self.lock       = asyncio.Lock()
        self.executor   = TradeExecutor()
        self.dex        = DexScreenerAdapter()
        self.positions  = self._load_positions()

    # ── Persistenz ────────────────────────────────────────────────────────────
    def _load_positions(self) -> dict:
        if os.path.exists(self.state_file):
            try:
                with open(self.state_file, "r") as f:
                    return json.load(f)
            except Exception:
                pass
        return {}

    async def _save_positions(self):
        async with self.lock:
            with open(self.state_file, "w") as f:
                json.dump(self.positions, f, indent=2)

    # ── Position hinzufügen ───────────────────────────────────────────────────
    async def add_position(self, token_address: str, entry_price: float, symbol: str = "UNKNOWN"):
        async with self.lock:
            self.positions[token_address] = {
                "symbol":           symbol,
                "entry_price":      entry_price,
                "created_at":       time.time(),  # unix timestamp for age calc
                # Sell-Tracking
                "remaining_pct":    1.0,    # 100% der Position noch offen
                "tp1_hit":          False,  # TP1 (+50%) bereits ausgelöst?
                "tp2_hit":          False,  # TP2 (+100%) bereits ausgelöst?
                "tp3_hit":          False,  # TP3 (+200%) bereits ausgelöst?
                "highest_price":    entry_price,  # für Trailing Stop
                "trailing_active":  False,  # Trailing Stop erst ab Gewinn aktiviert
            }
        await self._save_positions()
        logger.info(
            f"[MONITOR] ✅ Position eröffnet: {symbol} @ ${entry_price:.8f} | "
            f"Stop-Loss: ${entry_price * (1 - STOP_LOSS_PCT):.8f} (-{int(STOP_LOSS_PCT*100)}%) | "
            f"Trailing: ab +{int(TRAILING_ACTIVATE*100)}% (drop -{int(TRAILING_STOP_PCT*100)}% vom ATH) | "
            f"TP1: ${entry_price * (1 + TP1_PCT):.8f} (+{int(TP1_PCT*100)}%) | "
            f"Max Hold: {MAX_HOLD_HOURS}h"
        )

    # ── Haupt-Monitor Loop ────────────────────────────────────────────────────
    async def monitor(self):
        logger.info(
            f"[MONITOR] Gestartet | Stop-Loss: -{int(STOP_LOSS_PCT*100)}% | "
            f"TP1: +{int(TP1_PCT*100)}% (50% sell) | "
            f"TP2: +{int(TP2_PCT*100)}% (25% sell) | "
            f"TP3: +{int(TP3_PCT*100)}% (alles sell)"
        )
        while True:
            await asyncio.sleep(CHECK_INTERVAL)
            if not self.positions:
                continue

            logger.info(f"[MONITOR] Prüfe {len(self.positions)} Positionen...")

            for address, pos in list(self.positions.items()):
                await self._check_position(address, pos)

    # ── Einzelne Position prüfen ──────────────────────────────────────────────
    async def _check_position(self, address: str, pos: dict):
        symbol      = pos.get("symbol", "UNKNOWN")
        entry_price = float(pos.get("entry_price", 0))
        remaining   = float(pos.get("remaining_pct", 1.0))

        if entry_price == 0 or remaining <= 0:
            return

        try:
            token_data    = await self.dex.get_token_data(address)
            if not token_data:
                return

            current_price = float(token_data.get("price_usd", 0))
            if current_price == 0:
                return

            highest_price = float(pos.get("highest_price", entry_price))

            # Update highest price (ATH tracking)
            if current_price > highest_price:
                highest_price = current_price
                async with self.lock:
                    self.positions[address]["highest_price"] = current_price
                await self._save_positions()

            change_pct     = (current_price - entry_price) / entry_price
            drop_from_ath  = (current_price - highest_price) / highest_price if highest_price > 0 else 0

            # Position age in hours
            created_at = pos.get("created_at", 0)
            age_hours  = (time.time() - created_at) / 3600 if created_at else 0

            logger.info(
                f"[MONITOR] {symbol} | "
                f"Einstieg: ${entry_price:.8f} | "
                f"Aktuell: ${current_price:.8f} | "
                f"ATH: ${highest_price:.8f} | "
                f"P/L: {change_pct:+.2%} | "
                f"Drop ATH: {drop_from_ath:+.2%} | "
                f"Age: {age_hours:.1f}h | "
                f"Offen: {int(remaining*100)}%"
            )

            # ──────────────────────────────────────────────────────────────────
            # STOP-LOSS: -20% from entry → alles verkaufen
            # ──────────────────────────────────────────────────────────────────
            if change_pct <= -STOP_LOSS_PCT:
                logger.warning(
                    f"[MONITOR] 🛑 STOP-LOSS für {symbol}! "
                    f"Verlust: {change_pct:.2%} | Verkaufe {int(remaining*100)}%"
                )
                await self._execute_sell(
                    symbol=symbol,
                    address=address,
                    current_price=current_price,
                    sell_fraction=remaining,
                    reason=f"Stop-Loss {change_pct:.2%}",
                    funnel_stage="STOP_LOSS",
                )
                await self._close_position(address)
                return

            # ──────────────────────────────────────────────────────────────────
            # TRAILING STOP: activate after +30%, sell if drops 25% from ATH
            # ──────────────────────────────────────────────────────────────────
            if change_pct >= TRAILING_ACTIVATE:
                if not pos.get("trailing_active"):
                    async with self.lock:
                        self.positions[address]["trailing_active"] = True
                    await self._save_positions()
                    logger.info(
                        f"[MONITOR] 📈 Trailing Stop AKTIVIERT für {symbol} "
                        f"(+{change_pct:.1%} > +{TRAILING_ACTIVATE:.0%})"
                    )

            if pos.get("trailing_active") and drop_from_ath <= -TRAILING_STOP_PCT:
                logger.warning(
                    f"[MONITOR] 📉 TRAILING STOP für {symbol}! "
                    f"ATH: ${highest_price:.8f} → Aktuell: ${current_price:.8f} "
                    f"(Drop: {drop_from_ath:.2%}) | Verkaufe {int(remaining*100)}%"
                )
                await self._execute_sell(
                    symbol=symbol,
                    address=address,
                    current_price=current_price,
                    sell_fraction=remaining,
                    reason=f"Trailing Stop (ATH drop {drop_from_ath:.2%})",
                    funnel_stage="TRAILING_STOP",
                )
                await self._close_position(address)
                return

            # ──────────────────────────────────────────────────────────────────
            # TIME-BASED EXIT: close stale positions after MAX_HOLD_HOURS
            # ──────────────────────────────────────────────────────────────────
            if age_hours >= MAX_HOLD_HOURS and change_pct < STALE_EXIT_MIN_LOSS:
                logger.warning(
                    f"[MONITOR] ⏰ TIME EXIT für {symbol}! "
                    f"Alter: {age_hours:.1f}h > {MAX_HOLD_HOURS}h | "
                    f"P/L: {change_pct:+.2%} (< +{STALE_EXIT_MIN_LOSS:.0%}) | "
                    f"Verkaufe {int(remaining*100)}%"
                )
                await self._execute_sell(
                    symbol=symbol,
                    address=address,
                    current_price=current_price,
                    sell_fraction=remaining,
                    reason=f"Time exit ({age_hours:.1f}h, P/L {change_pct:+.2%})",
                    funnel_stage="TIME_EXIT",
                )
                await self._close_position(address)
                return

            # ──────────────────────────────────────────────────────────────────
            # TAKE PROFIT 3: +200% → alles verkaufen
            # ──────────────────────────────────────────────────────────────────
            if not pos.get("tp3_hit") and change_pct >= TP3_PCT:
                logger.success(
                    f"[MONITOR] 🚀 TP3 +{int(TP3_PCT*100)}% für {symbol}! "
                    f"Verkaufe restliche {int(remaining*100)}% der Position"
                )
                await self._execute_sell(
                    symbol=symbol,
                    address=address,
                    current_price=current_price,
                    sell_fraction=remaining,
                    reason=f"Take-Profit 3 (+{int(TP3_PCT*100)}%)",
                    funnel_stage="TP3",
                )
                await self._close_position(address)
                return

            # ──────────────────────────────────────────────────────────────────
            # TAKE PROFIT 2: +100% → 25% verkaufen
            # ──────────────────────────────────────────────────────────────────
            if not pos.get("tp2_hit") and change_pct >= TP2_PCT:
                sell_amount = TP2_SELL_PCT  # 25%
                logger.success(
                    f"[MONITOR] 💰 TP2 +{int(TP2_PCT*100)}% für {symbol}! "
                    f"Verkaufe {int(sell_amount*100)}% der Position"
                )
                await self._execute_sell(
                    symbol=symbol,
                    address=address,
                    current_price=current_price,
                    sell_fraction=sell_amount,
                    reason=f"Take-Profit 2 (+{int(TP2_PCT*100)}%)",
                    funnel_stage="TP2",
                )
                async with self.lock:
                    self.positions[address]["tp2_hit"]       = True
                    self.positions[address]["remaining_pct"] = round(remaining - sell_amount, 4)
                await self._save_positions()
                return

            # ──────────────────────────────────────────────────────────────────
            # TAKE PROFIT 1: +50% → 50% verkaufen
            # ──────────────────────────────────────────────────────────────────
            if not pos.get("tp1_hit") and change_pct >= TP1_PCT:
                sell_amount = TP1_SELL_PCT  # 50%
                logger.success(
                    f"[MONITOR] 💚 TP1 +{int(TP1_PCT*100)}% für {symbol}! "
                    f"Verkaufe {int(sell_amount*100)}% der Position"
                )
                await self._execute_sell(
                    symbol=symbol,
                    address=address,
                    current_price=current_price,
                    sell_fraction=sell_amount,
                    reason=f"Take-Profit 1 (+{int(TP1_PCT*100)}%)",
                    funnel_stage="TP1",
                )
                async with self.lock:
                    self.positions[address]["tp1_hit"]       = True
                    self.positions[address]["remaining_pct"] = round(remaining - sell_amount, 4)
                await self._save_positions()
                return

        except Exception as e:
            logger.error(f"[MONITOR] Fehler bei {symbol}: {e}")

    # ── Sell ausführen ────────────────────────────────────────────────────────
    async def _execute_sell(
        self,
        symbol:        str,
        address:       str,
        current_price: float,
        sell_fraction: float,
        reason:        str,
        funnel_stage:  str,
    ):
        pos          = self.positions.get(address, {})
        entry_price  = float(pos.get("entry_price", 0))
        pos_size     = float(os.getenv("TRADE_MAX_POSITION_USD", "0.20"))
        sell_usd     = pos_size * sell_fraction

        # P/L für diesen Sell berechnen
        if entry_price > 0:
            change_pct = (current_price - entry_price) / entry_price
            pl_usd     = sell_usd * change_pct
            logger.info(
                f"[MONITOR] 💵 Sell {symbol}: "
                f"${sell_usd:.4f} investiert → "
                f"P/L: ${pl_usd:+.4f} ({change_pct:+.2%})"
            )

        await self.executor.execute_trade(
            token_symbol=symbol,
            token_address=address,
            score=0,
            decision="SELL",
            price=current_price,
            rejection_reason=reason,
            funnel_stage=funnel_stage,
        )

    # ── Position schließen ────────────────────────────────────────────────────
    async def _close_position(self, address: str):
        sym = self.positions.get(address, {}).get("symbol", address[:8])
        async with self.lock:
            if address in self.positions:
                del self.positions[address]
        await self._save_positions()
        logger.info(f"[MONITOR] Position geschlossen: {sym}")
