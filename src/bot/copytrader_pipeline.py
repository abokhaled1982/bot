"""
src/bot/copytrader_pipeline.py — Hyperliquid Copy-Trader Pipeline

Receives signals from HyperliquidCopyTrader (a tracked pro opened/closed
a position on Hyperliquid) and executes the corresponding trade on Binance
Spot after verifying market quality.

Signal flow:
  Hyperliquid Trader opens BTC Long
    → COPY_OPEN_LONG signal
    → Verify BTCUSDT on Binance (liquid? spread OK? not already in position?)
    → Market BUY on Binance + OCO exit
  Hyperliquid Trader closes BTC Long
    → COPY_CLOSE_LONG signal
    → Market SELL on Binance (close our position)
"""
from __future__ import annotations

import asyncio
import json
import os
import sqlite3
import time
from datetime import datetime
from typing import Optional

from loguru import logger
from src.adapters.binance_orderflow import BinanceOrderFlowAdapter
from src.adapters.hyperliquid_copytrader import (
    HyperliquidCopyTrader, CopySignal,
)
from src.execution.binance_executor import (
    place_market_buy, place_oco_sell, get_account_balance,
    STOP_LOSS_PCT, TAKE_PROFIT_PCT,
)

# ── Config ────────────────────────────────────────────────────────────────────
DRY_RUN       = os.getenv("DRY_RUN", "True").lower() == "true"
MAX_POSITIONS = int(os.getenv("BINANCE_MAX_POSITIONS", "10"))
POSITION_SIZE = float(os.getenv("BINANCE_POSITION_SIZE_USDT", "10.0"))
MIN_VOL_24H   = float(os.getenv("BN_MIN_VOLUME_24H", "5000000"))
MAX_SPREAD    = float(os.getenv("SCALP_MAX_SPREAD_BPS", "5"))
MAX_HOLD_SEC  = float(os.getenv("SCALP_MAX_HOLD_SEC", "300"))
TRAIL_ACT     = float(os.getenv("SCALP_TRAIL_ACTIVATE_PCT", "0.8"))
TRAIL_GIVE    = float(os.getenv("SCALP_TRAIL_GIVEBACK_PCT", "0.4"))
TAKER_FEE     = float(os.getenv("BINANCE_TAKER_FEE_PCT", "0.1"))
SLIPPAGE_BPS  = float(os.getenv("SCALP_SLIPPAGE_BPS", "2"))
ROUND_TRIP    = 2 * TAKER_FEE + (2 * SLIPPAGE_BPS / 100)

DB_PATH = "binance_orderflow.db"
HISTORY_PATH = os.getenv("COPY_HISTORY_FILE", "copy_history.json")
CLOSE_REQUEST_PATH = os.getenv("COPY_CLOSE_REQUEST_FILE", "close_requests.json")
MAX_HISTORY = 500


# ── Checks ────────────────────────────────────────────────────────────────────

def check_binance_market(
    symbol: str, bn_adapter: BinanceOrderFlowAdapter,
) -> tuple[bool, str]:
    """Verify the coin is tradeable on Binance Spot."""
    ticker = bn_adapter.get_ticker(symbol)
    if not ticker:
        return False, f"{symbol} not on Binance Spot"

    vol = ticker.get("volume_24h", 0)
    if vol < MIN_VOL_24H:
        return False, f"Low vol ${vol/1e6:.1f}M"

    age = time.time() - ticker.get("updated_at", 0)
    if age > 30:
        return False, f"Stale {age:.0f}s"

    book = bn_adapter.get_book(symbol)
    if book:
        spread = book.get("spread_bps", float("inf"))
        if spread > MAX_SPREAD:
            return False, f"Spread {spread:.1f}bps"

    return True, f"OK (vol ${vol/1e6:.0f}M)"



