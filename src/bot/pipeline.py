"""
src/bot/pipeline.py — Token Evaluation Pipeline & Main Loop

Extracts all business logic from main.py so that main.py stays ≤100 lines.
"""
from __future__ import annotations

import asyncio
import json
import os
import time as _time

import aiohttp
from loguru import logger

from src.adapters.dexscreener  import DexScreenerAdapter
from src.adapters.pumpfun      import PumpFunAdapter
from src.adapters.helius_stream import HeliusStreamAdapter, get_onchain_token_data
from src.adapters.established  import EstablishedAdapter
from src.adapters.raydium      import RaydiumAdapter
from src.adapters.safety       import SafetyAdapter
from src.adapters.solana_chain import SolanaAdapter
from src.analysis.fusion       import SignalFusion
from src.execution.executor    import TradeExecutor
from src.execution.monitor     import PositionMonitor
from src.bot.filters           import (
    BLOCKED_TOKENS,
    calculate_hype_score,
    get_risk_flags,
    get_token_age_hours,
    pre_buy_filter,
    pre_buy_filter_migration,
)
from src.utils.notify import send_whatsapp_update

# ── Migration watchlist state ─────────────────────────────────────────────────
MIGRATION_WATCHLIST:  dict  = {}
WATCHLIST_MAX_AGE_SEC: int  = 600   # 10 minutes
WATCHLIST_MAX_RETRIES: int  = 20    # 20 × 30 s = 10 min

# ── Per-session duplicate guard ───────────────────────────────────────────────
BOUGHT_THIS_SESSION: set = set()


# ── Helpers ───────────────────────────────────────────────────────────────────

def _save_watchlist() -> None:
    wl_data = {
        addr: {
            "symbol":               wl["token"].get("symbol", "?"),
            "source":               wl["token"].get("source", ""),
            "added_at":             wl["added_at"],
            "retries":              wl["retries"],
            "pumpfun_detected_at":  wl["token"].get("pumpfun_detected_at", 0),
        }
        for addr, wl in MIGRATION_WATCHLIST.items()
    }
    with open("watchlist.json", "w") as f:
        json.dump(wl_data, f, indent=2)


async def get_btc_change() -> float:
    """Return approximate BTC 1-hour % change (24h change ÷ 24)."""
    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(
                "https://api.coingecko.com/api/v3/simple/price"
                "?ids=bitcoin&vs_currencies=usd&include_24hr_change=true",
                timeout=aiohttp.ClientTimeout(total=8),
            ) as r:
                if r.status == 200:
                    data = await r.json()
                    change = float(data["bitcoin"].get("usd_24h_change", 0))
                    return round(change / 24, 4)
    except Exception as e:
        logger.warning(f"BTC price error: {e}")
    return 0.0


# ── Token evaluation pipeline ─────────────────────────────────────────────────

