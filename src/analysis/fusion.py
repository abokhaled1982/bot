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
            logger.info("Connected to Redis cache for SignalFusion")
        except Exception as e:
            logger.warning(f"Could not connect to Redis, running without cache. {e}")
            self.cache = None

    def _get_cache(self, token_symbol: str) -> dict:
        if not self.cache: return None
        current_time = int(time.time())
        cache_key = f"analyse:{token_symbol}:{(current_time // 300) * 300}"
        cached_data = self.cache.get(cache_key)
        if cached_data:
            logger.info(f"Using cached analysis for {token_symbol}")
            return json.loads(cached_data)
        return None
        
    def _set_cache(self, token_symbol: str, data: dict):
        if not self.cache: return
        current_time = int(time.time())
        cache_key = f"analyse:{token_symbol}:{(current_time // 300) * 300}"
        self.cache.setex(cache_key, 300, json.dumps(data))
        
    def apply_prefilter(self, token_data: dict, chain_data: dict, market_data: dict, msgs: list) -> bool:
        """
        NEUER FILTER: Alles durchlassen!
        """
        symbol = token_data.get("symbol", "UNKNOWN")
        logger.info(f"[{symbol}] PRE-FILTER: Deaktiviert, lasse alles durch.")
        return True

    def calculate_score(self, claude_data: dict, chain_data: dict, dex_data: dict, btc_data: dict, unique_channels_5m: int = 0) -> dict:
        breakdown = {}
        
        # LOGGING: Detaillierte Scores
        logger.info(f"--- START SCORING ---")
        
        # 1. Hype-Score (Social) 20%
        hype = claude_data.get("hype_score", 0)
        hype_weighted = (hype / 100) * 20
        breakdown["hype_social"] = hype_weighted
        logger.info(f"Hype: {hype} -> {hype_weighted}")
        
        # 2. Liquidity Lock 25%
        liq_lock = 100 if chain_data.get("liquidity_locked", False) else 0
        liq_weighted = (liq_lock / 100) * 25
        breakdown["liquidity_lock"] = liq_weighted
        logger.info(f"LiqLock: {liq_lock} -> {liq_weighted}")
        
        # 3. Volume Spike 20%
        vol_spike = min(dex_data.get("volume_spike", 0) * 20, 100)
        vol_weighted = (vol_spike / 100) * 20
        breakdown["volume_spike"] = vol_weighted
        logger.info(f"VolSpike: {vol_spike} -> {vol_weighted}")
        
        # 4. Wallet-Konzentration 15%
        top_10_pct = chain_data.get("top_10_holder_percent", 100)
        wallet_conc = max(100 - top_10_pct, 0)
        wallet_weighted = (wallet_conc / 100) * 15
        breakdown["wallet_concentration"] = wallet_weighted
        logger.info(f"WalletConc: {wallet_conc} -> {wallet_weighted}")
        
        # 5. Dev-Aktivität 10%
        risks = claude_data.get("risk_flags", [])
        dev_act = 70 if "None" in risks or not risks else 30
        dev_weighted = (dev_act / 100) * 10
        breakdown["dev_activity"] = dev_weighted
        logger.info(f"DevAct: {dev_act} -> {dev_weighted}")
        
        # 6. BTC-Marktlage 10%
        btc_change = btc_data.get("btc_1h_change", 0)
        if btc_change >= 2.0: btc_score = 90
        elif btc_change <= -5.0: btc_score = 10
        else:
            normalized = (btc_change + 5) / 7
            btc_score = 10 + (normalized * 80)
        btc_weighted = (btc_score / 100) * 10
        breakdown["btc_market"] = btc_weighted
        logger.info(f"BTC: {btc_change} -> {btc_score} -> {btc_weighted}")
        
        fusion_score = sum(val for key, val in breakdown.items() if isinstance(val, (int, float)))
        logger.info(f"DEBUG: Fusion Total Score = {fusion_score}")
        
        decision = "SKIP"
        if fusion_score >= 72: decision = "BUY"
        elif fusion_score >= 45: decision = "HOLD"
        logger.info(f"DEBUG: Final Decision = {decision}")
            
        if liq_lock == 0:
            decision = "SKIP"
            breakdown["override_reason"] = "Liquidity not locked"
        elif top_10_pct > 80:
            decision = "SKIP"
            breakdown["override_reason"] = "Top 10 hold > 80%"
        elif "Pump_Suspicion" in risks or "Rugpull_Hint" in risks:
            decision = "SKIP"
            breakdown["override_reason"] = "Claude flagged critical risks"
        elif btc_change < -5.0:
            decision = "SKIP"
            breakdown["override_reason"] = "BTC dropping heavily (>5%)"
            
        if decision == "BUY" and btc_change < -3.0:
            decision = "HOLD"
            breakdown["override_reason"] = "BTC dropping > 3%, downgraded BUY to HOLD"
        
        logger.info(f"DEBUG: Post-Override Decision = {decision}")
            
        return {
            "score": round(fusion_score, 2),
            "decision": decision,
            "breakdown": breakdown
        }