def _save_positions(positions: dict) -> None:
    try:
        with open("positions.json", "w") as f:
            json.dump(positions, f, indent=2)
    except Exception as e:
        logger.error(f"Failed to save positions.json: {e}")


def _append_history(entry: dict) -> None:
    """Append a closed copy-trade so the dashboard can show per-trader history."""
    try:
        history = []
        if os.path.exists(HISTORY_PATH):
            with open(HISTORY_PATH, encoding="utf-8") as f:
                history = json.load(f)
        history.append(entry)
        with open(HISTORY_PATH, "w", encoding="utf-8") as f:
            json.dump(history[-MAX_HISTORY:], f, indent=2)
    except Exception as e:
        logger.error(f"Failed to append {HISTORY_PATH}: {e}")


def _close_paper_position(
    symbol: str, position: dict, exit_price: float,
    reason: str, positions: dict,
) -> None:
    entry_price = float(position["entry_price"])
    gross_pct = ((exit_price / entry_price) - 1) * 100
    pnl_pct = gross_pct - ROUND_TRIP
    pnl_usdt = float(position["size_usdt"]) * pnl_pct / 100
    qty = float(position.get("qty", 0))
    proceeds = qty * exit_price

    positions.pop(symbol, None)
    _save_positions(positions)

    _append_history({
        "trader":      position.get("hl_trader_full", ""),
        "trader_short": position.get("hl_trader", ""),
        "symbol":      symbol,
        "hl_coin":     position.get("hl_coin", ""),
        "entry_price": entry_price,
        "exit_price":  exit_price,
        "size_usdt":   float(position["size_usdt"]),
        "qty":         qty,
        "pnl_usdt":    pnl_usdt,
        "pnl_pct":     pnl_pct,
        "reason":      reason,
        "opened_at":   float(position.get("opened_at", 0)),
        "closed_at":   time.time(),
        "dry_run":     bool(position.get("dry_run", DRY_RUN)),
    })

    try:
        conn = sqlite3.connect(DB_PATH)
        ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        conn.execute(
            """INSERT INTO trades
               (token_address, symbol, entry_price, position_size, score,
                decision, sell_amount_usd, funnel_stage, gates_passed,
                timestamp)
               VALUES (?,?,?,?,?,?,?,?,?,?)""",
            (symbol, symbol, exit_price, position["size_usdt"], 0.0,
             f"SELL (DRY-RUN {reason})", proceeds,
             "COPY_EXIT", reason, ts),
        )
        conn.execute(
            """INSERT INTO bot_events
               (event_type, symbol, address, sell_amount_usd, price_usd,
                pnl_usd, pnl_pct, stage, message, timestamp)
               VALUES (?,?,?,?,?,?,?,?,?,?)""",
            ("SELL", symbol, symbol, proceeds, exit_price,
             pnl_usdt, pnl_pct, "COPY_EXIT", reason, ts),
        )
        conn.commit()
        conn.close()
    except Exception as e:
        logger.error(f"Paper exit DB error: {e}")

    logger.success(
        f"[COPY] SELL {symbol} | {reason} | exit=${exit_price:.4f} | "
        f"net PnL=${pnl_usdt:+.2f} ({pnl_pct:+.2f}%)"
    )


