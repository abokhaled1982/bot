"""
src/execution/monitor.py — Professional Position Monitor

Responsibilities:
  • Watch open positions on a 30-second polling loop
  • Fetch current price with DexScreener → Jupiter fallback
  • Trigger Stop-Loss, Trailing-Stop, Take-Profit, Time-Exit sell actions
  • Emit structured events (bot_events) for each sell trigger
  • Sync wallet positions so manually bought tokens are tracked

Strategy is fully configurable via environment variables.
"""
from __future__ import annotations

import asyncio
import json
import os
import time
import aiohttp
import requests
from loguru import logger

from src.adapters.dexscreener import DexScreenerAdapter
from src.execution.executor   import TradeExecutor
from src.execution             import events as _events

# ── Strategy configuration (envvar overridable) ───────────────────────────────
# ── Strategy configuration (envvar overridable) ───────────────────────────────
STOP_LOSS_PCT     = float(os.getenv("STOP_LOSS_PCT",       "0.20"))  
TRAILING_STOP_PCT = float(os.getenv("TRAILING_STOP_PCT",   "0.25"))  
TRAILING_ACTIVATE = float(os.getenv("TRAILING_ACTIVATE",   "0.30"))  

# 5 Take-Profit Level aus der .env
TP1_PCT           = float(os.getenv("TP1_PCT",             "0.15"))  
TP2_PCT           = float(os.getenv("TP2_PCT",             "0.25"))  
TP3_PCT           = float(os.getenv("TP3_PCT",             "0.50"))  
TP4_PCT           = float(os.getenv("TP4_PCT",             "1.00"))  
TP5_PCT           = float(os.getenv("TP5_PCT",             "2.00"))  

# Wieviel % der ORIGINAL-Position pro Stufe verkauft werden
TP1_SELL_PCT      = float(os.getenv("TP1_SELL_PCT",        "0.50"))  
TP2_SELL_PCT      = float(os.getenv("TP2_SELL_PCT",        "0.25"))  
TP3_SELL_PCT      = float(os.getenv("TP3_SELL_PCT",        "0.15"))  
TP4_SELL_PCT      = float(os.getenv("TP4_SELL_PCT",        "0.10"))  
TP5_SELL_PCT      = float(os.getenv("TP5_SELL_PCT",        "1.00")) # Rest verkaufen

CHECK_INTERVAL    = int(os.getenv("MONITOR_INTERVAL",      "30"))    
MAX_HOLD_HOURS    = float(os.getenv("MAX_HOLD_HOURS",      "24"))
STALE_EXIT_MIN    = float(os.getenv("STALE_EXIT_MIN_LOSS", "0.05"))

_RPC_ENV = os.getenv("SOLANA_RPC_URL", "https://api.mainnet-beta.solana.com")
_RPC_ENDPOINTS = list(dict.fromkeys([
    _RPC_ENV,
    "https://api.mainnet-beta.solana.com",
    "https://solana-rpc.publicnode.com",
    "https://solana.publicnode.com",
]))
_TOKEN_PROGRAMS = [
    "TokenzQdBNbLqP5VEhdkAS6EPFLC1PHnBqCXEpPxuEb",
    "TokenkegQfeZyiNwAJbNbGKPFXCWuBvf9Ss623VQ5DA",
]

SOL_MINT = "So11111111111111111111111111111111111111112"


# ── Price fetching with fallback ──────────────────────────────────────────────

async def _get_price(address: str, dex: DexScreenerAdapter, last_price: float = 0.0) -> float:
    """
    Fetch current token price.
    Primary: DexScreener  →  Fallback: Jupiter price API  →  last known price.
    """
    # Primary: DexScreener
    try:
        token_data = await dex.get_token_data(address)
        if token_data:
            price = float(token_data.get("price_usd", 0))
            if price > 0:
                return price
    except Exception as e:
        logger.debug(f"[MONITOR] DexScreener price fail for {address[:8]}: {e}")

    # Fallback: Jupiter price API
    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(
                f"https://api.jup.ag/price/v2?ids={address}",
                timeout=aiohttp.ClientTimeout(total=8),
            ) as resp:
                if resp.status == 200:
                    data  = await resp.json()
                    price = float((data.get("data") or {}).get(address, {}).get("price", 0))
                    if price > 0:
                        logger.debug(f"[MONITOR] Jupiter price fallback for {address[:8]}: ${price}")
                        return price
    except Exception as e:
        logger.debug(f"[MONITOR] Jupiter price fallback fail for {address[:8]}: {e}")

    # Last resort: last known price (prevents false SL triggers)
    if last_price > 0:
        logger.warning(f"[MONITOR] All price sources failed for {address[:8]} — using last ${last_price:.8f}")
        return last_price

    return 0.0


