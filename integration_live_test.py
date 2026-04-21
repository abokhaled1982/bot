#!/usr/bin/env python3
"""
integration_live_test.py

End-to-end integration test using REAL coin addresses.

  Section A  - Pipeline gating (dry_run=True inside executor so no real buys)
               5 coins: 3 expected to potentially pass, 2 expected to fail.

  Section B  - Real buy + immediate sell ($1.00 each, DRY_RUN read from .env)
               Uses BONK (highest liquidity) → WIF as fallback.
               Tests the same flow as the Dashboard Trade tab (direct execute).

Usage:
    python integration_live_test.py
"""
from __future__ import annotations

import asyncio
import os
import sqlite3
import sys
import time

from dotenv import load_dotenv

load_dotenv()
sys.path.insert(0, os.path.dirname(__file__))

from loguru import logger

from src.adapters.dexscreener  import DexScreenerAdapter
from src.adapters.safety       import SafetyAdapter
from src.adapters.solana_chain import SolanaAdapter
from src.analysis.fusion       import SignalFusion
from src.execution.executor    import TradeExecutor
from src.execution.monitor     import PositionMonitor
from src.bot.pipeline          import evaluate_token, get_btc_change
import src.bot.pipeline as _pipeline

# ── Config ────────────────────────────────────────────────────────────────────

TRADE_AMOUNT_USD = 1.00   # $1 per trade — fee-efficient, low risk

# ── Coin lists ────────────────────────────────────────────────────────────────

# 3 active memecoins that should potentially pass all gates
# 2 old / low-momentum coins expected to fail at G4/G5
PIPELINE_COINS = [
    # ── Expected to potentially PASS ─────────────────────────────────────────
    {
        "symbol":  "FARTCOIN",
        "address": "9BB6NFEcjBCtnNLFko2FqVQBq8HHM13kCyYcdQbgpump",
        "source":  "established",
        "note":    "popular 2024/25 meme, should have liquidity",
    },
    {
        "symbol":  "PENGU",
        "address": "2zMMhcVQEXDtdE6vsFS7S7D5oUodfJHE8vd1gnBouauv",
        "source":  "established",
        "note":    "Pudgy Penguins spin-off, large cap, active",
    },
    {
        "symbol":  "CHILLGUY",
        "address": "Df6yfrKC8kZE3KNkrHERKzAetSxbrWeniQfyJY4Jpump",
        "source":  "established",
        "note":    "trending 2024 meme, good liquidity",
    },
    # ── Expected to FAIL (old/dead, no momentum) ──────────────────────────────
    {
        "symbol":  "SMOG",
        "address": "FS66v5XYtJAFo14LiPz5HT93EUMAHmYipCfQhLpU4ss8",
        "source":  "established",
        "note":    "old 2024 coin — expect G4/G5 fail (no spike/momentum)",
    },
    {
        "symbol":  "TREMP",
        "address": "FU1q8vJpZNUrmqsciSjp8bAKKidGsLmouB8CBdf8TKQv",
        "source":  "established",
        "note":    "political meme — expect G4/G5 fail (no spike)",
    },
]

# High-liq coins for Section B real tx test
BUY_SELL_COINS = [
    {
        "symbol":  "BONK",
        "address": "DezXAZ8z7PnrnRJjz3wXBoRgixCa6xjnB7YaB1pPB263",
        "note":    "highest-liq Solana meme — best for Jupiter routing",
    },
    {
        "symbol":  "WIF",
        "address": "EKpQGSJtjMFqKZ9KQanSqYXRcF8fBopzLHYxdM65zcjm",
        "note":    "fallback if BONK route fails",
    },
]

SEP  = "=" * 70
SEP2 = "─" * 60


# ── Helpers ───────────────────────────────────────────────────────────────────

def _last_trade_row(address: str, db: str = "memecoin_bot.db") -> dict:
    try:
        conn = sqlite3.connect(db)
        cur  = conn.cursor()
        cur.execute(
            "SELECT decision, funnel_stage, gates_passed, rejection_reason "
            "FROM trades WHERE token_address=? ORDER BY rowid DESC LIMIT 1",
            (address,),
        )
        row = cur.fetchone()
        conn.close()
        if row:
            return {
                "decision":  row[0] or "?",
                "stage":     row[1] or "?",
                "gates":     row[2] or "",
                "reason":    (row[3] or "")[:80],
            }
    except Exception:
        pass
    return {}


# ── Section A: Pipeline gating ────────────────────────────────────────────────

