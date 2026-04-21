"""
src/bot/orderflow_pipeline.py — Order Flow Trading Pipeline

Uses Level 2 data (order book + whale trades) to detect high-probability
entry points and execute fast orders via Binance REST API.

Gate System:
  G1: Liquidity — 24h volume > $5M
  G2: Whale BUY signal in last 30s
  G3: Order book LONG imbalance (bids >> asks)
  G4: Trend positive (24h change > 0%)
  G5: No existing position → Execute Market BUY + OCO
"""
from __future__ import annotations

import asyncio
import json
import os
import sqlite3
import time
from datetime import datetime

from loguru import logger
from src.adapters.binance_orderflow import BinanceOrderFlowAdapter
from src.execution.binance_executor import (
    place_market_buy, place_oco_sell, get_account_balance,
    STOP_LOSS_PCT, TAKE_PROFIT_PCT
)

# ── Config ────────────────────────────────────────────────────────────────────
DRY_RUN        = os.getenv("DRY_RUN",                    "True").lower() == "true"
MAX_POSITIONS  = int(os.getenv("BINANCE_MAX_POSITIONS",  "10"))
POSITION_SIZE  = float(os.getenv("BINANCE_POSITION_SIZE_USDT", "10.0"))
MIN_VOLUME_24H = float(os.getenv("BN_MIN_VOLUME_24H",   "5000000"))
SCAN_INTERVAL  = float(os.getenv("SCAN_INTERVAL_SEC",   "3.0"))    # scan every 3s


# ── Gate functions ────────────────────────────────────────────────────────────

def gate1_liquidity(ticker: dict) -> tuple[bool, str]:
    vol = ticker.get("volume_24h", 0)
    age = time.time() - ticker.get("updated_at", 0)
    if vol < MIN_VOLUME_24H:
        return False, f"Vol too low: ${vol/1e6:.1f}M < ${MIN_VOLUME_24H/1e6:.0f}M"
    if age > 30:
        return False, f"Stale: {age:.0f}s old"
    return True, f"Vol OK: ${vol/1e6:.0f}M"


def gate2_whale_signal(symbol: str, adapter: BinanceOrderFlowAdapter) -> tuple[bool, str]:
    whale_buys = adapter.get_signals(min_type="WHALE_BUY")
    sym_whales = [s for s in whale_buys if s.symbol == symbol]
    if not sym_whales:
        return False, "No whale buy signal"
    best = max(sym_whales, key=lambda s: s.value_usd)
    return True, f"Whale BUY ${best.value_usd:,.0f} ({best.age_sec:.0f}s ago)"


def gate3_book_imbalance(symbol: str, adapter: BinanceOrderFlowAdapter) -> tuple[bool, str]:
    book_longs = adapter.get_signals(min_type="BOOK_LONG")
    sym_longs  = [s for s in book_longs if s.symbol == symbol]
    if not sym_longs:
        return False, "No book imbalance"
    best = max(sym_longs, key=lambda s: s.ratio)
    return True, f"Book LONG ratio={best.ratio:.2f}x ({best.age_sec:.0f}s ago)"


def gate4_trend(ticker: dict) -> tuple[bool, str]:
    ch24 = ticker.get("change_24h", 0)
    if ch24 < 0:
        return False, f"Downtrend: {ch24:.2f}% 24h"
    return True, f"Uptrend: {ch24:+.2f}% 24h"


# ── Single candidate evaluation ───────────────────────────────────────────────