async def _paper_monitor(
    bn_adapter: BinanceOrderFlowAdapter, positions: dict,
) -> None:
    """Monitor paper positions for TP/SL/Trail/Time exit."""
    while True:
        await asyncio.sleep(1)
        for symbol, pos in list(positions.items()):
            if not pos.get("dry_run") or pos.get("source") != "COPY":
                continue

            ticker = bn_adapter.get_ticker(symbol)
            if not ticker:
                continue

            price = float(ticker.get("price_usd", 0))
            entry = float(pos.get("entry_price", 0))
            if price <= 0 or entry <= 0:
                continue

            peak = max(float(pos.get("peak_price", entry)), price)
            pos["peak_price"] = peak
            gain = (price / entry - 1) * 100
            peak_gain = (peak / entry - 1) * 100
            held = time.time() - float(pos.get("opened_at", time.time()))

            if gain >= TAKE_PROFIT_PCT:
                _close_paper_position(
                    symbol, pos, price, "TAKE_PROFIT", positions,
                )
            elif gain <= -STOP_LOSS_PCT:
                _close_paper_position(
                    symbol, pos, price, "STOP_LOSS", positions,
                )
            elif (peak_gain >= TRAIL_ACT
                  and (peak_gain - gain) >= TRAIL_GIVE):
                _close_paper_position(
                    symbol, pos, price, "TRAILING_STOP", positions,
                )
            elif held >= MAX_HOLD_SEC:
                _close_paper_position(
                    symbol, pos, price, "TIME_EXIT", positions,
                )


async def _close_request_loop(
    bn_adapter: BinanceOrderFlowAdapter, positions: dict,
) -> None:
    """Execute close requests the dashboard drops as JSON (cross-process control)."""
    while True:
        await asyncio.sleep(2)
        if not os.path.exists(CLOSE_REQUEST_PATH):
            continue
        try:
            with open(CLOSE_REQUEST_PATH, encoding="utf-8") as f:
                request = json.load(f)
            os.remove(CLOSE_REQUEST_PATH)
        except Exception as e:
            logger.error(f"Close request read error: {e}")
            continue

        symbols = request.get("symbols") or []
        reason = str(request.get("reason", "MANUAL"))[:40]
        for symbol in symbols:
            pos = positions.get(symbol)
            if not pos:
                continue
            ticker = bn_adapter.get_ticker(symbol)
            price = float(ticker.get("price_usd", 0)) if ticker else 0.0
            if price <= 0:
                logger.warning(f"[COPY] Close request {symbol}: no price, skipped")
                continue
            if pos.get("dry_run"):
                _close_paper_position(symbol, pos, price, reason, positions)
            else:
                logger.warning(
                    f"[COPY] Close request {symbol} ignored — LIVE positions are "
                    f"managed by their OCO order. Close it manually on Binance."
                )



# ── Signal handler ────────────────────────────────────────────────────────────