async def section_a_pipeline():
    print(f"\n{SEP}")
    print("SECTION A — PIPELINE GATING TEST")
    print("  (executor dry_run=True inside this section — no real buys)")
    print(SEP)

    dex     = DexScreenerAdapter()
    safety  = SafetyAdapter()
    chain   = SolanaAdapter()
    fusion  = SignalFusion()
    monitor = PositionMonitor()

    # Fresh executor with dry_run forced True regardless of .env
    executor          = TradeExecutor()
    executor.dry_run  = True

    # Clear session so all coins are considered fresh
    _pipeline.BOUGHT_THIS_SESSION = set()

    btc_chg = await get_btc_change()
    print(f"\n  BTC 1h trend: {btc_chg:+.3f}%\n")

    results: list[dict] = []

    for coin in PIPELINE_COINS:
        symbol  = coin["symbol"]
        address = coin["address"]
        note    = coin.get("note", "")

        print(f"\n{SEP2}")
        print(f"  TOKEN : {symbol}")
        print(f"  ADDR  : {address[:20]}...{address[-8:]}")
        print(f"  NOTE  : {note}")

        t_start = time.time()
        passed  = await evaluate_token(
            coin, dex, safety, chain, fusion,
            executor, monitor, btc_chg,
            is_migration=False,
        )
        elapsed = time.time() - t_start

        db_row = _last_trade_row(address)
        stage  = db_row.get("stage", "?")
        gates  = db_row.get("gates", "")
        reason = db_row.get("reason", "")

        marker = "✅  PASS → BUY queued" if passed else f"❌  FAIL at [{stage}]"
        print(f"  RESULT: {marker}  ({elapsed:.1f}s)")
        print(f"  GATES : {gates or '—'}")
        if not passed:
            print(f"  REASON: {reason[:70]}")

        results.append({
            "symbol": symbol,
            "passed": passed,
            "stage":  stage,
            "gates":  gates,
            "reason": reason,
        })

        await asyncio.sleep(1.2)   # polite pacing between DexScreener calls

    # Summary
    passed_list = [r for r in results if r["passed"]]
    failed_list = [r for r in results if not r["passed"]]

    print(f"\n{SEP}")
    print("SECTION A SUMMARY")
    print(SEP)
    print(f"  PASSED ({len(passed_list)}/5):")
    if passed_list:
        for r in passed_list:
            print(f"    ✅  {r['symbol']:12}  gates={r['gates']}")
    else:
        print("    (none)")
    print(f"\n  FAILED ({len(failed_list)}/5):")
    for r in failed_list:
        print(f"    ❌  {r['symbol']:12}  stopped_at={r['stage']}")
        print(f"         {r['reason'][:65]}")
    print()
    return results


# ── Section B: Real buy + immediate sell ─────────────────────────────────────