async def evaluate_token(
    token:        dict,
    dex:          DexScreenerAdapter,
    safety:       SafetyAdapter,
    chain:        SolanaAdapter,
    fusion:       SignalFusion,
    executor:     TradeExecutor,
    monitor:      PositionMonitor,
    btc_change:   float,
    is_migration: bool = False,
) -> bool:
    """
    Run a single token through the 6-gate evaluation pipeline.
    Returns True if position was opened, False otherwise.
    """
    global BOUGHT_THIS_SESSION

    address = token.get("address")
    if not address or address in BLOCKED_TOKENS:
        return False
    if address in BOUGHT_THIS_SESSION or address in monitor.positions:
        return False

    symbol  = token.get("symbol") or "UNKNOWN"
    source  = token.get("source", "unknown")
    src_tag = "MIGRATION" if is_migration else source.upper()
    gates:  list[str] = []

    # ── GATE 1: DexScreener data ──────────────────────────────────────────────
    token_data = await dex.get_token_data(address)
    if not token_data:
        if source == "helius_raydium":
            logger.info(f"[{symbol}] [HELIUS] G1: DexScreener not indexed → on-chain fallback")
            token_data = await get_onchain_token_data(address)
            if not token_data:
                logger.warning(f"[{symbol}] [HELIUS] G1 FAIL: no on-chain data")
                return False
            # Silently add to watchlist for DexScreener retry later
            if address not in MIGRATION_WATCHLIST:
                MIGRATION_WATCHLIST[address] = {
                    "token": token, "added_at": _time.time(), "retries": 0,
                }
                _save_watchlist()
        elif is_migration and address not in MIGRATION_WATCHLIST:
            MIGRATION_WATCHLIST[address] = {
                "token": token, "added_at": _time.time(), "retries": 0,
            }
            logger.info(
                f"[{symbol}] [{src_tag}] G1 WAIT → watchlist "
                f"({len(MIGRATION_WATCHLIST)} pending)"
            )
            _save_watchlist()
            return False
        else:
            retries = MIGRATION_WATCHLIST.get(address, {}).get("retries", 0)
            reason  = (
                f"Migration not indexed after {retries} retries"
                if is_migration else "No DexScreener data"
            )
            logger.info(f"[{symbol}] [{src_tag}] G1 FAIL: {reason}")
            await executor.execute_trade(
                symbol, address, 0, "SKIP",
                price=0, rejection_reason=f"[G1 Data] {reason}",
                funnel_stage="DATA_CHECK", gates_passed="",
            )
            return False

    gates.append("G1:Data")

    # Remove from watchlist once DexScreener has data
    if address in MIGRATION_WATCHLIST:
        wait_sec = _time.time() - MIGRATION_WATCHLIST[address]["added_at"]
        logger.info(f"[{symbol}] Watchlist → indexed after {wait_sec:.0f}s")
        del MIGRATION_WATCHLIST[address]
        _save_watchlist()

    symbol      = token_data.get("symbol", symbol)
    price_usd   = float(token_data.get("price_usd",     0))
    liq         = float(token_data.get("liquidity_usd", 0))
    spike       = float(token_data.get("volume_spike",  0))
    ch_1h       = float(token_data.get("change_1h",     0))
    ch_5m       = float(token_data.get("change_5m",     0))
    age_hours   = get_token_age_hours(token_data)
    pair_created_ms = int(token_data.get("pair_created_at", 0) or 0)

    # ── Build extended context (stored in every DB row for analysis) ──────
    extra = {
        "source":           src_tag,
        "dex_url":          token_data.get("dex_url"),
        "market_cap":       float(token_data.get("market_cap", 0) or 0),
        "fdv":              float(token_data.get("fdv", 0) or 0),
        "liquidity_usd":    liq,
        "volume_h1":        float(token_data.get("volume_h1", 0) or 0),
        "volume_h24":       float(token_data.get("volume_h24", 0) or 0),
        "volume_spike":     spike,
        "change_5m":        ch_5m,
        "change_1h":        ch_1h,
        "change_24h":       float(token_data.get("change_24h", 0) or 0),
        "vol_mcap_ratio":   float(token_data.get("vol_mcap_ratio", 0) or 0),
        "buys_h1":          int(token_data.get("buys_h1", 0) or 0),
        "sells_h1":         int(token_data.get("sells_h1", 0) or 0),
        "buys_h24":         int(token_data.get("buys_h24", 0) or 0),
        "sells_h24":        int(token_data.get("sells_h24", 0) or 0),
        "token_age_hours":  round(age_hours, 2),
        # Raydium data (if token came from Raydium source)
        "raydium_vol_24h":  token.get("raydium_vol_24h"),
        "raydium_tvl":      token.get("raydium_tvl"),
        "raydium_burn_pct": token.get("raydium_burn"),
    }

    base_info = {
        "source":              src_tag,
        "is_migration":        is_migration,
        "pair_created_at":     pair_created_ms,
        "token_age_hours":     round(age_hours, 2),
        "scanned_at":          _time.time(),
        "pumpfun_detected_at": token.get("pumpfun_detected_at", 0),
        "market_data": {
            "liquidity_usd": liq,
            "market_cap":    token_data.get("market_cap", 0),
            "volume_24h":    token_data.get("volume_24h", 0),
            "change_5m":     ch_5m,
            "change_1h":     ch_1h,
            "change_24h":    token_data.get("change_24h", 0),
            "buys_h1":       token_data.get("buys_h1", 0),
            "sells_h1":      token_data.get("sells_h1", 0),
        },
    }

    logger.info(
        f"[{symbol}] [{src_tag}] ${price_usd:.8f} | "
        f"Spike: {spike:.1f}x | 1h: {ch_1h:+.1f}% | 5m: {ch_5m:+.1f}% | "
        f"Liq: ${liq:,.0f}"
    )

    # ── GATE 2: Safety (RugCheck) ─────────────────────────────────────────────
    safety_data = await safety.get_safety_details(address)
    if not safety_data or not safety_data.get("is_safe"):
        reason = (
            safety_data.get("mint_authority", "Unknown")
            if safety_data else "No safety data / possible scam"
        )
        logger.warning(f"[{symbol}] G2 FAIL: Safety — {reason}")
        # Add partial safety data to extra for rejected tokens
        if safety_data:
            extra.update({
                "mint_authority":    safety_data.get("mint_authority"),
                "rugcheck_score":    safety_data.get("rugcheck_score"),
                "rugcheck_lp_locked": safety_data.get("rugcheck_lp_locked"),
                "rugcheck_dangers":  ",".join(safety_data.get("rugcheck_dangers", [])) or None,
                "rugcheck_warnings": ",".join(safety_data.get("rugcheck_warnings", [])) or None,
            })
        await executor.execute_trade(
            symbol, address, 0, "REJECT",
            price=price_usd, rejection_reason=f"[G2 Safety] {reason}",
            ai_reasoning=json.dumps(base_info),
            funnel_stage="SAFETY_CHECK", gates_passed="",
            pair_created_at=pair_created_ms,
            extra=extra,
        )
        return False

    gates.append("G2:Safety")
    extra.update({
        "mint_authority":    safety_data.get("mint_authority"),
        "rugcheck_score":    safety_data.get("rugcheck_score"),
        "rugcheck_lp_locked": safety_data.get("rugcheck_lp_locked"),
        "rugcheck_dangers":  ",".join(safety_data.get("rugcheck_dangers", [])) or None,
        "rugcheck_warnings": ",".join(safety_data.get("rugcheck_warnings", [])) or None,
    })

    # ── GATE 3: Chain data + risk assessment ──────────────────────────────────
    # Cap G3 at 6 s to prevent free-tier RPC hangs from blocking the event loop
    try:
        chain_data = await asyncio.wait_for(chain.get_chain_data(address), timeout=6.0)
    except asyncio.TimeoutError:
        logger.warning(f"[{symbol}] G3: chain data timed out — using safe defaults")
        chain_data = {}
    top_10_pct  = float(chain_data.get("top_10_holder_percent", 100))
    liq_locked  = bool(chain_data.get("liquidity_locked", False))
    hype_score  = calculate_hype_score(token_data)
    risk_flags  = get_risk_flags(token_data, top_10_pct, is_migration=is_migration)

    base_info["chain_data"] = {
        "top_10_pct":    top_10_pct,
        "liq_locked":    liq_locked,
        "holder_count":  chain_data.get("holder_count", 0),
    }
    base_info["hype_score"] = hype_score
    base_info["risk_flags"] = risk_flags
    gates.append("G3:Risk")
    extra.update({
        "top_10_holder_pct": top_10_pct,
        "holder_count":      chain_data.get("holder_count"),
        "liquidity_locked":  liq_locked,
        "hype_score":        hype_score,
        "risk_flags":        ",".join(risk_flags),
    })

    # ── GATE 4: Pre-buy filter ────────────────────────────────────────────────
    ok, reason = (
        pre_buy_filter_migration(token_data, risk_flags)
        if is_migration
        else pre_buy_filter(token_data, risk_flags)
    )
    filter_label = "Migration" if is_migration else "Standard"

    if not ok:
        detail = (
            f"[G4 PreFilter/{filter_label}] {reason} | "
            f"Hype:{hype_score} Flags:{','.join(risk_flags)} "
            f"Liq:${liq:,.0f} Spike:{spike:.1f}x 1h:{ch_1h:+.1f}% "
            f"Top10:{top_10_pct:.0f}%"
        )
        logger.warning(f"[{symbol}] G4 FAIL ({filter_label}): {reason}")
        await executor.execute_trade(
            symbol, address, 0, "REJECT",
            price=price_usd, rejection_reason=detail,
            ai_reasoning=json.dumps(base_info),
            funnel_stage="PRE_FILTER", gates_passed=",".join(gates),
            pair_created_at=pair_created_ms,
            extra=extra,
        )
        return False

    gates.append("G4:PreFilter")
    logger.info(f"[{symbol}] G4 OK ({filter_label}) | Hype: {hype_score} | Flags: {risk_flags}")

    # ── GATE 5: Fusion scoring ────────────────────────────────────────────────
    claude_result = dict(base_info)
    claude_result.update({
        "hype_score": hype_score,
        "risk_flags": risk_flags,
        "sentiment":  "Bullish" if hype_score >= 50 else "Neutral",
        "key_signals": [
            f"Source: {src_tag}",       f"Vol-Spike {spike:.1f}x",
            f"1h {ch_1h:+.1f}%",        f"5m {ch_5m:+.1f}%",
            f"Liq ${liq:,.0f}",          f"MCap ${token_data.get('market_cap',0):,.0f}",
            f"Buys/Sells 1h: {token_data.get('buys_h1',0)}/{token_data.get('sells_h1',0)}",
            f"Top10: {top_10_pct:.0f}%", f"LiqLock: {liq_locked}",
            f"Age: {age_hours:.1f}h",
        ],
    })
    claude_result["market_data"]["vol_mcap_ratio"] = token_data.get("vol_mcap_ratio", 0)
    claude_result["market_data"]["buys_h24"]       = token_data.get("buys_h24", 0)
    claude_result["market_data"]["sells_h24"]      = token_data.get("sells_h24", 0)

    if is_migration:
        claude_result["hype_score"] = min(100, hype_score + 15)
        claude_result["key_signals"].insert(0, "PUMP.FUN MIGRATION (bonus +15)")

    market_data   = {"btc_1h_change": btc_change, "volume_spike": spike}
    fusion_result = fusion.calculate_score(claude_result, chain_data, token_data, market_data)
    score      = fusion_result["score"]
    decision   = fusion_result["decision"]
    confidence = fusion_result.get("confidence", "LOW")
    breakdown  = fusion_result.get("breakdown", {})

    extra.update({
        "fusion_hype":      breakdown.get("hype_momentum"),
        "fusion_liq_lock":  breakdown.get("liquidity_lock"),
        "fusion_vol_spike": breakdown.get("volume_spike"),
        "fusion_wallet":    breakdown.get("wallet_concentration"),
        "fusion_buy_sell":  breakdown.get("buy_sell_pressure"),
        "fusion_vol_mcap":  breakdown.get("vol_mcap_ratio"),
        "fusion_risk":      breakdown.get("risk_score"),
        "fusion_btc":       breakdown.get("btc_market"),
        "fusion_override":  breakdown.get("override_reason") or None,
    })

    logger.info(f"[{symbol}] Score: {score:.1f} | Decision: {decision} | Conf: {confidence}")

    if decision != "BUY":
        over = breakdown.get("override_reason", "")
        detail = (
            f"[G5 Scoring] Score {score:.1f} → {decision} | Confidence:{confidence}"
            + (f" | Override: {over}" if over else "")
            + f" | Hype={breakdown.get('hype_momentum',0):.1f}"
        )
        await executor.execute_trade(
            symbol, address, score, "REJECT",
            price=price_usd, rejection_reason=detail,
            ai_reasoning=json.dumps(claude_result),
            funnel_stage="SCORING", gates_passed=",".join(gates),
            pair_created_at=pair_created_ms,
            extra=extra,
        )
        return False

    gates.append("G5:Scoring")

    # ── GATE 6: Execution ─────────────────────────────────────────────────────
    if len(monitor.positions) >= 20:
        logger.warning(f"Max positions reached — skipping {symbol}")
        await executor.execute_trade(
            symbol, address, score, "REJECT",
            price=price_usd, rejection_reason="[G6 Exec] Max 20 positions reached",
            funnel_stage="EXEC_LIMIT", gates_passed=",".join(gates),
            pair_created_at=pair_created_ms,
            extra=extra,
        )
        return False

    gates.append("G6:Exec")
    accept_detail = (
        f"[ACCEPT] Score {score:.1f} | {confidence} | {src_tag} | "
        f"Hype:{hype_score} Liq:${liq:,.0f} Spike:{spike:.1f}x "
        f"1h:{ch_1h:+.1f}% 5m:{ch_5m:+.1f}% Top10:{top_10_pct:.0f}%"
    )
    claude_result["accept_detail"] = accept_detail

    res = await executor.execute_trade(
        symbol, address, score, "BUY",
        price=price_usd, rejection_reason=None,
        ai_reasoning=json.dumps(claude_result),
        funnel_stage="BUY_EXEC",
        confidence=confidence,
        liquidity_usd=liq,
        gates_passed=",".join(gates),
        pair_created_at=pair_created_ms,
        extra=extra,
    )

    if res and res.get("status") == "success":
        BOUGHT_THIS_SESSION.add(address)
        await monitor.add_position(address, price_usd, symbol=symbol)
        if not executor.dry_run:
            send_whatsapp_update(
                f"BUY: {symbol} @ ${price_usd:.8f} | Score: {score:.0f} | "
                f"{confidence} | {src_tag}"
            )
        logger.success(f"[{symbol}] BOUGHT @ ${price_usd:.8f} | {confidence} | {src_tag}")
        return True

    return False


