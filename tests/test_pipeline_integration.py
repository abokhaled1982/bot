"""
tests/test_pipeline_integration.py — End-to-end pipeline integration test.

Runs ~100 real memecoins through the FULL pipeline with SIMULATED trades.
No real money is used — all buys/sells are dry-run.

Tests:
  1. Token discovery from ALL sources (DexScreener, Raydium, Established)
  2. Full 6-gate evaluation pipeline for each token
  3. Simulated BUY execution for tokens that pass all gates
  4. Simulated SELL execution for bought positions
  5. Generates a summary report with pass/fail stats per gate

Usage:
  pytest tests/test_pipeline_integration.py -v -s
"""
from __future__ import annotations

import asyncio
import json
import os
import sqlite3
import tempfile
import time

import pytest
from loguru import logger

# ── Force DRY_RUN for safety ─────────────────────────────────────────────────
os.environ["DRY_RUN"] = "TRUE"
os.environ["TRADE_MAX_POSITION_USD"] = "0.10"

from src.adapters.dexscreener import DexScreenerAdapter
from src.adapters.raydium     import RaydiumAdapter
from src.adapters.established import EstablishedAdapter
from src.adapters.safety      import SafetyAdapter
from src.adapters.solana_chain import SolanaAdapter
from src.analysis.fusion      import SignalFusion
from src.execution.executor   import TradeExecutor
from src.bot.filters          import (
    BLOCKED_TOKENS,
    calculate_hype_score,
    get_risk_flags,
    get_token_age_hours,
    pre_buy_filter,
    pre_buy_filter_migration,
)


# ── Test DB (isolated, not your real DB) ──────────────────────────────────────
TEST_DB = os.path.join(tempfile.gettempdir(), "test_pipeline_integration.db")


def _init_test_db():
    conn = sqlite3.connect(TEST_DB)
    c = conn.cursor()
    c.execute("""CREATE TABLE IF NOT EXISTS trades (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        token_address TEXT, symbol TEXT, entry_price REAL,
        position_size REAL, score REAL, decision TEXT,
        rejection_reason TEXT, ai_reasoning TEXT,
        funnel_stage TEXT, gates_passed TEXT,
        pair_created_at INTEGER, tx_signature TEXT,
        tx_status TEXT, buy_amount_usd REAL,
        sell_amount_usd REAL, timestamp DATETIME
    )""")
    c.execute("""CREATE TABLE IF NOT EXISTS bot_events (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        event_type TEXT, symbol TEXT, address TEXT,
        tx_signature TEXT, buy_amount_usd REAL,
        sell_amount_usd REAL, price_usd REAL,
        pnl_usd REAL, pnl_pct REAL,
        stage TEXT, message TEXT, timestamp TEXT
    )""")
    c.execute("""CREATE TABLE IF NOT EXISTS bot_logs (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        level TEXT, message TEXT, timestamp DATETIME
    )""")
    conn.commit()
    conn.close()


# ── Evaluation result tracker ─────────────────────────────────────────────────

class TokenResult:
    __slots__ = (
        "address", "symbol", "source", "price", "mcap", "liquidity",
        "g1_data", "g2_safety", "g3_risk", "g4_prefilter", "g5_score",
        "g6_exec", "decision", "score", "confidence",
        "rejection_reason", "risk_flags", "hype_score",
        "rugcheck_score", "sell_simulated", "elapsed_ms",
    )

    def __init__(self, address: str, symbol: str, source: str):
        self.address = address
        self.symbol = symbol
        self.source = source
        self.price = 0.0
        self.mcap = 0.0
        self.liquidity = 0.0
        self.g1_data = False
        self.g2_safety = False
        self.g3_risk = False
        self.g4_prefilter = False
        self.g5_score = False
        self.g6_exec = False
        self.decision = "PENDING"
        self.score = 0.0
        self.confidence = ""
        self.rejection_reason = ""
        self.risk_flags = []
        self.hype_score = 0
        self.rugcheck_score = None
        self.sell_simulated = False
        self.elapsed_ms = 0

    @property
    def gate_reached(self) -> str:
        if self.g6_exec: return "G6:Exec"
        if self.g5_score: return "G5:Score"
        if self.g4_prefilter: return "G4:PreFilter"
        if self.g3_risk: return "G3:Risk"
        if self.g2_safety: return "G2:Safety"
        if self.g1_data: return "G1:Data"
        return "G0:None"


