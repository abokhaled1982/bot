"""
src/bot/binance_pipeline.py — Binance Crypto Trading Pipeline (G1–G6)

Data available from mini-ticker (real-time, ~1s):
  • change_24h   — 24h price change %
  • volume_24h   — 24h quote volume in USDT
  • high_24h     — 24h high price
  • low_24h      — 24h low price
  • price_usd    — current price
  • change_1m    — calculated from rolling price history (after ~1min)
  • change_5m    — calculated from rolling price history (after ~5min)
"""
from __future__ import annotations

import asyncio
import json
import os
import sqlite3
import time
from datetime import datetime

from loguru import logger
from src.adapters.binance_stream import BinanceStreamAdapter

# ── Config ────────────────────────────────────────────────────────────────────
DRY_RUN          = os.getenv("DRY_RUN", "True").lower() == "true"
MAX_POSITIONS    = int(os.getenv("BINANCE_MAX_POSITIONS", "10"))
POSITION_SIZE    = float(os.getenv("BINANCE_POSITION_SIZE_USDT", "10.0"))
STOP_LOSS_PCT    = float(os.getenv("BINANCE_STOP_LOSS_PCT", "5.0"))
TAKE_PROFIT_PCT  = float(os.getenv("BINANCE_TAKE_PROFIT_PCT", "15.0"))

# G1: Liquidity gate
G1_MIN_VOLUME    = float(os.getenv("BN_MIN_VOLUME_24H", "5000000"))   # $5M minimum
G1_MAX_DATA_AGE  = 30  # seconds

# G2: Noise/crash filter
G2_MIN_24H       = float(os.getenv("BN_G2_MIN_24H", "-15.0"))   # block crashes
G2_MAX_VOLATILITY = float(os.getenv("BN_G2_MAX_VOL", "50.0"))   # max High/Low range %

# G3: Trend filter — coin must be going UP on 24h basis
G3_MIN_TREND     = float(os.getenv("BN_G3_MIN_TREND", "1.0"))   # minimum +1% 24h

# G4: Sweet-spot momentum — not too weak, not an extreme pump
G4_MIN_24H       = float(os.getenv("BN_G4_MIN", "2.0"))         # minimum +2% 24h
G4_MAX_24H       = float(os.getenv("BN_G4_MAX", "30.0"))        # maximum +30% 24h (avoid tops)

# G5: Fusion score
G5_BUY_THRESHOLD = float(os.getenv("BN_BUY_SCORE", "40.0"))


# ── G1: Market Data Validation ────────────────────────────────────────────────

def gate1_market_data(ticker: dict) -> tuple[bool, str]:
    """Ensure valid, liquid, fresh data."""
    if not ticker:
        return False, "No ticker data"
    if ticker.get("price_usd", 0) <= 0:
        return False, "Invalid price"
    vol = ticker.get("volume_24h", 0)
    if vol < G1_MIN_VOLUME:
        return False, f"Vol too low: ${vol/1e6:.1f}M < ${G1_MIN_VOLUME/1e6:.0f}M"
    age = time.time() - ticker.get("updated_at", 0)
    if age > G1_MAX_DATA_AGE:
        return False, f"Stale data: {age:.0f}s old"
    return True, "OK"


# ── G2: Noise / Crash Filter ─────────────────────────────────────────────────

def gate2_noise_filter(ticker: dict) -> tuple[bool, str]:
    """Block crashes and extreme volatility."""
    ch24 = ticker.get("change_24h", 0)
    high = ticker.get("high_24h", 0)
    low  = ticker.get("low_24h",  1)

    if ch24 < G2_MIN_24H:
        return False, f"Crash: {ch24:.1f}% in 24h (min {G2_MIN_24H}%)"
    if low > 0 and (high - low) / low * 100 > G2_MAX_VOLATILITY:
        return False, f"Extreme volatility: {(high-low)/low*100:.0f}% H/L range"
    return True, "OK"


# ── G3: Trend Confirmation ────────────────────────────────────────────────────

def gate3_trend(ticker: dict) -> tuple[bool, str]:
    """Coin must be in a positive 24h trend."""
    ch24 = ticker.get("change_24h", 0)
    if ch24 < G3_MIN_TREND:
        return False, f"No uptrend: {ch24:+.2f}% 24h (min +{G3_MIN_TREND}%)"
    return True, f"Uptrend: {ch24:+.2f}% 24h"