# ── Position Monitor class ────────────────────────────────────────────────────

class PositionMonitor:

    def __init__(self, state_file: str = "positions.json"):
        self.state_file = state_file
        self.lock       = asyncio.Lock()
        self.executor   = TradeExecutor()
        self.dex        = DexScreenerAdapter()
        self.positions: dict = self._load_positions()

    # ── Persistence ───────────────────────────────────────────────────────────

    def _load_positions(self) -> dict:
        if os.path.exists(self.state_file):
            try:
                with open(self.state_file) as f:
                    return json.load(f)
            except Exception:
                pass
        return {}

    async def _save_positions(self) -> None:
        async with self.lock:
            with open(self.state_file, "w") as f:
                json.dump(self.positions, f, indent=2)

    # ── Add position ──────────────────────────────────────────────────────────

    async def add_position(seplf, address: str, entry_price: float, symbol: str = "UNKNOWN") -> None:
        async with self.lock:
            self.positions[address] = {
                "symbol":          symbol,
                "entry_price":     entry_price,
                "created_at":      time.time(),
                "remaining_pct":   1.0,
                "tp1_hit":         False,
                "tp2_hit":         False,
                "tp3_hit":         False,
                "tp4_hit":         False,  # NEU
                "tp5_hit":         False,  # NEU
                "highest_price":   entry_price,
                "trailing_active": False,
                "last_price":      entry_price,
            }
        await self._save_positions()
        _events.emit(
            "POSITION_ADDED", symbol, address,
            price_usd=entry_price,
            message=f"Position opened @ ${entry_price:.8f} | SL: ${entry_price*(1-STOP_LOSS_PCT):.8f}",
        )
        logger.info(
            f"[MONITOR] ✅ Position opened: {symbol} @ ${entry_price:.8f} | "
            f"SL: -{int(STOP_LOSS_PCT*100)}% | "
            f"TP1: +{int(TP1_PCT*100)}% | TP2: +{int(TP2_PCT*100)}% | TP3: +{int(TP3_PCT*100)}%"
        )

    # ── Wallet sync ───────────────────────────────────────────────────────────

    async def _sync_wallet_positions(self) -> None:
        """Register tokens found in wallet but not tracked, and remove gone ones."""
        # In dry-run mode, skip wallet sync — positions are virtual
        if self.executor.dry_run:
            return

        wallet = str(self.executor.keypair.pubkey()) if self.executor.keypair else None
        if not wallet:
            return

        wallet_mints: dict[str, float] = {}
        for rpc in _RPC_ENDPOINTS:
            rpc_ok = False
            for prog in _TOKEN_PROGRAMS:
                try:
                    async with aiohttp.ClientSession() as session:
                        async with session.post(
                            rpc,
                            json={"jsonrpc": "2.0", "id": 1,
                                  "method": "getTokenAccountsByOwner",
                                  "params": [wallet, {"programId": prog}, {"encoding": "jsonParsed"}]},
                            timeout=aiohttp.ClientTimeout(total=8),
                        ) as resp:
                            data = await resp.json()
                            for acc in data.get("result", {}).get("value", []):
                                info   = acc["account"]["data"]["parsed"]["info"]
                                amount = float(info["tokenAmount"]["uiAmount"] or 0)
                                if amount > 0:
                                    wallet_mints[info["mint"]] = amount
                            rpc_ok = True
                except Exception:
                    continue
            if rpc_ok:
                break   # Got data from this RPC — no need to try others

        added, removed = [], []
        async with self.lock:
            for mint, amount in wallet_mints.items():
                if mint not in self.positions:
                    # Try to get a price
                    token_data = await self.dex.get_token_data(mint)
                    price      = float((token_data or {}).get("price_usd", 0))
                    sym        = (token_data or {}).get("symbol", mint[:8])
                    self.positions[mint] = {
                        "symbol":          sym,
                        "entry_price":     price,
                        "created_at":      time.time(),
                        "remaining_pct":   1.0,
                        "tp1_hit":         False,
                        "tp2_hit":         False,
                        "tp3_hit":         False,
                        "highest_price":   price,
                        "trailing_active": False,
                        "manually_added":  True,
                        "last_price":      price,
                    }
                    added.append(sym)

            for mint in list(self.positions.keys()):
                if mint not in wallet_mints:
                    removed.append(self.positions[mint].get("symbol", mint[:8]))
                    del self.positions[mint]

        if added or removed:
            await self._save_positions()
        if added:
            logger.info(f"[MONITOR] Wallet sync: +{len(added)} token(s): {', '.join(added)}")
        if removed:
            logger.info(f"[MONITOR] Wallet sync: -{len(removed)} token(s) gone: {', '.join(removed)}")

    # ── Monitor loop ──────────────────────────────────────────────────────────

    async def monitor(self) -> None:
        logger.info(
            f"[MONITOR] Started | SL: -{int(STOP_LOSS_PCT*100)}% | "
            f"Trailing: -{int(TRAILING_STOP_PCT*100)}% (activates +{int(TRAILING_ACTIVATE*100)}%) | "
            f"TP1: +{int(TP1_PCT*100)}% | TP2: +{int(TP2_PCT*100)}% | TP3: +{int(TP3_PCT*100)}%"
        )
        _events.emit("BOT_START", message=f"Monitor started | {len(self.positions)} positions loaded")

        await self._sync_wallet_positions()
        cycle = 0
        while True:
            await asyncio.sleep(CHECK_INTERVAL)
            cycle += 1
            if cycle % 5 == 0:
                await self._sync_wallet_positions()
            if not self.positions:
                continue
            logger.info(f"[MONITOR] Checking {len(self.positions)} position(s)...")
            for address, pos in list(self.positions.items()):
                await self._check_position(address, pos)

    # ── Check single position ─────────────────────────────────────────────────

    async def _check_position(self, address: str, pos: dict) -> None:
        symbol      = pos.get("symbol", "UNKNOWN")
        entry_price = float(pos.get("entry_price", 0))
        remaining   = float(pos.get("remaining_pct", 1.0))

        if entry_price == 0 or remaining <= 0:
            return

        try:
            last_price    = float(pos.get("last_price", entry_price))
            current_price = await _get_price(address, self.dex, last_price)
            if current_price == 0:
                logger.warning(f"[MONITOR] {symbol}: price unavailable — skipping check")
                return

            # Update last known price + ATH
            highest_price = float(pos.get("highest_price", entry_price))
            if current_price > highest_price:
                highest_price = current_price
            async with self.lock:
                self.positions[address]["highest_price"] = highest_price
                self.positions[address]["last_price"]    = current_price
            await self._save_positions()

            change_pct    = (current_price - entry_price)  / entry_price
            drop_from_ath = (current_price - highest_price) / highest_price if highest_price > 0 else 0
            age_hours     = (time.time() - pos.get("created_at", time.time())) / 3600

            logger.info(
                f"[MONITOR] {symbol} | "
                f"entry ${entry_price:.8f} | now ${current_price:.8f} | "
                f"P/L {change_pct:+.2%} | ATH drop {drop_from_ath:+.2%} | "
                f"age {age_hours:.1f}h | open {int(remaining*100)}%"
            )

            # ── STOP-LOSS ─────────────────────────────────────────────────────
            if change_pct <= -STOP_LOSS_PCT:
                logger.warning(f"[MONITOR] 🛑 STOP-LOSS {symbol}: {change_pct:.2%}")
                await self._sell_position(
                    symbol, address, current_price, 1.0, change_pct,
                    reason=f"Stop-Loss {change_pct:.2%}", stage="STOP_LOSS",
                    event_type="SELL_STOP_LOSS", full_close=True,
                )
                return

            # ── TRAILING STOP ─────────────────────────────────────────────────
            if change_pct >= TRAILING_ACTIVATE and not pos.get("trailing_active"):
                async with self.lock:
                    self.positions[address]["trailing_active"] = True
                await self._save_positions()
                logger.info(f"[MONITOR] 📈 Trailing stop ACTIVATED for {symbol} (+{change_pct:.1%})")

            if pos.get("trailing_active") and drop_from_ath <= -TRAILING_STOP_PCT:
                logger.warning(f"[MONITOR] 📉 TRAILING STOP {symbol}: ATH drop {drop_from_ath:.2%}")
                await self._sell_position(
                    symbol, address, current_price, 1.0, change_pct,
                    reason=f"Trailing Stop (ATH drop {drop_from_ath:.2%})", stage="TRAILING_STOP",
                    event_type="SELL_TRAILING_STOP", full_close=True,
                )
                return

            # ── TIME EXIT ─────────────────────────────────────────────────────
            if age_hours >= MAX_HOLD_HOURS and change_pct < STALE_EXIT_MIN:
                logger.warning(f"[MONITOR] ⏰ TIME EXIT {symbol}: {age_hours:.1f}h | P/L {change_pct:+.2%}")
                await self._sell_position(
                    symbol, address, current_price, 1.0, change_pct,
                    reason=f"Time exit ({age_hours:.1f}h)", stage="TIME_EXIT",
                    event_type="SELL_TIME_EXIT", full_close=True,
                )
                return

           
            # ── TAKE PROFIT 5 (200%) ──────────────────────────────────────────
            if not pos.get("tp5_hit") and change_pct >= TP5_PCT:
                frac = min(TP5_SELL_PCT / remaining if remaining > 0 else 1.0, 1.0)
                logger.success(f"[MONITOR] 🚀 TP5 +{int(TP5_PCT*100)}% {symbol}")
                await self._sell_position(
                    symbol, address, current_price, frac, change_pct,
                    reason=f"Take-Profit 5 (+{int(TP5_PCT*100)}%)", stage="TP5",
                    event_type="SELL_TP5", full_close=(frac >= 0.99),
                )
                if frac < 0.99:
                    async with self.lock:
                        self.positions[address]["tp5_hit"]       = True
                        self.positions[address]["remaining_pct"] = round(remaining - TP5_SELL_PCT, 4)
                    await self._save_positions()
                return

            # ── TAKE PROFIT 4 (100%) ──────────────────────────────────────────
            if not pos.get("tp4_hit") and change_pct >= TP4_PCT:
                frac = min(TP4_SELL_PCT / remaining if remaining > 0 else 1.0, 1.0)
                logger.success(f"[MONITOR] 💰 TP4 +{int(TP4_PCT*100)}% {symbol}")
                await self._sell_position(
                    symbol, address, current_price, frac, change_pct,
                    reason=f"Take-Profit 4 (+{int(TP4_PCT*100)}%)", stage="TP4",
                    event_type="SELL_TP4", full_close=(frac >= 0.99),
                )
                if frac < 0.99:
                    async with self.lock:
                        self.positions[address]["tp4_hit"]       = True
                        self.positions[address]["remaining_pct"] = round(remaining - TP4_SELL_PCT, 4)
                    await self._save_positions()
                return

            # ── TAKE PROFIT 3 (50%) ───────────────────────────────────────────
            if not pos.get("tp3_hit") and change_pct >= TP3_PCT:
                frac = min(TP3_SELL_PCT / remaining if remaining > 0 else 1.0, 1.0)
                logger.success(f"[MONITOR] 💰 TP3 +{int(TP3_PCT*100)}% {symbol}")
                await self._sell_position(
                    symbol, address, current_price, frac, change_pct,
                    reason=f"Take-Profit 3 (+{int(TP3_PCT*100)}%)", stage="TP3",
                    event_type="SELL_TP3", full_close=(frac >= 0.99),
                )
                if frac < 0.99:
                    async with self.lock:
                        self.positions[address]["tp3_hit"]       = True
                        self.positions[address]["remaining_pct"] = round(remaining - TP3_SELL_PCT, 4)
                    await self._save_positions()
                return

            # ── TAKE PROFIT 2 (25%) ───────────────────────────────────────────
            if not pos.get("tp2_hit") and change_pct >= TP2_PCT:
                frac = min(TP2_SELL_PCT / remaining if remaining > 0 else 1.0, 1.0)
                logger.success(f"[MONITOR] 💚 TP2 +{int(TP2_PCT*100)}% {symbol}")
                await self._sell_position(
                    symbol, address, current_price, frac, change_pct,
                    reason=f"Take-Profit 2 (+{int(TP2_PCT*100)}%)", stage="TP2",
                    event_type="SELL_TP2", full_close=(frac >= 0.99),
                )
                if frac < 0.99:
                    async with self.lock:
                        self.positions[address]["tp2_hit"]       = True
                        self.positions[address]["remaining_pct"] = round(remaining - TP2_SELL_PCT, 4)
                    await self._save_positions()
                return

            # ── TAKE PROFIT 1 (15%) ───────────────────────────────────────────
            if not pos.get("tp1_hit") and change_pct >= TP1_PCT:
                frac = min(TP1_SELL_PCT / remaining if remaining > 0 else 1.0, 1.0)
                logger.success(f"[MONITOR] 💚 TP1 +{int(TP1_PCT*100)}% {symbol}")
                await self._sell_position(
                    symbol, address, current_price, frac, change_pct,
                    reason=f"Take-Profit 1 (+{int(TP1_PCT*100)}%)", stage="TP1",
                    event_type="SELL_TP1", full_close=(frac >= 0.99),
                )
                if frac < 0.99:
                    async with self.lock:
                        self.positions[address]["tp1_hit"]       = True
                        self.positions[address]["remaining_pct"] = round(remaining - TP1_SELL_PCT, 4)
                    await self._save_positions()
                return
        except Exception as e:
            logger.error(f"[MONITOR] Error checking {symbol}: {e}")

    # ── Sell helper ───────────────────────────────────────────────────────────

    async def _sell_position(
        self,
        symbol:       str,
        address:      str,
        current_price: float,
        sell_fraction: float,
        change_pct:   float,
        reason:       str,
        stage:        str,
        event_type:   str,
        full_close:   bool,
    ) -> None:
        pos_size   = float(os.getenv("TRADE_MAX_POSITION_USD", "0.20"))
        pnl_usd    = pos_size * sell_fraction * change_pct
        pnl_pct    = change_pct

        logger.info(f"[MONITOR] 💵 {symbol}: P/L ${pnl_usd:+.4f} ({pnl_pct:+.2%}) | reason: {reason}")

        result = await self.executor.execute_trade(
            token_symbol=symbol,
            token_address=address,
            score=0,
            decision="SELL",
            price=current_price,
            rejection_reason=reason,
            funnel_stage=stage,
            sell_fraction=sell_fraction,
            gates_passed="G1:Data,G2:Safety,G3:Risk,G4:PreFilter,G5:Scoring,G6:Exec,POSITION",
        )

        # Emit a specific strategy event (enriched with P/L)
        _events.emit(
            event_type, symbol, address,
            tx_signature=result.get("tx") if result else None,
            sell_amount_usd=result.get("sell_amount_usd") if result else None,
            price_usd=current_price,
            pnl_usd=pnl_usd,
            pnl_pct=pnl_pct,
            stage=stage,
            message=reason,
        )

        if full_close:
            await self._close_position(address)

    async def _close_position(self, address: str) -> None:
        sym = self.positions.get(address, {}).get("symbol", address[:8])
        async with self.lock:
            self.positions.pop(address, None)
        await self._save_positions()
        _events.emit("POSITION_CLOSED", sym, address, message="Position fully closed")
        logger.info(f"[MONITOR] Position closed: {sym}")