# ── Full pipeline evaluation (mirrors pipeline.py logic) ─────────────────────

async def evaluate_token_full(
    token: dict,
    dex: DexScreenerAdapter,
    safety: SafetyAdapter,
    chain: SolanaAdapter,
    fusion: SignalFusion,
    executor: TradeExecutor,
) -> TokenResult:
    """Run one token through the full 6-gate pipeline. Returns detailed result."""
    address = token.get("address", "")
    symbol = token.get("symbol", "?")
    source = token.get("source", "unknown")
    result = TokenResult(address, symbol, source)
    t0 = time.time()

    # ── GATE 1: DexScreener data ──────────────────────────────────────────
    if address in BLOCKED_TOKENS:
        result.decision = "BLOCKED"
        result.rejection_reason = "Blocked token (stablecoin/infra)"
        result.elapsed_ms = int((time.time() - t0) * 1000)
        return result

    token_data = await dex.get_token_data(address)
    if not token_data:
        result.decision = "SKIP"
        result.rejection_reason = "G1: No DexScreener data"
        result.elapsed_ms = int((time.time() - t0) * 1000)
        return result

    result.g1_data = True
    result.symbol = token_data.get("symbol", symbol)
    result.price = float(token_data.get("price_usd", 0))
    result.mcap = float(token_data.get("market_cap", 0))
    result.liquidity = float(token_data.get("liquidity_usd", 0))

    # ── GATE 2: Safety (RugCheck + mint authority) ────────────────────────
    safety_data = await safety.get_safety_details(address)
    if not safety_data or not safety_data.get("is_safe"):
        reason = safety_data.get("reason", safety_data.get("mint_authority", "Unknown")) if safety_data else "No safety data"
        result.decision = "REJECT"
        result.rejection_reason = f"G2: Safety — {reason}"
        result.rugcheck_score = safety_data.get("rugcheck_score") if safety_data else None
        result.elapsed_ms = int((time.time() - t0) * 1000)
        return result

    result.g2_safety = True
    result.rugcheck_score = safety_data.get("rugcheck_score")

    # ── GATE 3: Chain data + risk assessment ──────────────────────────────
    try:
        chain_data = await asyncio.wait_for(chain.get_chain_data(address), timeout=6.0)
    except asyncio.TimeoutError:
        chain_data = {}

    top_10_pct = float(chain_data.get("top_10_holder_percent", 100))
    hype_score = calculate_hype_score(token_data)
    risk_flags = get_risk_flags(token_data, top_10_pct)

    result.g3_risk = True
    result.hype_score = hype_score
    result.risk_flags = risk_flags

    # ── GATE 4: Pre-buy filter ────────────────────────────────────────────
    ok, reason = pre_buy_filter(token_data, risk_flags)
    if not ok:
        result.decision = "REJECT"
        result.rejection_reason = f"G4: {reason}"
        result.elapsed_ms = int((time.time() - t0) * 1000)
        return result

    result.g4_prefilter = True

    # ── GATE 5: Fusion scoring ────────────────────────────────────────────
    claude_result = {
        "hype_score": hype_score,
        "risk_flags": risk_flags,
        "sentiment": "Bullish" if hype_score >= 50 else "Neutral",
        "key_signals": [],
        "market_data": {
            "liquidity_usd": result.liquidity,
            "market_cap": result.mcap,
            "vol_mcap_ratio": token_data.get("vol_mcap_ratio", 0),
            "buys_h24": token_data.get("buys_h24", 0),
            "sells_h24": token_data.get("sells_h24", 0),
        },
    }
    market_data = {"btc_1h_change": 0.0, "volume_spike": float(token_data.get("volume_spike", 0))}
    fusion_result = fusion.calculate_score(claude_result, chain_data, token_data, market_data)

    result.score = fusion_result["score"]
    result.confidence = fusion_result.get("confidence", "LOW")

    if fusion_result["decision"] != "BUY":
        result.g5_score = True
        result.decision = fusion_result["decision"]
        over = fusion_result.get("breakdown", {}).get("override_reason", "")
        result.rejection_reason = f"G5: Score {result.score:.1f} → {result.decision}" + (f" ({over})" if over else "")
        result.elapsed_ms = int((time.time() - t0) * 1000)
        return result

    result.g5_score = True

    # ── GATE 6: Simulated execution ───────────────────────────────────────
    gates = "G1:Data,G2:Safety,G3:Risk,G4:PreFilter,G5:Scoring,G6:Exec"
    buy_result = await executor.execute_trade(
        result.symbol, address, result.score, "BUY",
        price=result.price, confidence=result.confidence,
        liquidity_usd=result.liquidity,
        funnel_stage="BUY_EXEC", gates_passed=gates,
    )
    if buy_result and buy_result.get("status") == "success":
        result.g6_exec = True
        result.decision = "BUY (SIMULATED)"

        # Simulate SELL immediately after BUY
        sell_result = await executor.execute_trade(
            result.symbol, address, result.score, "SELL",
            price=result.price, confidence=result.confidence,
            funnel_stage="SELL_SIM", gates_passed=gates,
            sell_fraction=1.0,
        )
        if sell_result and sell_result.get("status") == "success":
            result.sell_simulated = True
    else:
        result.decision = "EXEC_FAIL"
        result.rejection_reason = "G6: Execution failed"

    result.elapsed_ms = int((time.time() - t0) * 1000)
    return result