# ── G4: Momentum Sweet Spot ───────────────────────────────────────────────────

def gate4_momentum(ticker: dict) -> tuple[bool, str]:
    """Coin must be gaining, but not at FOMO-top levels."""
    ch24 = ticker.get("change_24h", 0)

    if ch24 < G4_MIN_24H:
        return False, f"Momentum too weak: {ch24:+.2f}% (min +{G4_MIN_24H}%)"
    if ch24 > G4_MAX_24H:
        return False, f"Extreme pump — avoid FOMO top: {ch24:+.2f}% (max +{G4_MAX_24H}%)"
    return True, f"Momentum OK: {ch24:+.2f}% 24h"


# ── G5: Fusion Score (0–100) ──────────────────────────────────────────────────

def gate5_fusion_score(ticker: dict) -> tuple[bool, str, float]:
    """
    Score based on data we ACTUALLY have from the mini-ticker:
      • 24h momentum strength   (50%)
      • Volume tier             (50%)
    Threshold: G5_BUY_THRESHOLD (default 40)
    """
    ch24 = ticker.get("change_24h", 0)
    vol  = ticker.get("volume_24h",  0)

    # ── Momentum score (0–50 pts) ─────────────────────────────────────────
    # Map +2% → 0 pts, +15% → 50 pts (sweet spot), diminishing above
    momentum_score = min(max((ch24 - G4_MIN_24H) / (G4_MAX_24H - G4_MIN_24H) * 50, 0), 50)

    # ── Volume tier score (0–50 pts) ─────────────────────────────────────
    if   vol >= 500_000_000: vol_score = 50   # BTC/ETH tier
    elif vol >= 100_000_000: vol_score = 45
    elif vol >= 50_000_000:  vol_score = 38
    elif vol >= 20_000_000:  vol_score = 30
    elif vol >= 10_000_000:  vol_score = 22
    elif vol >= 5_000_000:   vol_score = 15
    else:                    vol_score = 5

    score = momentum_score + vol_score

    if   score >= 65: confidence = "HIGH"
    elif score >= 50: confidence = "MEDIUM"
    elif score >= G5_BUY_THRESHOLD: confidence = "LOW"
    else:             confidence = "SKIP"

    buy = score >= G5_BUY_THRESHOLD
    action = "BUY" if buy else "SKIP"
    return buy, f"{action} score={score:.1f} conf={confidence}", score


# ── Evaluate single candidate through all gates ───────────────────────────────