# ── Main loop ─────────────────────────────────────────────────────────────────

async def main_loop() -> None:
    logger.info("=" * 65)
    logger.info("Memecoin Trading Bot — Multi-Source Discovery")
    logger.info(f"DRY_RUN: {os.getenv('DRY_RUN')} | "
                f"Position: ${os.getenv('TRADE_MAX_POSITION_USD')}")
    logger.info("=" * 65)

    dex         = DexScreenerAdapter()
    pumpfun     = PumpFunAdapter()
    helius      = HeliusStreamAdapter()
    established = EstablishedAdapter()
    raydium     = RaydiumAdapter()
    safety      = SafetyAdapter()
    chain       = SolanaAdapter()
    fusion      = SignalFusion()
    executor    = TradeExecutor()
    monitor     = PositionMonitor()

    global BOUGHT_THIS_SESSION
    BOUGHT_THIS_SESSION = set(monitor.positions.keys())
    logger.info(f"Existing positions loaded: {len(BOUGHT_THIS_SESSION)}")

    asyncio.create_task(monitor.monitor())
    asyncio.create_task(pumpfun.start())
    asyncio.create_task(pumpfun.cleanup_loop())
    asyncio.create_task(helius.start())
    asyncio.create_task(helius.cleanup_loop())
    await asyncio.sleep(3)

    scan_count = 0
    while True:
        if os.path.exists("STOP_BOT"):
            logger.warning("STOP_BOT detected — stopping.")
            break

        # ── Manual trade trigger ──────────────────────────────────────────────
        if os.path.exists("MANUAL_TRADE"):
            await _handle_manual_trade(executor, monitor)

        try:
            scan_count += 1
            pf_status  = pumpfun.status()
            hel_status = helius.status()
            logger.info(
                f"── Scan #{scan_count} | "
                f"PF: {'OK' if pf_status['connected'] else 'DOWN'} | "
                f"Helius: {'OK' if hel_status['connected'] else 'DOWN'} | "
                f"Positions: {len(monitor.positions)}/20 ──"
            )

            btc_change = await get_btc_change()

            bought = 0

            # P0: Helius Raydium stream (fastest source)
            for t in helius.get_candidates(limit=10):
                bought += await evaluate_token(t, dex, safety, chain, fusion,
                                               executor, monitor, btc_change, True)

            # P0: Watchlist retry
            bought += await _retry_watchlist(dex, safety, chain, fusion, executor,
                                              monitor, btc_change)

            # P1: Pump.fun migrations
            for t in pumpfun.get_migration_candidates(limit=10):
                bought += await evaluate_token(t, dex, safety, chain, fusion,
                                               executor, monitor, btc_change, True)

            # P2: Established memecoins
            for t in await established.get_candidates():
                bought += await evaluate_token(t, dex, safety, chain, fusion,
                                               executor, monitor, btc_change, False)

            # P2.5: Raydium top volume pools (free, no API key)
            for t in await raydium.get_candidates():
                bought += await evaluate_token(t, dex, safety, chain, fusion,
                                               executor, monitor, btc_change, False)

            # P3: DexScreener trending + boosted + CTO + ads
            for t in (await dex.get_all_candidates())[:15]:
                bought += await evaluate_token(t, dex, safety, chain, fusion,
                                               executor, monitor, btc_change, False)

            # P4: Pump.fun new tokens (only if nothing found yet)
            if bought == 0:
                for t in pumpfun.get_new_token_candidates(limit=5):
                    bought += await evaluate_token(t, dex, safety, chain, fusion,
                                                   executor, monitor, btc_change, False)

            logger.info(
                f"── Scan #{scan_count} done | Bought: {bought} | "
                f"Positions: {len(monitor.positions)}/20 | "
                f"Watchlist: {len(MIGRATION_WATCHLIST)} | sleeping 30s ──"
            )
            await asyncio.sleep(30)

        except Exception as e:
            logger.error(f"Loop error: {e}")
            await asyncio.sleep(30)