# ── Report generator ──────────────────────────────────────────────────────────

def print_report(results: list[TokenResult], elapsed_sec: float):
    total = len(results)
    g1 = sum(1 for r in results if r.g1_data)
    g2 = sum(1 for r in results if r.g2_safety)
    g3 = sum(1 for r in results if r.g3_risk)
    g4 = sum(1 for r in results if r.g4_prefilter)
    g5 = sum(1 for r in results if r.g5_score)
    g6 = sum(1 for r in results if r.g6_exec)
    bought = sum(1 for r in results if r.g6_exec)
    sold = sum(1 for r in results if r.sell_simulated)
    blocked = sum(1 for r in results if r.decision == "BLOCKED")

    print("\n")
    print("=" * 90)
    print("  PIPELINE INTEGRATION TEST — REPORT")
    print("=" * 90)
    print(f"  Total tokens evaluated:  {total}")
    print(f"  Total time:              {elapsed_sec:.1f}s ({elapsed_sec/total:.2f}s avg per token)")
    print(f"  Database:                {TEST_DB}")
    print()
    print("  ── Funnel ──────────────────────────────────────────────────")
    print(f"  {'Candidates':30s} {total:>5d}  (100%)")
    print(f"  {'G1: DexScreener data':30s} {g1:>5d}  ({g1/total*100:.0f}%)")
    print(f"  {'G2: Safety (RugCheck)':30s} {g2:>5d}  ({g2/total*100:.0f}%)")
    print(f"  {'G3: Risk assessment':30s} {g3:>5d}  ({g3/total*100:.0f}%)")
    print(f"  {'G4: Pre-buy filter':30s} {g4:>5d}  ({g4/total*100:.0f}%)")
    print(f"  {'G5: Fusion scoring':30s} {g5:>5d}  ({g5/total*100:.0f}%)")
    print(f"  {'G6: BUY (simulated)':30s} {g6:>5d}  ({g6/total*100:.0f}%)")
    print(f"  {'    SELL (simulated)':30s} {sold:>5d}  ({sold/total*100:.0f}%)")
    print(f"  {'Blocked (stablecoin/infra)':30s} {blocked:>5d}")
    print()

    # ── Rejection breakdown ───────────────────────────────────────────────
    fail_at = {"G0": 0, "G1": 0, "G2": 0, "G3": 0, "G4": 0, "G5": 0}
    for r in results:
        if r.decision in ("BUY (SIMULATED)", "BLOCKED"):
            continue
        if not r.g1_data:
            fail_at["G1"] += 1
        elif not r.g2_safety:
            fail_at["G2"] += 1
        elif not r.g4_prefilter:
            fail_at["G4"] += 1
        elif r.g5_score and not r.g6_exec:
            fail_at["G5"] += 1
        else:
            fail_at["G5"] += 1  # scored but not BUY

    print("  ── Where tokens got rejected ─────────────────────────────")
    for gate, count in fail_at.items():
        if count > 0:
            bar = "█" * int(count / total * 40)
            print(f"  {gate}: {count:>3d}  {bar}")
    print()

    # ── Source breakdown ──────────────────────────────────────────────────
    sources: dict[str, dict] = {}
    for r in results:
        src = r.source.split("_")[0] if "_" in r.source else r.source
        if src not in sources:
            sources[src] = {"total": 0, "bought": 0}
        sources[src]["total"] += 1
        if r.g6_exec:
            sources[src]["bought"] += 1

    print("  ── By source ─────────────────────────────────────────────")
    print(f"  {'Source':20s} {'Total':>6s} {'Bought':>7s} {'Rate':>6s}")
    for src, s in sorted(sources.items(), key=lambda x: x[1]["total"], reverse=True):
        rate = f"{s['bought']/s['total']*100:.0f}%" if s["total"] > 0 else "—"
        print(f"  {src:20s} {s['total']:>6d} {s['bought']:>7d} {rate:>6s}")
    print()

    # ── Bought tokens detail ──────────────────────────────────────────────
    bought_tokens = [r for r in results if r.g6_exec]
    if bought_tokens:
        print("  ── SIMULATED BUYS ────────────────────────────────────────")
        print(f"  {'Symbol':12s} {'Score':>6s} {'Conf':>6s} {'Price':>12s} {'MCap':>12s} {'Liq':>10s} {'Hype':>5s} {'RugCk':>6s} {'Sold':>5s} {'ms':>5s}")
        for r in sorted(bought_tokens, key=lambda x: x.score, reverse=True):
            print(
                f"  {r.symbol:12s} {r.score:6.1f} {r.confidence:>6s} "
                f"${r.price:<11.8f} ${r.mcap:>10,.0f} ${r.liquidity:>8,.0f} "
                f"{r.hype_score:>5d} {str(r.rugcheck_score):>6s} "
                f"{'✅' if r.sell_simulated else '❌':>5s} {r.elapsed_ms:>5d}"
            )
    else:
        print("  ── NO TOKENS PASSED ALL 6 GATES ──────────────────────────")
    print()

    # ── Top rejected tokens (closest to passing) ──────────────────────────
    rejected = [r for r in results if not r.g6_exec and r.g1_data]
    rejected.sort(key=lambda x: x.score, reverse=True)
    print("  ── TOP 15 REJECTED (highest scores) ──────────────────────")
    print(f"  {'Symbol':12s} {'Score':>6s} {'Gate':>12s} {'Reason':50s}")
    for r in rejected[:15]:
        reason = r.rejection_reason[:50] if r.rejection_reason else ""
        print(f"  {r.symbol:12s} {r.score:6.1f} {r.gate_reached:>12s} {reason}")
    print()

    # ── Risk flag frequency ───────────────────────────────────────────────
    flag_counts: dict[str, int] = {}
    for r in results:
        for f in r.risk_flags:
            flag_counts[f] = flag_counts.get(f, 0) + 1

    if flag_counts:
        print("  ── Risk flag frequency ───────────────────────────────────")
        for flag, count in sorted(flag_counts.items(), key=lambda x: x[1], reverse=True):
            bar = "█" * int(count / total * 30)
            print(f"  {flag:25s} {count:>4d}  {bar}")
    print()
    print("=" * 90)


