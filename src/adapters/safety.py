import asyncio
import os
import sqlite3
from datetime import datetime
from loguru import logger
from solana.rpc.async_api import AsyncClient
from solders.pubkey import Pubkey
from spl.token.client import Token
from spl.token.constants import TOKEN_PROGRAM_ID
import aiohttp

# RugCheck score thresholds:
#   0-500    = Good (low risk)
#   500-2000 = Warning
#   2000+    = Danger (high risk / probable scam)
RUGCHECK_MAX_SCORE = 2000
RUGCHECK_TIMEOUT   = 6  # seconds


class SafetyAdapter:
    def __init__(self):
        self.rpc_url = os.getenv("SOLANA_RPC_URL", "https://api.mainnet-beta.solana.com")
        self.client = AsyncClient(self.rpc_url)

    async def _rugcheck(self, token_address: str) -> dict | None:
        """
        Query RugCheck API for token risk report.
        Free, no API key, ~200ms response time.
        Returns summary dict or None on failure.
        """
        url = f"https://api.rugcheck.xyz/v1/tokens/{token_address}/report/summary"
        try:
            async with aiohttp.ClientSession() as session:
                async with session.get(
                    url, timeout=aiohttp.ClientTimeout(total=RUGCHECK_TIMEOUT),
                ) as r:
                    if r.status == 200:
                        return await r.json()
                    logger.debug(f"[SAFETY] RugCheck HTTP {r.status} for {token_address[:12]}")
        except Exception as e:
            logger.debug(f"[SAFETY] RugCheck error for {token_address[:12]}: {e}")
        return None

    async def get_safety_details(self, token_address: str) -> dict:
        try:
            if token_address.startswith("0x"):
                return {"is_safe": False, "reason": "EVM Address"}
            pubkey = Pubkey.from_string(token_address)

            # Run on-chain check and RugCheck in parallel
            rpc_task = self.client.get_account_info(pubkey)
            rug_task = self._rugcheck(token_address)
            resp, rug_data = await asyncio.gather(rpc_task, rug_task)

            # ── On-chain mint authority check ─────────────────────────────────
            if not resp.value or not resp.value.data:
                return {"is_safe": False, "reason": "No Account Info", "address": token_address}

            data = resp.value.data
            mint_auth_option = int.from_bytes(data[0:4], "little")
            is_revoked       = (mint_auth_option == 0)
            mint_auth_pubkey = data[4:36]

            result = {
                "mint_authority": "Revoked" if is_revoked else "Active",
                "authority_hex":  mint_auth_pubkey.hex(),
                "total_supply":   int.from_bytes(data[36:44], "little"),
            }

            # ── RugCheck enrichment ───────────────────────────────────────────
            if rug_data:
                rug_score    = int(rug_data.get("score", 9999))
                lp_locked    = float(rug_data.get("lpLockedPct", 0) or 0)
                risks        = rug_data.get("risks", [])
                danger_risks = [r for r in risks if r.get("level") == "danger"]
                warn_risks   = [r for r in risks if r.get("level") == "warn"]

                result["rugcheck_score"]     = rug_score
                result["rugcheck_lp_locked"] = round(lp_locked, 2)
                result["rugcheck_dangers"]   = [r.get("name", "") for r in danger_risks]
                result["rugcheck_warnings"]  = [r.get("name", "") for r in warn_risks]

                # Combined safety decision:
                #   - Mint must be revoked (or RugCheck score very low)
                #   - RugCheck score must be below threshold
                #   - No "danger" level risks
                is_safe = (
                    (is_revoked or rug_score <= 100)
                    and rug_score <= RUGCHECK_MAX_SCORE
                    and len(danger_risks) == 0
                )
                result["is_safe"] = is_safe

                if not is_safe:
                    reasons = []
                    if not is_revoked and rug_score > 100:
                        reasons.append("Mint active")
                    if rug_score > RUGCHECK_MAX_SCORE:
                        reasons.append(f"RugCheck score {rug_score}")
                    if danger_risks:
                        reasons.append(f"Dangers: {', '.join(r.get('name','') for r in danger_risks)}")
                    result["reason"] = " | ".join(reasons)

                logger.info(
                    f"[SAFETY] {token_address[:12]}... Mint:{'Revoked' if is_revoked else 'Active'} "
                    f"RugScore:{rug_score} LP:{lp_locked:.0f}% "
                    f"Dangers:{len(danger_risks)} Warns:{len(warn_risks)} → "
                    f"{'SAFE' if is_safe else 'UNSAFE'}"
                )
            else:
                # RugCheck unavailable — fall back to mint authority only
                result["is_safe"] = is_revoked
                result["rugcheck_score"] = None
                if not is_revoked:
                    result["reason"] = "Mint active (RugCheck unavailable)"

            return result

        except Exception as e:
            logger.error(f"Safety check error: {e}")
            return {"is_safe": False, "reason": str(e), "address": token_address}

    async def is_safe(self, token_address: str) -> bool:
        details = await self.get_safety_details(token_address)
        return details.get("is_safe", False)