# ── Helper: watchlist retry ───────────────────────────────────────────────────

async def _retry_watchlist(
    dex, safety, chain, fusion, executor, monitor, btc_change
) -> int:
    if not MIGRATION_WATCHLIST:
        return 0

    now     = _time.time()
    expired = [
        addr for addr, wl in MIGRATION_WATCHLIST.items()
        if now - wl["added_at"] > WATCHLIST_MAX_AGE_SEC
        or wl["retries"] >= WATCHLIST_MAX_RETRIES
    ]
    for addr in expired:
        sym = MIGRATION_WATCHLIST[addr]["token"].get("symbol", "?")
        logger.warning(
            f"[{sym}] Watchlist EXPIRED: {MIGRATION_WATCHLIST[addr]['retries']} retries, "
            f"{now - MIGRATION_WATCHLIST[addr]['added_at']:.0f}s"
        )
        del MIGRATION_WATCHLIST[addr]
    if expired:
        _save_watchlist()

    bought = 0
    retry_list = list(MIGRATION_WATCHLIST.items())
    if retry_list:
        logger.info(f"── Watchlist retry: {len(retry_list)} pending ──")
    for addr, wl in retry_list:
        wl["retries"] += 1
        bought += await evaluate_token(
            wl["token"], dex, safety, chain, fusion, executor, monitor,
            btc_change, is_migration=True,
        )
    return bought