async def handle_copy_signal(
    sig: CopySignal,
    bn_adapter: BinanceOrderFlowAdapter,
    positions: dict,
    hl_adapter: Optional[HyperliquidCopyTrader] = None,
) -> bool:
    """Process one copy signal — open or close on Binance."""
    symbol = sig.symbol

    # ── CLOSE signal: sell existing position ──────────────────────────────────
    if sig.signal == "COPY_CLOSE_LONG":
        if symbol not in positions:
            return False
        pos = positions[symbol]
        ticker = bn_adapter.get_ticker(symbol)
        price = float(ticker.get("price_usd", 0)) if ticker else 0
        if price <= 0:
            price = float(pos.get("entry_price", 0))
        _close_paper_position(
            symbol, pos, price,
            f"TRADER_CLOSED ({sig.trader_short})", positions,
        )
        return True

    # ── DECREASE signal: partial close ────────────────────────────────────────
    if sig.signal == "COPY_DECREASE":
        if symbol in positions:
            ticker = bn_adapter.get_ticker(symbol)
            price = float(ticker.get("price_usd", 0)) if ticker else 0
            if price > 0:
                _close_paper_position(
                    symbol, positions[symbol], price,
                    f"TRADER_DECREASED ({sig.trader_short})", positions,
                )
                return True
        return False

    # ── OPEN/INCREASE: buy on Binance ─────────────────────────────────────────
    if sig.signal not in ("COPY_OPEN_LONG", "COPY_INCREASE"):
        logger.debug(f"[COPY] Ignoring {sig.signal} {symbol} (short/info)")
        return False

    if symbol in positions:
        logger.debug(f"[COPY] {symbol} already in positions, skip")
        return False

    if len(positions) >= MAX_POSITIONS:
        logger.warning(f"[COPY] Max positions {MAX_POSITIONS} reached")
        return False

    # Verify on Binance
    ok, reason = check_binance_market(symbol, bn_adapter)
    if not ok:
        logger.info(
            f"[COPY] ✖ {symbol} | {sig.trader_short} opened {sig.coin} "
            f"but Binance check failed: {reason}"
        )
        return False

    ticker = bn_adapter.get_ticker(symbol)
    price = float(ticker.get("price_usd", 0)) if ticker else 0
    if price <= 0:
        logger.warning(f"[COPY] No price for {symbol}")
        return False

    # All checks passed — execute!
    size_usdt = POSITION_SIZE
    if hl_adapter is not None:
        override = hl_adapter.get_copy_size(sig.trader)
        if override and override > 0:
            size_usdt = override
    logger.success(
        f"[COPY] ✅ COPY {sig.coin} | {sig.trader_short} | "
        f"HL: ${sig.size_usd:,.0f} {sig.leverage:.0f}x | "
        f"BN: ${size_usdt} @ ${price:.4f}"
    )

    order = place_market_buy(symbol, size_usdt, price)
    if not order:
        logger.error(f"[COPY] Order failed for {symbol}")
        return False

    exec_price = float(order.get("price", price))
    exec_qty = float(order.get("executedQty", 0))

    if exec_qty > 0:
        place_oco_sell(symbol, exec_qty, exec_price)

    pos_data = {
        "symbol":         symbol,
        "entry_price":    exec_price,
        "qty":            exec_qty,
        "size_usdt":      size_usdt,
        "opened_at":      time.time(),
        "peak_price":     exec_price,
        "order_id":       order.get("orderId", ""),
        "dry_run":        DRY_RUN,
        "source":         "COPY",
        "hl_trader":      sig.trader_short,
        "hl_trader_full": sig.trader,
        "hl_coin":        sig.coin,
        "hl_size_usd":    sig.size_usd,
        "hl_leverage":    sig.leverage,
    }
    positions[symbol] = pos_data
    _save_positions(positions)

    # DB logging
    try:
        conn = sqlite3.connect(DB_PATH)
        ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        conn.execute(
            """INSERT INTO trades
               (token_address, symbol, entry_price, position_size, score,
                decision, buy_amount_usd, funnel_stage, gates_passed,
                timestamp)
               VALUES (?,?,?,?,?,?,?,?,?,?)""",
            (symbol, symbol, exec_price, size_usdt, 95.0,
             f"BUY COPY ({'DRY-RUN' if DRY_RUN else 'LIVE'})",
             size_usdt, "COPY_EXECUTION",
             f"Trader:{sig.trader}", ts),
        )
        conn.execute(
            """INSERT INTO bot_events
               (event_type, symbol, address, buy_amount_usd, price_usd,
                stage, message, timestamp)
               VALUES (?,?,?,?,?,?,?,?)""",
            ("BUY", symbol, symbol, size_usdt, exec_price,
             "COPY_EXECUTION",
             f"Copy {sig.trader_short} {sig.coin} "
             f"${sig.size_usd:,.0f} {sig.leverage:.0f}x",
             ts),
        )
        conn.commit()
        conn.close()
    except Exception as e:
        logger.error(f"DB save error: {e}")

    return True



# ── Main loop ─────────────────────────────────────────────────────────────────