async def evaluate_candidate(
    ticker: dict,
    adapter: BinanceOrderFlowAdapter,
    positions: dict,
) -> bool:
    symbol = ticker.get("symbol", "?")
    price  = ticker.get("price_usd", 0)

    if symbol in positions:
        return False

    # G1 — silent (too many coins fail here)
    ok, reason = gate1_liquidity(ticker)
    if not ok:
        return False

    # G2 — silent (fire constantly)
    ok, g2_reason = gate2_whale_signal(symbol, adapter)
    if not ok:
        return False

    # G3 — if we reach here, start logging
    ok, g3_reason = gate3_book_imbalance(symbol, adapter)
    if not ok:
        logger.info(f"[{symbol}] 🐳+G2✔ G3✖ Whale OK ({g2_reason}) | Book FAIL (no imbalance)")
        return False

    # G4
    ok, g4_reason = gate4_trend(ticker)
    if not ok:
        logger.info(
            f"[{symbol}] 🐳+G2✔ 📗+G3✔ G4✖ | {g2_reason} | {g3_reason} | Trend: {g4_reason}"
        )
        return False

    # G5
    if len(positions) >= MAX_POSITIONS:
        logger.warning(f"[{symbol}] G5✖ Max {MAX_POSITIONS} positions reached")
        return False

    # ✅ All gates passed
    gates = ["G1", "G2", "G3", "G4", "G5"]
    logger.success(
        f"[{symbol}] ✅ ALL GATES | Price:${price:.4f} | {g2_reason} | {g3_reason} | {g4_reason}"
    )

    # Place market buy
    order = place_market_buy(symbol, POSITION_SIZE)
    if not order:
        logger.error(f"[{symbol}] Order placement failed")
        return False

    exec_price = float(order.get("price", price))
    exec_qty   = float(order.get("executedQty", 0))

    # Place OCO (Stop-Loss + Take-Profit)
    if exec_qty > 0:
        place_oco_sell(symbol, exec_qty, exec_price)

    # Save position
    pos_data = {
        "symbol":      symbol,
        "entry_price": exec_price,
        "qty":         exec_qty,
        "size_usdt":   POSITION_SIZE,
        "opened_at":   time.time(),
        "order_id":    order.get("orderId", ""),
        "dry_run":     DRY_RUN,
    }
    positions[symbol] = pos_data

    try:
        with open("positions.json", "w") as f:
            json.dump(positions, f, indent=2)
    except Exception as e:
        logger.error(f"Failed to save positions.json: {e}")

    # DB logging
    try:
        conn = sqlite3.connect("memecoin_bot.db")
        conn.execute(
            """INSERT INTO trades
               (token_address, symbol, entry_price, position_size, score, decision,
                buy_amount_usd, funnel_stage, gates_passed, timestamp)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (symbol, symbol, exec_price, POSITION_SIZE, 90.0,
             f"BUY ({'DRY-RUN' if DRY_RUN else 'LIVE'})",
             POSITION_SIZE, "ORDERFLOW_EXECUTION", "+".join(gates),
             datetime.now().strftime("%Y-%m-%d %H:%M:%S"))
        )
        conn.execute(
            """INSERT INTO bot_events
               (event_type, symbol, address, buy_amount_usd, price_usd, stage, message, timestamp)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
            ("BUY", symbol, symbol, POSITION_SIZE, exec_price,
             "ORDERFLOW_EXECUTION",
             f"{g2_reason} | {g3_reason}",
             datetime.now().strftime("%Y-%m-%d %H:%M:%S"))
        )
        conn.commit()
        conn.close()
        logger.debug(f"[{symbol}] Saved to DB ✓")
    except Exception as e:
        logger.error(f"DB save failed: {e}")

    return True


# ── Main loop ─────────────────────────────────────────────────────────────────

async def main_loop() -> None:
    logger.info("=" * 65)
    logger.info("Binance Order Flow Bot — Event-Driven Whale + Book Imbalance")
    logger.info(f"DRY_RUN: {DRY_RUN} | Size: ${POSITION_SIZE} USDT/trade")
    logger.info(f"Whale threshold: ${float(os.getenv('WHALE_THRESHOLD_USDT', '50000')):,.0f} | "
                f"Imbalance: {float(os.getenv('IMBALANCE_RATIO', '1.5')):.1f}x")
    logger.info(f"SL: -{STOP_LOSS_PCT}% | TP: +{TAKE_PROFIT_PCT}%")
    logger.info("⚡ Mode: EVENT-DRIVEN (reacts within milliseconds of whale trade)")
    logger.info("=" * 65)

    if not DRY_RUN:
        bal = get_account_balance("USDT")
        logger.info(f"💰 USDT Balance: ${bal:.2f}")
        if bal < POSITION_SIZE:
            logger.error(f"Insufficient USDT! Need ${POSITION_SIZE}, have ${bal:.2f}")
            return

    adapter = BinanceOrderFlowAdapter()

    positions: dict = {}
    if os.path.exists("positions.json"):
        try:
            with open("positions.json") as f:
                positions = json.load(f)
            logger.info(f"Loaded {len(positions)} existing positions")
        except Exception:
            pass

    asyncio.create_task(adapter.start())
    asyncio.create_task(adapter.cleanup_loop())

    logger.info("[ORDERFLOW] Warming up streams (10s)...")
    await asyncio.sleep(10)
    logger.info("[ORDERFLOW] ⚡ Listening for whale trades...")

    async def _status_loop() -> None:
        count = 0
        while True:
            await asyncio.sleep(15)
            count += 1
            st = adapter.status()
            logger.info(
                f"── Status #{count} | "
                f"Tickers:{st['tracked_symbols']} | "
                f"Pairs:{st['subscribed_pairs']} | "
                f"Signals:{st['fresh_signals']} | "
                f"Positions:{len(positions)}/{MAX_POSITIONS} ──"
            )

    asyncio.create_task(_status_loop())

    # ── Event-driven: react instantly on every whale signal ───────────────────
    while True:
        if os.path.exists("STOP_BOT"):
            logger.warning("STOP_BOT detected — stopping.")
            break

        sig = await adapter.signal_queue.get()

        if sig.signal != "WHALE_BUY":
            continue

        ticker = adapter.get_ticker(sig.symbol)
        if not ticker:
            continue

        await evaluate_candidate(ticker, adapter, positions)

