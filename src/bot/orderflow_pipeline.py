"""
src/bot/orderflow_pipeline.py — Short-Term Order Flow Scalping Pipeline

Entries require several independent confirmations inside one short window
instead of a single whale print plus a 24h trend value.

Gate System:
  G1: Liquidity, fresh data, tight spread
  G2: Whale BUY print
  G3: Persistent order book bid dominance (not a single snapshot)
  G4: Aggressive buy flow + short-term upward momentum
  G5: Risk limits -> Market BUY + protective exit
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

# Short-term scalping parameters
FLOW_WINDOW_SEC   = float(os.getenv("SCALP_FLOW_WINDOW_SEC",   "30"))
MIN_MOMENTUM_PCT  = float(os.getenv("SCALP_MIN_MOMENTUM_PCT",  "0.05"))
MAX_MOMENTUM_PCT  = float(os.getenv("SCALP_MAX_MOMENTUM_PCT",  "1.50"))
MIN_FLOW_RATIO    = float(os.getenv("SCALP_MIN_FLOW_RATIO",    "1.6"))
MIN_PERSISTENCE   = float(os.getenv("SCALP_MIN_PERSISTENCE",   "0.6"))
MIN_BOOK_SAMPLES  = int(os.getenv("SCALP_MIN_BOOK_SAMPLES",    "5"))
MAX_WALL_PULL_PCT = float(os.getenv("SCALP_MAX_WALL_PULL_PCT", "40"))
MAX_SPREAD_BPS    = float(os.getenv("SCALP_MAX_SPREAD_BPS",    "5"))
MIN_BID_DEPTH     = float(os.getenv("SCALP_MIN_BID_DEPTH_USDT", "25000"))
MAX_HOLD_SEC      = float(os.getenv("SCALP_MAX_HOLD_SEC",      "300"))
TRAIL_ACTIVATE    = float(os.getenv("SCALP_TRAIL_ACTIVATE_PCT", "0.8"))
TRAIL_GIVEBACK    = float(os.getenv("SCALP_TRAIL_GIVEBACK_PCT", "0.4"))
REGIME_MAX_DROP   = float(os.getenv("SCALP_REGIME_MAX_DROP_PCT", "-5.0"))
TAKER_FEE_PCT     = float(os.getenv("BINANCE_TAKER_FEE_PCT",   "0.1"))
SLIPPAGE_BPS      = float(os.getenv("SCALP_SLIPPAGE_BPS",      "2"))
ROUND_TRIP_COST   = 2 * TAKER_FEE_PCT + (2 * SLIPPAGE_BPS / 100)

# Pegged pairs cannot move far enough to cover trading costs.
STABLE_ASSETS = {"USDC", "FDUSD", "TUSD", "BUSD", "DAI", "USDP", "EUR", "RLUSD", "USD1", "XUSD"}


# ── Gate functions ────────────────────────────────────────────────────────────

def expected_move_pct(metrics: dict) -> float:
    """Scale the observed short-term range to the hold period (random-walk approximation)."""
    return metrics.get("range_pct", 0.0) * (MAX_HOLD_SEC / FLOW_WINDOW_SEC) ** 0.5

def gate1_liquidity(ticker: dict, metrics: dict) -> tuple[bool, str]:
    symbol = ticker.get("symbol", "")
    if symbol.removesuffix("USDT") in STABLE_ASSETS:
        return False, "Pegged pair"

    vol = ticker.get("volume_24h", 0)
    age = time.time() - ticker.get("updated_at", 0)
    if vol < MIN_VOLUME_24H:
        return False, f"Vol too low: ${vol/1e6:.1f}M < ${MIN_VOLUME_24H/1e6:.0f}M"
    if age > 30:
        return False, f"Stale: {age:.0f}s old"
    if ticker.get("change_24h", 0) < REGIME_MAX_DROP:
        return False, f"Regime: {ticker.get('change_24h', 0):.2f}% 24h"
    if metrics["spread_bps"] > MAX_SPREAD_BPS:
        return False, f"Spread {metrics['spread_bps']:.1f}bps > {MAX_SPREAD_BPS:.1f}"
    if metrics["bid_vol"] < MIN_BID_DEPTH:
        return False, f"Thin book: ${metrics['bid_vol']:,.0f}"

    reachable = expected_move_pct(metrics)
    if reachable < TAKE_PROFIT_PCT + ROUND_TRIP_COST:
        return False, (
            f"Target unreachable: ~{reachable:.2f}% moeglich in {MAX_HOLD_SEC:.0f}s, "
            f"noetig {TAKE_PROFIT_PCT + ROUND_TRIP_COST:.2f}%"
        )
    return True, f"Vol ${vol/1e6:.0f}M | spread {metrics['spread_bps']:.1f}bps | Bewegung ~{reachable:.2f}%"


def gate2_whale_signal(symbol: str, adapter: BinanceOrderFlowAdapter) -> tuple[bool, str]:
    whale_buys = adapter.get_signals(min_type="WHALE_BUY")
    sym_whales = [s for s in whale_buys if s.symbol == symbol]
    if not sym_whales:
        return False, "No whale buy signal"
    best = max(sym_whales, key=lambda s: s.value_usd)
    return True, f"Whale BUY ${best.value_usd:,.0f} ({best.age_sec:.0f}s ago)"


def gate3_book_pressure(metrics: dict) -> tuple[bool, str]:
    """Bid dominance must hold across many snapshots and the wall must not vanish."""
    if metrics["book_samples"] < MIN_BOOK_SAMPLES:
        return False, f"Only {metrics['book_samples']} book samples"
    if metrics["book_persistence"] < MIN_PERSISTENCE:
        return False, f"Imbalance unstable: {metrics['book_persistence']*100:.0f}%"
    if metrics["wall_pull_pct"] > MAX_WALL_PULL_PCT:
        return False, f"Bid wall pulled: -{metrics['wall_pull_pct']:.0f}% (Spoofing-Verdacht)"

    note = f"Book bid-dominant {metrics['book_persistence']*100:.0f}% of {metrics['book_samples']} snaps"
    if metrics.get("absorbed"):
        note += f" | Absorption {metrics['absorption_ratio']:.2f}x"
    return True, note


def gate4_flow_momentum(metrics: dict) -> tuple[bool, str]:
    """Aggressive buyers must dominate, unless sellers are being absorbed at a firm bid."""
    if metrics["trade_count"] < 10:
        return False, f"Too few trades: {metrics['trade_count']}"
    if metrics["flow_ratio"] < MIN_FLOW_RATIO and not metrics.get("absorbed"):
        return False, f"Buy flow {metrics['flow_ratio']:.2f}x < {MIN_FLOW_RATIO:.2f}x"
    if metrics["momentum_pct"] < MIN_MOMENTUM_PCT:
        return False, f"Momentum {metrics['momentum_pct']:+.3f}% < {MIN_MOMENTUM_PCT:.3f}%"
    if metrics["momentum_pct"] > MAX_MOMENTUM_PCT:
        return False, f"Already extended {metrics['momentum_pct']:+.2f}%"
    return True, (
        f"Flow {metrics['flow_ratio']:.2f}x | "
        f"{FLOW_WINDOW_SEC:.0f}s momentum {metrics['momentum_pct']:+.3f}%"
    )


def _save_positions(positions: dict) -> None:
    try:
        with open("positions.json", "w") as file:
            json.dump(positions, file, indent=2)
    except Exception as error:
        logger.error(f"Failed to save positions.json: {error}")


def _close_paper_position(
    symbol: str,
    position: dict,
    exit_price: float,
    reason: str,
    positions: dict,
) -> None:
    entry_price = float(position["entry_price"])
    quantity = float(position["qty"])
    gross_pct = ((exit_price / entry_price) - 1) * 100
    pnl_pct = gross_pct - ROUND_TRIP_COST
    proceeds = quantity * exit_price
    pnl_usdt = float(position["size_usdt"]) * pnl_pct / 100

    positions.pop(symbol, None)
    _save_positions(positions)

    try:
        conn = sqlite3.connect("binance_orderflow.db")
        timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        conn.execute(
            """INSERT INTO trades
               (token_address, symbol, entry_price, position_size, score, decision,
                sell_amount_usd, funnel_stage, gates_passed, timestamp)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (symbol, symbol, exit_price, position["size_usdt"], 0.0,
             f"SELL (DRY-RUN {reason})", proceeds, "PAPER_EXIT", reason, timestamp),
        )
        conn.execute(
            """INSERT INTO bot_events
               (event_type, symbol, address, sell_amount_usd, price_usd, pnl_usd, pnl_pct,
                stage, message, timestamp)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            ("SELL", symbol, symbol, proceeds, exit_price, pnl_usdt, pnl_pct,
             "PAPER_EXIT", reason, timestamp),
        )
        conn.commit()
        conn.close()
    except Exception as error:
        logger.error(f"Paper exit DB save failed: {error}")

    logger.success(
        f"[PAPER] SELL {symbol} | {reason} | exit=${exit_price:.6f} | "
        f"gross {gross_pct:+.2f}% | net PnL=${pnl_usdt:+.2f} ({pnl_pct:+.2f}%)"
    )


async def _paper_position_monitor(adapter: BinanceOrderFlowAdapter, positions: dict) -> None:
    """Close simulated positions on target, stop, trailing give-back or timeout."""
    while True:
        await asyncio.sleep(1)
        for symbol, position in list(positions.items()):
            if not position.get("dry_run"):
                continue

            ticker = adapter.get_ticker(symbol)
            if not ticker:
                continue

            current_price = float(ticker.get("price_usd", 0))
            entry_price = float(position.get("entry_price", 0))
            if current_price <= 0 or entry_price <= 0:
                continue

            peak_price = max(float(position.get("peak_price", entry_price)), current_price)
            position["peak_price"] = peak_price
            gain_pct = (current_price / entry_price - 1) * 100
            peak_gain_pct = (peak_price / entry_price - 1) * 100
            held_sec = time.time() - float(position.get("opened_at", time.time()))

            if gain_pct >= TAKE_PROFIT_PCT:
                _close_paper_position(symbol, position, current_price, "TAKE_PROFIT", positions)
            elif gain_pct <= -STOP_LOSS_PCT:
                _close_paper_position(symbol, position, current_price, "STOP_LOSS", positions)
            elif peak_gain_pct >= TRAIL_ACTIVATE and (peak_gain_pct - gain_pct) >= TRAIL_GIVEBACK:
                _close_paper_position(symbol, position, current_price, "TRAILING_STOP", positions)
            elif held_sec >= MAX_HOLD_SEC:
                _close_paper_position(symbol, position, current_price, "TIME_EXIT", positions)


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

    metrics = adapter.get_flow_metrics(symbol, FLOW_WINDOW_SEC)

    # G1 — silent (too many coins fail here)
    ok, reason = gate1_liquidity(ticker, metrics)
    if not ok:
        return False

    # G2 — silent (fire constantly)
    ok, g2_reason = gate2_whale_signal(symbol, adapter)
    if not ok:
        return False

    # G3 — if we reach here, start logging
    ok, g3_reason = gate3_book_pressure(metrics)
    if not ok:
        logger.info(f"[{symbol}] G2✔ G3✖ {g2_reason} | {g3_reason}")
        return False

    # G4
    ok, g4_reason = gate4_flow_momentum(metrics)
    if not ok:
        logger.info(f"[{symbol}] G2✔ G3✔ G4✖ | {g3_reason} | {g4_reason}")
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

    # Place market buy using the live WebSocket price (no REST lookup in the hot path)
    order = place_market_buy(symbol, POSITION_SIZE, price)
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
        "peak_price":  exec_price,
        "order_id":    order.get("orderId", ""),
        "dry_run":     DRY_RUN,
    }
    positions[symbol] = pos_data

    _save_positions(positions)

    # DB logging
    try:
        conn = sqlite3.connect("binance_orderflow.db")
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
    logger.info("Binance Short-Term Order Flow Scalper")
    logger.info(f"DRY_RUN: {DRY_RUN} | Size: ${POSITION_SIZE} USDT/trade")
    logger.info(f"Whale threshold: ${float(os.getenv('WHALE_THRESHOLD_USDT', '50000')):,.0f} | "
                f"Imbalance: {float(os.getenv('IMBALANCE_RATIO', '1.5')):.1f}x")
    logger.info(f"SL: -{STOP_LOSS_PCT}% | TP: +{TAKE_PROFIT_PCT}% | "
                f"Trail: {TRAIL_ACTIVATE}%/{TRAIL_GIVEBACK}% | Max hold: {MAX_HOLD_SEC:.0f}s")
    logger.info(f"Flow window: {FLOW_WINDOW_SEC:.0f}s | Min flow {MIN_FLOW_RATIO}x | "
                f"Min momentum {MIN_MOMENTUM_PCT}% | Max spread {MAX_SPREAD_BPS}bps")
    logger.info(f"Assumed round-trip cost: {ROUND_TRIP_COST:.3f}% — TP must exceed this")
    logger.info("=" * 65)

    if TAKE_PROFIT_PCT <= ROUND_TRIP_COST:
        logger.warning(
            f"TP {TAKE_PROFIT_PCT}% is not above cost {ROUND_TRIP_COST:.3f}% — "
            "every winning trade would still lose money."
        )

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
    if DRY_RUN:
        asyncio.create_task(_paper_position_monitor(adapter, positions))

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