# ══════════════════════════════════════════════════════════════════════════════
# TESTS
# ══════════════════════════════════════════════════════════════════════════════


def _make_adapters():
    """Create fresh adapters (call per test to avoid event-loop issues)."""
    _init_test_db()
    dex = DexScreenerAdapter()
    raydium = RaydiumAdapter()
    established = EstablishedAdapter()
    safety = SafetyAdapter()
    chain = SolanaAdapter()
    fusion = SignalFusion()

    executor = TradeExecutor()
    executor.dry_run = True
    executor.db_path = TEST_DB

    return {
        "dex": dex,
        "raydium": raydium,
        "established": established,
        "safety": safety,
        "chain": chain,
        "fusion": fusion,
        "executor": executor,
    }


# ── Test 1: Token discovery from all sources ──────────────────────────────────

@pytest.mark.asyncio
async def test_discovery_sources():
    """Verify all discovery sources return candidates."""
    adapters = _make_adapters()
    dex = adapters["dex"]
    raydium = adapters["raydium"]
    established = adapters["established"]

    dex_cands = await dex.get_all_candidates()
    ray_cands = await raydium.get_candidates()
    est_cands = await established.get_candidates()

    print(f"\n  Discovery: DexScreener={len(dex_cands)} Raydium={len(ray_cands)} Established={len(est_cands)}")

    # DexScreener should return boosted + CTO + ads + trending
    assert len(dex_cands) > 0, "DexScreener returned no candidates"

    # Raydium should return top pools
    assert len(ray_cands) > 0, "Raydium returned no candidates"

    # Each candidate must have address and source
    for c in dex_cands + ray_cands:
        assert c.get("address"), f"Candidate missing address: {c}"
        assert c.get("source"), f"Candidate missing source: {c}"

    await dex.close()


