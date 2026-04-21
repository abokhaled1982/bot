import json
from loguru import logger
import time
import os
import redis

class SignalFusion:
    def __init__(self):
        redis_url = os.getenv("REDIS_URL", "redis://localhost:6379/0")
        try:
            self.cache = redis.from_url(redis_url, decode_responses=True)
        except Exception as e:
            logger.warning(f"Redis nicht verbunden: {e}")
            self.cache = None

    def calculate_score(self, claude_data: dict, chain_data: dict, dex_data: dict, btc_data: dict, unique_channels_5m: int = 0) -> dict:
        breakdown = {}

        # ── 1. Hype / Momentum Score (20%) ────────────────────────────────────
        hype          = claude_data.get("hype_score", 0)
        hype_weighted = (hype / 100) * 20
        breakdown["hype_momentum"] = round(hype_weighted, 2)

        # ── 2. Liquidity Lock (10%) — real check now, lower weight ────────────
        liq_lock      = 100 if chain_data.get("liquidity_locked", False) else 0
        liq_weighted  = (liq_lock / 100) * 10
        breakdown["liquidity_lock"] = round(liq_weighted, 2)

        # ── 3. Volume Spike (15%) ─────────────────────────────────────────────
        vol_spike     = min(dex_data.get("volume_spike", 0) * 20, 100)
        vol_weighted  = (vol_spike / 100) * 15
        breakdown["volume_spike"] = round(vol_weighted, 2)

        # ── 4. Wallet Konzentration (15%) ─────────────────────────────────────
        top_10_pct    = chain_data.get("top_10_holder_percent", 100)
        wallet_conc   = max(100 - top_10_pct, 0)
        wallet_w      = (wallet_conc / 100) * 15
        breakdown["wallet_concentration"] = round(wallet_w, 2)

        # ── 5. Buy/Sell Pressure (15%) ────────────────────────────────────────
        buys_h1  = int(dex_data.get("buys_h1", 0))
        sells_h1 = int(dex_data.get("sells_h1", 0))
        total_txns = buys_h1 + sells_h1
        if total_txns > 5:
            buy_ratio = buys_h1 / total_txns
            pressure_score = min(max((buy_ratio - 0.3) / 0.4, 0), 1) * 100  # 30%=0, 70%=100
        else:
            pressure_score = 50  # neutral if not enough data
        pressure_w = (pressure_score / 100) * 15
        breakdown["buy_sell_pressure"] = round(pressure_w, 2)

        # ── 6. Volume/MCap Ratio (10%) — momentum signal ─────────────────────
        vol_mcap = float(dex_data.get("vol_mcap_ratio", 0))
        if   vol_mcap >= 1.0:  vmr_score = 100  # very high activity
        elif vol_mcap >= 0.5:  vmr_score = 80
        elif vol_mcap >= 0.2:  vmr_score = 60
        elif vol_mcap >= 0.1:  vmr_score = 40
        elif vol_mcap >= 0.05: vmr_score = 20
        else:                  vmr_score = 5
        vmr_w = (vmr_score / 100) * 10
        breakdown["vol_mcap_ratio"] = round(vmr_w, 2)

        # ── 7. Risk Flag Score (10%) ──────────────────────────────────────────
        risks   = claude_data.get("risk_flags", [])
        if "No_Risk_Flags" in risks:
            risk_score = 100
        else:
            # Each risk flag reduces score
            penalty = len([f for f in risks if f != "No_Risk_Flags"]) * 25
            risk_score = max(0, 100 - penalty)
        risk_w = (risk_score / 100) * 10
        breakdown["risk_score"] = round(risk_w, 2)

        # ── 8. BTC Marktlage (5%) — less weight, memcoins decorrelate ────────
        btc_change    = btc_data.get("btc_1h_change", 0)
        if   btc_change >= 2.0:   btc_score = 90
        elif btc_change <= -5.0:  btc_score = 10
        else:
            normalized = (btc_change + 5) / 7
            btc_score  = 10 + (normalized * 80)
        btc_weighted = (btc_score / 100) * 5
        breakdown["btc_market"] = round(btc_weighted, 2)

        fusion_score = sum(v for v in breakdown.values() if isinstance(v, (int, float)))

        # ── Entscheidung ──────────────────────────────────────────────────────
        if   fusion_score >= 65: decision = "BUY"
        elif fusion_score >= 40: decision = "HOLD"
        else:                    decision = "SKIP"

        # ── OVERRIDE REGELN ───────────────────────────────────────────────────
        if top_10_pct > 80:
            decision = "SKIP"
            breakdown["override_reason"] = "Top 10 halten > 80%"

        elif any(f in risks for f in ["Pump_Suspicion", "Rugpull_Hint"]):
            decision = "SKIP"
            breakdown["override_reason"] = "Critical risk flags detected"

        elif any(f in risks for f in ["Falling_Fast", "Dumping_Now", "Heavy_Selling"]):
            decision = "SKIP"
            breakdown["override_reason"] = "Token fällt gerade — kein Kauf"

        elif any(f in risks for f in ["Low_Liquidity", "Thin_Liquidity_Ratio"]):
            decision = "SKIP"
            breakdown["override_reason"] = "Liquidität zu niedrig oder zu dünn"

        elif btc_change < -5.0:
            decision = "SKIP"
            breakdown["override_reason"] = "BTC fällt > 5%"

        elif decision == "BUY" and btc_change < -3.0:
            decision = "HOLD"
            breakdown["override_reason"] = "BTC fällt > 3% — downgrade zu HOLD"

        # Confidence level for position sizing
        if   fusion_score >= 80: confidence = "HIGH"
        elif fusion_score >= 70: confidence = "MEDIUM"
        else:                    confidence = "LOW"

        logger.info(
            f"Score: {fusion_score:.1f} | Entscheidung: {decision} | "
            f"Confidence: {confidence} | Override: {breakdown.get('override_reason','—')}"
        )

        return {
            "score":      round(fusion_score, 2),
            "decision":   decision,
            "confidence": confidence,
            "breakdown":  breakdown,
        }
