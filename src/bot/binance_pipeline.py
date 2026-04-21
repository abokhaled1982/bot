"""
src/bot/binance_pipeline.py — Binance Crypto Trading Pipeline (G1–G6)
"""
from __future__ import annotations

import asyncio
import os
import time

from loguru import logger
from src.adapters.binance_stream import BinanceStreamAdapter

# ── Config ────────────────────────────────────────────────────────────────────
DRY_RUN          = os.getenv("DRY_RUN", "True").lower() == "true"
MAX_POSITIONS    = int(os.getenv("BINANCE_MAX_POSITIONS", "10"))
POSITION_SIZE    = float(os.getenv("BINANCE_POSITION_SIZE_USDT", "10.0"))
STOP_LOSS_PCT    = float(os.getenv("BINANCE_STOP_LOSS_PCT", "5.0"))
TAKE_PROFIT_PCT  = float(os.getenv("BINANCE_TAKE_PROFIT_PCT", "15.0"))

# G2: Noise filter thresholds
G2_MAX_5M_SPIKE  = float(os.getenv("BN_G2_MAX_5M", "15.0"))   # block extreme pumps
G2_MIN_24H       = float(os.getenv("BN_G2_MIN_24H", "-20.0"))  # block crashes

# G3: Technical thresholds (used once kline data is available)
G3_RSI_MIN       = float(os.getenv("BN_RSI_MIN", "35.0"))
G3_RSI_MAX       = float(os.getenv("BN_RSI_MAX", "68.0"))

# G4: Momentum filter
G4_MOMENTUM_MIN  = float(os.getenv("BN_MOMENTUM_MIN_PCT", "0.3"))
G4_MOMENTUM_MAX  = float(os.getenv("BN_MOMENTUM_MAX_PCT", "8.0"))

# G5: Fusion score threshold
G5_BUY_THRESHOLD = float(os.getenv("BN_BUY_SCORE", "60.0"))


# ── G1: Market Data Validation ────────────────────────────────────────────────

def gate1_market_data(ticker: dict) -> tuple[bool, str]:
    """Check that we have valid, fresh market data from Binance."""
    if not ticker:
        return False, "No ticker data"
    if ticker.get("price_usd", 0) <= 0:
        return False, "Invalid price"
    if ticker.get("volume_24h", 0) < 1_000_000:
        return False, f"Vol too low: ${ticker['volume_24h']/1e6:.1f}M < $1M"
    age = time.time() - ticker.get("updated_at", 0)
    if age > 30:
        return False, f"Stale data: {age:.0f}s old"
    return True, "OK"


# ── G2: Noise Filter ─────────────────────────────────────────────────────────

def gate2_noise_filter(ticker: dict) -> tuple[bool, str]:
    """Block extreme pumps, crashes, and illiquid coins."""
    ch5m = ticker.get("change_5m", 0)
    ch24 = ticker.get("change_24h", 0)
    high = ticker.get("high_24h", 0)
    low  = ticker.get("low_24h", 1)

    if ch5m > G2_MAX_5M_SPIKE:
        return False, f"Extreme pump: +{ch5m:.1f}% in 5m (max {G2_MAX_5M_SPIKE}%)"
    if ch24 < G2_MIN_24H:
        return False, f"Crash: {ch24:.1f}% 24h (min {G2_MIN_24H}%)"
    if low > 0 and (high - low) / low * 100 > 50:
        return False, f"Extreme volatility: {(high-low)/low*100:.0f}% range"
    return True, "OK"


# ── G3: Technical Confirmation (RSI placeholder) ──────────────────────────────

def gate3_technical(ticker: dict) -> tuple[bool, str]:
    """
    RSI / MACD confirmation.
    Phase 1: use 24h change as RSI proxy until kline REST is integrated.
    """
    ch24  = ticker.get("change_24h", 0)
    spike = ticker.get("volume_spike", 1.0)

    # Proxy RSI: if market is up moderately + volume spike → bullish signal
    if ch24 < -10:
        return False, f"Bearish 24h: {ch24:.1f}% (RSI proxy too low)"
    if spike < 1.5:
        return False, f"Volume spike too weak: {spike:.1f}x (min 1.5x)"
    return True, f"OK (24h:{ch24:+.1f}% spike:{spike:.1f}x)"


# ── G4: Momentum & Market Structure ──────────────────────────────────────────