# ── Helper: manual trade ──────────────────────────────────────────────────────

async def _handle_manual_trade(executor: TradeExecutor, monitor: PositionMonitor) -> None:
    try:
        with open("MANUAL_TRADE") as f:
            trigger = json.load(f)
        os.remove("MANUAL_TRADE")

        action    = trigger.get("action", "").upper()
        addr      = trigger.get("address", "")
        sell_pct  = float(trigger.get("sell_pct",  1.0))
        amount    = float(trigger.get("amount", executor.max_position_usd))

        if not addr:
            return

        logger.info(f"[MANUAL] {action} {addr[:20]}... ({sell_pct*100:.0f}%)")

        dex_tmp = DexScreenerAdapter()
        td      = await dex_tmp.get_token_data(addr)
        price   = float(td.get("price_usd", 0)) if td else 0.0
        sym     = str(td.get("symbol", addr[:8])) if td else addr[:8]

        if action == "SELL":
            result = await executor.execute_trade(
                sym, addr, 100.0, "SELL",
                price=price, confidence="HIGH",
                rejection_reason="Manual sell via dashboard",
                funnel_stage="MANUAL", gates_passed="MANUAL",
                sell_fraction=sell_pct,
            )
            if result and result.get("status") == "success":
                logger.info(f"[MANUAL] ✅ SELL OK: {result.get('tx', '')}")
                if sell_pct >= 1.0 and addr in monitor.positions:
                    async with monitor.lock:
                        del monitor.positions[addr]
                    await monitor._save_positions()
            else:
                logger.error(f"[MANUAL] ❌ SELL failed: {result}")

        elif action == "BUY":
            result = await executor.execute_trade(
                sym, addr, 100.0, "BUY",
                price=price, confidence="HIGH",
                funnel_stage="MANUAL", gates_passed="MANUAL",
                position_size_override=amount,
            )
            if result and result.get("status") == "success":
                logger.info(f"[MANUAL] ✅ BUY OK: {result.get('tx', '')}")
                await monitor.add_position(addr, price, sym)
            else:
                logger.error(f"[MANUAL] ❌ BUY failed: {result}")

    except Exception as e:
        logger.error(f"[MANUAL] Processing error: {e}")
        if os.path.exists("MANUAL_TRADE"):
            os.remove("MANUAL_TRADE")