async def main_loop() -> None:
    """Copy-Trader main loop: HL signals → Binance execution."""
    logger.info("=" * 65)
    logger.info("Hyperliquid Copy-Trader → Binance Spot")
    logger.info(f"DRY_RUN: {DRY_RUN} | Size: ${POSITION_SIZE}/trade")
    logger.info(f"SL: -{STOP_LOSS_PCT}% | TP: +{TAKE_PROFIT_PCT}%")
    logger.info(f"Trail: {TRAIL_ACT}%/{TRAIL_GIVE}% | "
                f"Max hold: {MAX_HOLD_SEC:.0f}s")
    logger.info(f"Round-trip cost: {ROUND_TRIP:.3f}%")
    logger.info("=" * 65)

    if not DRY_RUN:
        bal = get_account_balance("USDT")
        logger.info(f"💰 USDT Balance: ${bal:.2f}")
        if bal < POSITION_SIZE:
            logger.error(
                f"Insufficient USDT: ${bal:.2f} < ${POSITION_SIZE}"
            )
            return

    # Initialize adapters
    hl_adapter = HyperliquidCopyTrader()
    bn_adapter = BinanceOrderFlowAdapter()

    # Load existing positions
    positions: dict = {}
    if os.path.exists("positions.json"):
        try:
            with open("positions.json") as f:
                raw = json.load(f)
            # Drop leftovers from the retired order-flow strategy — they have no
            # trader attribution and would block MAX_POSITIONS forever.
            positions = {s: p for s, p in raw.items() if p.get("source") == "COPY"}
            dropped = len(raw) - len(positions)
            logger.info(f"Loaded {len(positions)} copy positions")
            if dropped:
                logger.warning(f"Discarded {dropped} legacy non-copy position(s) from positions.json")
                _save_positions(positions)
        except Exception:
            pass

    # Start adapters
    asyncio.create_task(hl_adapter.start())
    asyncio.create_task(hl_adapter.cleanup_loop())
    asyncio.create_task(bn_adapter.start())
    asyncio.create_task(bn_adapter.cleanup_loop())
    asyncio.create_task(_close_request_loop(bn_adapter, positions))
    if DRY_RUN:
        asyncio.create_task(_paper_monitor(bn_adapter, positions))

    # Wait for streams to warm up
    logger.info("[COPY] Warming up streams (12s)...")
    await asyncio.sleep(12)
    logger.info("[COPY] ⚡ Listening for trader signals...")

    async def _status() -> None:
        count = 0
        while True:
            await asyncio.sleep(15)
            count += 1
            hl_st = hl_adapter.status()
            copied = len(hl_adapter.get_active_wallets())
            logger.info(
                f"── #{count} | Kopiert:{copied} Trader "
                f"(von {hl_st['tracked_traders']} beobachtet) | "
                f"Meine Positionen:{len(positions)}/{MAX_POSITIONS} ──"
            )
            if not positions:
                continue
            for symbol, pos in positions.items():
                ticker = bn_adapter.get_ticker(symbol)
                price = float(ticker.get("price_usd", 0)) if ticker else 0.0
                entry = float(pos.get("entry_price", 0))
                size = float(pos.get("size_usdt", 0))
                held = time.time() - float(pos.get("opened_at", time.time()))
                if price > 0 and entry > 0:
                    gain = (price / entry - 1) * 100 - ROUND_TRIP
                    pnl = size * gain / 100
                    mark = "🟢" if pnl >= 0 else "🔴"
                    logger.info(
                        f"   {mark} {symbol} | {pos.get('hl_trader', '?')} | "
                        f"${size:.0f} | entry ${entry:.4f} → ${price:.4f} | "
                        f"PnL ${pnl:+.2f} ({gain:+.2f}%) | {held/60:.1f}min"
                    )
                else:
                    logger.info(
                        f"   ⚪️ {symbol} | {pos.get('hl_trader', '?')} | "
                        f"${size:.0f} | entry ${entry:.4f} | kein Preis | {held/60:.1f}min"
                    )

    asyncio.create_task(_status())

    # ── Event loop: react to HL signals ───────────────────────────────────────
    while True:
        if os.path.exists("STOP_BOT"):
            logger.warning("STOP_BOT detected — stopping.")
            break

        sig = await hl_adapter.signal_queue.get()
        await handle_copy_signal(sig, bn_adapter, positions, hl_adapter)