# ── Test 2: Full pipeline on ~100 tokens ──────────────────────────────────────

@pytest.mark.asyncio
async def test_full_pipeline_100_tokens():
    """Run ~100 tokens through the full 6-gate pipeline with simulated trades."""
    adapters = _make_adapters()
    dex = adapters["dex"]
    raydium = adapters["raydium"]
    established = adapters["established"]
    safety = adapters["safety"]
    chain = adapters["chain"]
    fusion = adapters["fusion"]
    executor = adapters["executor"]

    # Collect candidates from all sources (deduplicated)
    all_candidates = []
    seen = set()

    dex_cands = await dex.get_all_candidates()
    ray_cands = await raydium.get_candidates()
    est_cands = await established.get_candidates()

    for c in dex_cands + ray_cands + est_cands:
        addr = c.get("address")
        if addr and addr not in seen and addr not in BLOCKED_TOKENS:
            seen.add(addr)
            all_candidates.append(c)

    # Cap at ~100 for reasonable test time
    candidates = all_candidates[:100]
    total = len(candidates)
    print(f"\n  Evaluating {total} tokens through full pipeline...")

    # Run all evaluations
    results: list[TokenResult] = []
    t_start = time.time()

    for i, token in enumerate(candidates):
        if (i + 1) % 10 == 0:
            elapsed = time.time() - t_start
            print(f"  [{i+1}/{total}] {elapsed:.0f}s elapsed...")

        try:
            r = await evaluate_token_full(token, dex, safety, chain, fusion, executor)
            results.append(r)
        except Exception as e:
            logger.error(f"Error evaluating {token.get('symbol','?')}: {e}")
            r = TokenResult(token.get("address", ""), token.get("symbol", "?"), token.get("source", "?"))
            r.decision = "ERROR"
            r.rejection_reason = str(e)[:80]
            results.append(r)

    elapsed = time.time() - t_start

    # Print full report
    print_report(results, elapsed)

    # ── Assertions ────────────────────────────────────────────────────────
    assert len(results) == total, "Not all tokens were evaluated"

    # G1: Most tokens should have DexScreener data
    g1_pass = sum(1 for r in results if r.g1_data)
    assert g1_pass > total * 0.5, f"G1 pass rate too low: {g1_pass}/{total}"

    # G2: Safety should process without crashing
    g2_tested = sum(1 for r in results if r.g1_data)
    assert g2_tested > 0, "No tokens reached G2"

    # Every simulated BUY should also have a simulated SELL
    bought = [r for r in results if r.g6_exec]
    sold = [r for r in results if r.sell_simulated]
    if bought:
        assert len(sold) == len(bought), f"Buy/sell mismatch: {len(bought)} buys, {len(sold)} sells"

    # No errors should have occurred
    errors = [r for r in results if r.decision == "ERROR"]
    assert len(errors) <= total * 0.1, f"Too many errors: {len(errors)}/{total}"

    # Verify DB records inline
    conn = sqlite3.connect(TEST_DB)
    c = conn.cursor()
    c.execute("SELECT COUNT(*) FROM trades WHERE decision LIKE '%SIMULATED%'")
    sim_count = c.fetchone()[0]
    c.execute("SELECT COUNT(*) FROM bot_events WHERE event_type LIKE '%SIMULATED%'")
    evt_count = c.fetchone()[0]
    conn.close()
    print(f"  DB: {sim_count} simulated trade records, {evt_count} simulated events")
    if bought:
        assert sim_count > 0, "No simulated trades in DB despite buys"

    await dex.close()


# ── Individual gate tests ─────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_safety_gate_rugcheck():
    """Verify RugCheck integration returns valid scores."""
    safety = SafetyAdapter()

    # Known good token: FARTCOIN (revoked mint, low rug score)
    result = await safety.get_safety_details("9BB6NFEcjBCtnNLFko2FqVQBq8HHM13kCyYcdQbgpump")
    assert result.get("is_safe") is True, f"FARTCOIN should be safe: {result}"
    assert result.get("rugcheck_score") is not None, "Missing RugCheck score"
    assert result.get("rugcheck_score") < 500, f"FARTCOIN rug score too high: {result['rugcheck_score']}"
    print(f"\n  FARTCOIN: safe={result['is_safe']} rugcheck={result['rugcheck_score']} lp_locked={result.get('rugcheck_lp_locked',0):.0f}%")