async def section_b_buy_sell():
    print(f"\n{SEP}")
    print(f"SECTION B — REAL BUY + SELL TEST (${TRADE_AMOUNT_USD:.2f} per trade)")
    print("  Same flow as Dashboard Trade tab — direct executor call")
    print(SEP)

    executor  = TradeExecutor()
    dex       = DexScreenerAdapter()
    live_mode = not executor.dry_run

    mode_tag = "🔴 LIVE (on-chain)" if live_mode else "🟡 SIMULATED (DRY_RUN=True in .env)"
    print(f"\n  Mode: {mode_tag}")
    print(f"  Trade amount: ${TRADE_AMOUNT_USD:.2f}\n")

    traded = False
    for coin in BUY_SELL_COINS:
        symbol  = coin["symbol"]
        address = coin["address"]
        note    = coin.get("note", "")

        print(f"{SEP2}")
        print(f"  Token: {symbol}  ({note})")

        # ── Fetch current market data ─────────────────────────────────────────
        token_data = await dex.get_token_data(address)
        if not token_data:
            print(f"  ⚠️  No DexScreener data for {symbol} — skipping")
            continue

        price = float(token_data.get("price_usd", 0))
        liq   = float(token_data.get("liquidity_usd", 0))
        ch_1h = float(token_data.get("change_1h", 0))
        print(f"  Price : ${price:.8f}")
        print(f"  Liq   : ${liq:,.0f}")
        print(f"  1h chg: {ch_1h:+.2f}%")

        if liq < 20_000:
            print(f"  ⚠️  Liquidity too low (${liq:,.0f}) — skipping, trying next coin")
            continue

        # ── BUY ───────────────────────────────────────────────────────────────
        print(f"\n  ▶  BUY ${TRADE_AMOUNT_USD:.2f} of {symbol}...")
        t_buy_start = time.time()
        buy_res = await executor.execute_trade(
            token_symbol     = symbol,
            token_address    = address,
            score            = 99.0,
            decision         = "BUY",
            price            = price,
            rejection_reason = "[INTEGRATION_TEST] Direct buy/sell validation",
            funnel_stage     = "MANUAL",
            confidence       = "HIGH",
            liquidity_usd    = liq,
            gates_passed     = "MANUAL",
            position_size_override = TRADE_AMOUNT_USD,
        )
        t_buy = time.time() - t_buy_start

        print(f"  BUY result  : {buy_res}")
        print(f"  BUY time    : {t_buy:.1f}s")

        if buy_res.get("status") != "success":
            print(f"  ❌ BUY failed — trying next coin")
            continue

        is_dry  = buy_res.get("dry_run", False)
        buy_usd = buy_res.get("buy_amount_usd") or TRADE_AMOUNT_USD
        buy_via = "Ultra API" if t_buy < 3.0 else "Legacy Swap API"

        if not is_dry:
            tx_buy = buy_res.get("tx", "?")
            print(f"  ✅ BUY confirmed on-chain via {buy_via}")
            print(f"     TX: https://solscan.io/tx/{tx_buy}")
            print(f"  ⏳ Waiting 8s for token balance to settle on-chain...")
            await asyncio.sleep(8)
        else:
            print(f"  ✅ BUY simulated (dry_run=True)")

        # ── SELL ──────────────────────────────────────────────────────────────
        print(f"\n  ▶  SELL all {symbol} (fraction=1.0)...")

        # Refresh price for sell
        token_data2 = await dex.get_token_data(address)
        price2      = float((token_data2 or token_data).get("price_usd", price))

        t_sell_start = time.time()
        sell_res = await executor.execute_trade(
            token_symbol     = symbol,
            token_address    = address,
            score            = 99.0,
            decision         = "SELL",
            price            = price2,
            rejection_reason = "[INTEGRATION_TEST] Immediate sell after buy",
            funnel_stage     = "MANUAL",
            confidence       = "HIGH",
            liquidity_usd    = liq,
            gates_passed     = "MANUAL",
            sell_fraction    = 1.0,
        )
        t_sell = time.time() - t_sell_start

        print(f"  SELL result : {sell_res}")
        print(f"  SELL time   : {t_sell:.1f}s")

        if sell_res.get("status") == "success":
            sell_via = "Ultra API" if t_sell < 3.0 else "Legacy Swap API"
            if not is_dry:
                tx_sell  = sell_res.get("tx", "?")
                sell_usd = sell_res.get("sell_amount_usd") or 0.0
                pnl      = sell_usd - buy_usd
                print(f"  ✅ SELL confirmed on-chain via {sell_via}")
                print(f"     TX: https://solscan.io/tx/{tx_sell}")
                print(f"\n  ══ TRADE SUMMARY ═══════════════════════")
                print(f"     Bought : ${buy_usd:.4f}  ({buy_via}, {t_buy:.1f}s)")
                print(f"     Sold   : ${sell_usd:.4f}  ({sell_via}, {t_sell:.1f}s)")
                print(f"     Net P&L: ${pnl:+.4f} ({pnl/buy_usd*100:+.1f}%)")
                print(f"     Total  : {t_buy + t_sell + 8:.1f}s (incl 8s settle)")
                print(f"  ═════════════════════════════════════════\n")
            else:
                print(f"  ✅ SELL simulated  (DRY_RUN mode — no on-chain tokens to sell)\n")
        else:
            err = sell_res.get("message", "unknown error")
            print(f"  ❌ SELL failed: {err}\n")

        traded = True
        break   # one complete buy+sell cycle is enough

    if not traded:
        print("  ⚠️  No coin had sufficient liquidity for buy/sell test")


# ── Entry Point ───────────────────────────────────────────────────────────────

async def main():
    print(SEP)
    print("  MEMECOIN BOT — END-TO-END INTEGRATION LIVE TEST")
    print(f"  Date: {time.strftime('%Y-%m-%d %H:%M:%S UTC', time.gmtime())}")
    print(f"  TRADE_AMOUNT_USD: ${TRADE_AMOUNT_USD:.2f}")
    print(f"  DRY_RUN (from .env): {os.getenv('DRY_RUN', 'TRUE')}")
    print(SEP)

    await section_a_pipeline()

    # Give background G3 threads time to finish before Section B
    # so they don't exhaust RPC rate limits during buy/sell confirmation
    print("\n  ⏳ Waiting 30s for background RPC threads to settle...\n")
    await asyncio.sleep(30)

    await section_b_buy_sell()

    print(SEP)
    print("  INTEGRATION TEST COMPLETE")
    print(SEP)


if __name__ == "__main__":
    # Reduce loguru noise — show only INFO+ and suppress debug spam
    logger.remove()
    logger.add(sys.stdout, level="INFO", colorize=True,
               format="<level>{level: <8}</level> | {message}")
    asyncio.run(main())