async def evaluate_candidate(ticker: dict, positions: dict) -> bool:
    symbol = ticker.get("symbol", "?")
    price  = ticker.get("price_usd", 0)
    gates  = []

    # G1
    ok, reason = gate1_market_data(ticker)
    if not ok:
        logger.debug(f"[{symbol}] G1 FAIL: {reason}")
        return False
    gates.append("G1")

    # G2
    ok, reason = gate2_noise_filter(ticker)
    if not ok:
        logger.info(f"[{symbol}] G2 FAIL: {reason}")
        return False
    gates.append("G2")

    # G3
    ok, reason = gate3_trend(ticker)
    if not ok:
        logger.info(f"[{symbol}] G3 FAIL: {reason}")
        return False
    gates.append("G3")

    # G4
    ok, reason = gate4_momentum(ticker)
    if not ok:
        logger.info(f"[{symbol}] G4 FAIL: {reason}")
        return False
    gates.append("G4")

    # G5
    ok, reason, score = gate5_fusion_score(ticker)
    if not ok:
        logger.info(f"[{symbol}] G5 FAIL: {reason}")
        return False
    gates.append("G5")

    # G6: Position limits + duplicate check
    if symbol in positions:
        logger.debug(f"[{symbol}] G6 FAIL: already in portfolio")
        return False
    if len(positions) >= MAX_POSITIONS:
        logger.warning(f"[{symbol}] G6 FAIL: max {MAX_POSITIONS} positions reached")
        return False
    gates.append("G6")

    # ── BUY ───────────────────────────────────────────────────────────────────
    mode    = "DRY-RUN" if DRY_RUN else "LIVE"
    ch24    = ticker.get("change_24h", 0)
    vol_m   = ticker.get("volume_24h", 0) / 1e6

    logger.success(
        f"[{symbol}] ✅ {mode} BUY @ ${price:.6f} | "
        f"24h:{ch24:+.2f}% | Vol:${vol_m:.1f}M | "
        f"Score:{score:.1f} | Gates:{'+'.join(gates)}"
    )

    pos_data = {
        "symbol":      symbol,
        "entry_price": price,
        "size_usdt":   POSITION_SIZE,
        "opened_at":   time.time(),
        "change_24h":  ch24,
        "score":       round(score, 1),
    }
    positions[symbol] = pos_data

    # Save to positions.json (Positions Tab)
    try:
        with open("positions.json", "w") as f:
            json.dump(positions, f, indent=2)
    except Exception as e:
        logger.error(f"Failed to save positions.json: {e}")

    # Save to SQLite (History Tab)
    try:
        conn = sqlite3.connect("memecoin_bot.db")
        conn.execute(
            """INSERT INTO trades
               (token_address, symbol, entry_price, position_size, score, decision,
                buy_amount_usd, funnel_stage, gates_passed, timestamp)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (symbol, symbol, price, POSITION_SIZE, score, f"BUY ({mode})",
             POSITION_SIZE, "G6_EXECUTION", "+".join(gates),
             datetime.now().strftime("%Y-%m-%d %H:%M:%S"))
        )
        conn.execute(
            """INSERT INTO bot_events
               (event_type, symbol, address, buy_amount_usd, price_usd, stage, message, timestamp)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
            ("BUY", symbol, symbol, POSITION_SIZE, price,
             "G6_EXECUTION",
             f"24h:{ch24:+.2f}% Score:{score:.1f}",
             datetime.now().strftime("%Y-%m-%d %H:%M:%S"))
        )
        conn.commit()
        conn.close()
        logger.debug(f"[{symbol}] Saved to DB ✓")
    except Exception as e:
        logger.error(f"Failed to save to database: {e}")

    return True


# ── Main loop ─────────────────────────────────────────────────────────────────

async def main_loop() -> None:
    logger.info("=" * 65)
    logger.info("Binance Crypto Bot — Multi-Gate Discovery (G1–G6)")
    logger.info(f"DRY_RUN: {DRY_RUN} | Size: ${POSITION_SIZE} USDT | Max: {MAX_POSITIONS} pos")
    logger.info(f"G4: +{G4_MIN_24H}% to +{G4_MAX_24H}% 24h | G5 threshold: {G5_BUY_THRESHOLD} pts")
    logger.info("=" * 65)

    stream = BinanceStreamAdapter()

    # Load existing positions
    positions = {}
    if os.path.exists("positions.json"):
        try:
            with open("positions.json") as f:
                positions = json.load(f)
            logger.info(f"Loaded {len(positions)} existing positions from positions.json")
        except Exception:
            pass

    asyncio.create_task(stream.start())
    asyncio.create_task(stream.cleanup_loop())

    logger.info("[BINANCE] Waiting 5s for WebSocket to connect...")
    await asyncio.sleep(5)

    scan = 0
    while True:
        if os.path.exists("STOP_BOT"):
            logger.warning("STOP_BOT detected — stopping.")
            break

        scan += 1
        st    = stream.status()
        cands = stream.get_candidates(limit=30)

        logger.info(
            f"── Scan #{scan} | WS: {'OK' if st['connected'] else 'DOWN'} | "
            f"Symbols: {st['tracked_symbols']} | "
            f"Candidates: {len(cands)} | "
            f"Positions: {len(positions)}/{MAX_POSITIONS} ──"
        )

        bought = 0
        for ticker in cands:
            if ticker["symbol"] in positions:
                continue
            bought += await evaluate_candidate(ticker, positions)

        if bought:
            logger.success(f"── Scan #{scan} → {bought} new position(s) opened ──")

        logger.info(
            f"── Scan #{scan} done | Bought: {bought} | "
            f"Positions: {len(positions)}/{MAX_POSITIONS} | next scan in 5s ──"
        )
        await asyncio.sleep(5)