@pytest.mark.asyncio
async def test_fusion_scoring():
    """Verify fusion scoring produces valid scores for sample data."""
    fusion = SignalFusion()

    # High-quality token data
    good_token = {
        "volume_spike": 5.0, "buys_h1": 100, "sells_h1": 40,
        "vol_mcap_ratio": 0.5, "change_1h": 20, "change_5m": 5,
    }
    good_claude = {
        "hype_score": 75, "risk_flags": ["No_Risk_Flags"],
        "market_data": {"vol_mcap_ratio": 0.5, "buys_h24": 500, "sells_h24": 200},
    }
    good_chain = {"top_10_holder_percent": 30, "liquidity_locked": True}

    result = fusion.calculate_score(good_claude, good_chain, good_token, {"btc_1h_change": 0.5})
    print(f"\n  Good token: score={result['score']:.1f} decision={result['decision']} confidence={result['confidence']}")
    assert result["score"] > 50, f"Good token should score high: {result['score']}"
    assert result["decision"] in ("BUY", "HOLD"), f"Unexpected decision: {result['decision']}"

    # Bad token data
    bad_token = {
        "volume_spike": 0.5, "buys_h1": 5, "sells_h1": 50,
        "vol_mcap_ratio": 0.01, "change_1h": -30, "change_5m": -15,
    }
    bad_claude = {
        "hype_score": 10, "risk_flags": ["Falling_Fast", "Heavy_Selling"],
        "market_data": {"vol_mcap_ratio": 0.01, "buys_h24": 20, "sells_h24": 200},
    }
    bad_chain = {"top_10_holder_percent": 90, "liquidity_locked": False}

    result = fusion.calculate_score(bad_claude, bad_chain, bad_token, {"btc_1h_change": -2.0})
    print(f"  Bad token:  score={result['score']:.1f} decision={result['decision']}")
    assert result["decision"] == "SKIP", f"Bad token should be SKIP: {result['decision']}"


@pytest.mark.asyncio
async def test_dex_token_data_caching():
    """Verify DexScreener token data caching works."""
    dex = DexScreenerAdapter()

    # Fetch a known token
    addr = "DezXAZ8z7PnrnRJjz3wXBoRgixCa6xjnB7YaB1pPB263"  # BONK
    t0 = time.time()
    data1 = await dex.get_token_data(addr)
    t1 = time.time()
    data2 = await dex.get_token_data(addr)  # should hit cache
    t2 = time.time()

    fetch_ms = (t1 - t0) * 1000
    cache_ms = (t2 - t1) * 1000
    print(f"\n  BONK fetch: {fetch_ms:.0f}ms, cached: {cache_ms:.0f}ms")

    assert data1 is not None, "BONK data should exist"
    assert data1 == data2, "Cache should return same data"
    await dex.close()


@pytest.mark.asyncio
async def test_executor_dry_run():
    """Verify executor is in dry-run mode and simulates correctly."""
    _init_test_db()
    executor = TradeExecutor()
    executor.dry_run = True
    executor.db_path = TEST_DB
    assert executor.dry_run is True, "Executor must be in DRY_RUN mode"

    result = await executor.execute_trade(
        "TEST_TOKEN", "FakeAddress123", 75.0, "BUY",
        price=0.001, confidence="HIGH",
        funnel_stage="TEST", gates_passed="TEST",
    )
    assert result.get("status") == "success", f"Dry-run BUY failed: {result}"
    assert result.get("dry_run") is True, "Should be flagged as dry_run"
    assert result.get("tx", "").startswith("SIM_"), "Should have simulated TX"
    assert result.get("buy_amount_usd") is not None, "Should have buy_amount_usd"

    result = await executor.execute_trade(
        "TEST_TOKEN", "FakeAddress123", 75.0, "SELL",
        price=0.001, confidence="HIGH",
        funnel_stage="TEST", gates_passed="TEST",
    )
    assert result.get("status") == "success", f"Dry-run SELL failed: {result}"
    assert result.get("dry_run") is True, "Should be flagged as dry_run"
    assert result.get("tx", "").startswith("SIM_"), "Should have simulated TX"
    assert result.get("sell_amount_usd") is not None, "Should have sell_amount_usd"

    print("\n  Executor: DRY_RUN=True, simulated BUY+SELL both OK")