def gate4_momentum(ticker: dict) -> tuple[bool, str]:
    """Ensure positive but not extreme momentum (avoid FOMO entries)."""
    ch5m = ticker.get("change_5m", 0)
    ch1m = ticker.get("change_1m", 0)

    # Use 24h/4.8 as proxy if no 5m history yet
    if ch5m == 0.0:
        ch5m = ticker.get("change_24h", 0) / 4.8

    if ch5m < G4_MOMENTUM_MIN:
        return False, f"Insufficient momentum: {ch5m:+.2f}% (min +{G4_MOMENTUM_MIN}%)"
    if ch5m > G4_MOMENTUM_MAX:
        return False, f"Momentum too extreme: {ch5m:+.2f}% (max {G4_MOMENTUM_MAX}%)"
    return True, f"OK (5m:{ch5m:+.2f}% 1m:{ch1m:+.2f}%)"


# ── G5: Binance Fusion Score ─────────────────────────────────────────────────

def gate5_fusion_score(ticker: dict) -> tuple[bool, str, float]:
    """Calculate composite score and decide BUY/HOLD/SKIP."""
    ch5m  = ticker.get("change_5m", 0) or ticker.get("change_24h", 0) / 4.8
    ch24  = ticker.get("change_24h", 0)
    spike = ticker.get("volume_spike", 1.0)
    vol   = ticker.get("volume_24h", 0)

    # Momentum component (30%)
    momentum_score = min(max((ch5m - G4_MOMENTUM_MIN) / G4_MOMENTUM_MAX * 100, 0), 100)

    # Volume spike component (30%)
    spike_score = min((spike - 1.0) / 4.0 * 100, 100)

    # 24h trend component (20%)
    trend_score = min(max((ch24 + 20) / 40 * 100, 0), 100)

    # Volume tier component (20%)
    if   vol >= 100_000_000: vol_score = 100
    elif vol >= 50_000_000:  vol_score = 80
    elif vol >= 10_000_000:  vol_score = 60
    elif vol >= 5_000_000:   vol_score = 40
    else:                    vol_score = 20

    score = (
        momentum_score * 0.30 +
        spike_score    * 0.30 +
        trend_score    * 0.20 +
        vol_score      * 0.20
    )

    if   score >= 70: decision = "BUY",    "HIGH"
    elif score >= 55: decision = "BUY",    "MEDIUM"
    elif score >= 40: decision = "HOLD",   "LOW"
    else:             decision = "SKIP",   "LOW"

    action, confidence = decision
    return action == "BUY" and score >= G5_BUY_THRESHOLD, f"{action} score={score:.1f} conf={confidence}", score


# ── Evaluate single candidate ─────────────────────────────────────────────────

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
    ok, reason = gate3_technical(ticker)
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

    # G6: Position limits
    if len(positions) >= MAX_POSITIONS:
        logger.warning(f"[{symbol}] G6 FAIL: max {MAX_POSITIONS} positions reached")
        return False
    gates.append("G6")

    # ── BUY ──────────────────────────────────────────────────────────────────
    if DRY_RUN:
        logger.success(
            f"[{symbol}] ✅ DRY-RUN BUY @ ${price:.6f} | "
            f"Score:{score:.1f} | Gates:{'+'.join(gates)} | "
            f"Size:${POSITION_SIZE} USDT"
        )
        positions[symbol] = {
            "symbol":      symbol,
            "entry_price": price,
            "size_usdt":   POSITION_SIZE,
            "opened_at":   time.time(),
        }
        return True
    else:
        logger.info(f"[{symbol}] LIVE BUY (not yet implemented — enable DRY_RUN)")
        return False


# ── Main loop ─────────────────────────────────────────────────────────────────

async def main_loop() -> None:
    logger.info("=" * 65)
    logger.info("Binance Crypto Bot — Multi-Gate Discovery")
    logger.info(f"DRY_RUN: {DRY_RUN} | Position: ${POSITION_SIZE} USDT")
    logger.info(f"Max Positions: {MAX_POSITIONS} | SL: {STOP_LOSS_PCT}% | TP: {TAKE_PROFIT_PCT}%")
    logger.info("=" * 65)

    stream    = BinanceStreamAdapter()
    positions = {}

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
        st   = stream.status()
        cands = stream.get_candidates(limit=20)

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

        logger.info(
            f"── Scan #{scan} done | Bought: {bought} | "
            f"Positions: {len(positions)}/{MAX_POSITIONS} | sleeping 30s ──"
        )
        await asyncio.sleep(30)
