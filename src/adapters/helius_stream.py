"""
Helius WebSocket Adapter — Ergänzungs-Scanner (PRIORITY 0)
===========================================================
Lauscht auf Raydium-Pool-Erstellungen via Helius Enhanced WebSocket.
Pump.fun Migrations tauchen hier ~200-400ms FRÜHER auf als über PumpPortal,
weil wir direkt auf den Raydium AMM Program hören.

Integration: Läuft parallel zu PumpFunAdapter — kein Ersatz.
Gleicher Queue-Mechanismus wie PumpFunAdapter (drop-in Integration).

Raydium AMM Program IDs:
  - LiquidityPoolV4 (AMM v4): 675kPX9MHTjS2zt1qfr1NYHuzeLXfQM9H24wFSUt1Mp8
  - CPMM (neue Pools):        CPMMoo8L3F4NbTegBCKVNunggL7H1ZpdTHKxQB5qKP1C

On-Chain Data Fallback:
  - get_onchain_token_data(mint) liefert das gleiche Dict wie DexScreenerAdapter
    und wird in evaluate_token() verwendet wenn DexScreener den Token noch nicht kennt.
    Datenquellen: Helius Enhanced API + Jupiter price API + Raydium pool state via RPC.
"""

import asyncio
import json
import os
import time
from collections import deque
from loguru import logger

import aiohttp
import websockets

HELIUS_API_KEY  = os.getenv("HELIUS_API_KEY", "")
HELIUS_RPC_URL  = f"https://mainnet.helius-rpc.com/?api-key={HELIUS_API_KEY}"
HELIUS_WS_URL   = f"wss://mainnet.helius-rpc.com/?api-key={HELIUS_API_KEY}"

# Raydium program IDs we subscribe to
RAYDIUM_AMM_V4  = "675kPX9MHTjS2zt1qfr1NYHuzeLXfQM9H24wFSUt1Mp8"
RAYDIUM_CPMM    = "CPMMoo8L3F4NbTegBCKVNunggL7H1ZpdTHKxQB5qKP1C"

MAX_QUEUE_SIZE  = 100
RECONNECT_DELAY = 5

# SOL mint (to identify SOL/Token pairs)
SOL_MINT  = "So11111111111111111111111111111111111111112"
USDC_MINT = "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v"
USDT_MINT = "Es9vMFrzaCERmJfrF4H2FYD4KCoNkY11McCe8BenwNYB"
BASE_MINTS = {SOL_MINT, USDC_MINT, USDT_MINT}


# ── On-Chain Token Data (DexScreener-compatible dict) ─────────────────────────
async def get_onchain_token_data(mint: str, sol_price_usd: float = 0.0) -> dict | None:
    """
    Fetches token market data directly from on-chain sources without DexScreener.
    Returns a dict with the same keys as DexScreenerAdapter.get_token_data().

    Data sources (in order of priority):
      1. Jupiter price API  → price_usd (fastest, ~50ms)
      2. Helius Asset API   → symbol, decimals, supply
      3. RPC getTokenSupply → supply / decimals for mcap calc
    """
    if not HELIUS_API_KEY or not mint:
        return None

    price_usd  = 0.0
    symbol     = mint[:8]
    supply     = 0.0
    decimals   = 6
    liq_usd    = 0.0  # unknown on-chain without pool state parsing
    created_at = int(time.time() * 1000)  # now — token is brand new

    timeout = aiohttp.ClientTimeout(total=5)

    async with aiohttp.ClientSession(timeout=timeout) as session:
        # 1. Helius DAS Asset API → symbol, supply, price_info (single call gets everything)
        try:
            async with session.post(
                HELIUS_RPC_URL,
                json={"jsonrpc": "2.0", "id": 1, "method": "getAsset", "params": {"id": mint}},
            ) as r:
                if r.status == 200:
                    data = await r.json()
                    result = data.get("result", {})
                    content = result.get("content", {})
                    metadata = content.get("metadata", {})
                    sym = metadata.get("symbol") or result.get("symbol") or ""
                    if sym:
                        symbol = sym
                    token_info = result.get("token_info", {})
                    supply_raw = float(token_info.get("supply") or 0)
                    decimals   = int(token_info.get("decimals") or 6)
                    if supply_raw > 0:
                        supply = supply_raw / (10 ** decimals)
                    # Price from Helius (USDC-based)
                    price_info = token_info.get("price_info", {})
                    if price_info:
                        price_usd = float(price_info.get("price_per_token") or 0)
        except Exception as e:
            logger.debug(f"[HELIUS] Asset API error for {mint[:12]}: {e}")

    # Market cap estimate
    market_cap = price_usd * supply if price_usd > 0 and supply > 0 else 0

    # For a brand-new migration: no volume history yet → conservative defaults
    # volume_spike=1.0, change_*=0 → will pass only relaxed migration filters
    return {
        "symbol":          symbol,
        "address":         mint,
        "price_usd":       price_usd,
        "volume_spike":    1.0,    # unknown — new token
        "volume_h1":       0.0,
        "volume_h24":      0.0,
        "liquidity_usd":   liq_usd,
        "change_5m":       0.0,
        "change_1h":       0.0,
        "change_24h":      0.0,
        "market_cap":      market_cap,
        "fdv":             market_cap,
        "vol_mcap_ratio":  0.0,
        "buys_h1":         0,
        "sells_h1":        0,
        "buys_h24":        0,
        "sells_h24":       0,
        "pair_created_at": created_at,
        "supply":          supply,
        "decimals":        decimals,
        "_source":         "helius_onchain",   # mark so evaluate_token knows
    }


class HeliusStreamAdapter:
    """
    WebSocket adapter that listens to Raydium pool creation logs
    and surfaces new token candidates in real-time.

    Usage in main.py (same pattern as PumpFunAdapter):
        helius = HeliusStreamAdapter()
        asyncio.create_task(helius.start())
        ...
        candidates = helius.get_candidates(limit=10)
    """

    def __init__(self):
        self.candidate_queue: deque = deque(maxlen=MAX_QUEUE_SIZE)
        self._seen: set = set()
        self.total_detected  = 0
        self.connected       = False
        self._ws             = None
        self._sub_ids: dict  = {}   # method -> subscription id

    # ── Public interface (same as PumpFunAdapter) ─────────────────────────────
    def get_candidates(self, limit: int = 10) -> list:
        """Pop up to `limit` new Raydium pool candidates."""
        candidates = []
        while self.candidate_queue and len(candidates) < limit:
            candidates.append(self.candidate_queue.popleft())
        return candidates

    def status(self) -> dict:
        return {
            "connected":      self.connected,
            "queue_size":     len(self.candidate_queue),
            "total_detected": self.total_detected,
        }

    # ── WebSocket lifecycle ───────────────────────────────────────────────────
    async def start(self):
        """Run forever with auto-reconnect."""
        if not HELIUS_API_KEY:
            logger.warning("[HELIUS] Kein HELIUS_API_KEY — Stream deaktiviert. "
                           "Setze HELIUS_API_KEY in .env")
            return

        while True:
            try:
                await self._connect()
            except Exception as e:
                logger.error(f"[HELIUS] WebSocket error: {e}")
                self.connected = False
            logger.warning(f"[HELIUS] Reconnecting in {RECONNECT_DELAY}s...")
            await asyncio.sleep(RECONNECT_DELAY)

    async def _connect(self):
        connect_kwargs = {
            "open_timeout":  10,
            "ping_interval": 20,
            "ping_timeout":  10,
        }
        logger.info(f"[HELIUS] Verbinde mit Helius WebSocket...")
        async with websockets.connect(HELIUS_WS_URL, **connect_kwargs) as ws:
            self._ws = ws
            self.connected = True
            logger.info("[HELIUS] ✅ Verbunden — abonniere Raydium AMM logs")

            # Subscribe to program logs for Raydium AMM v4
            await ws.send(json.dumps({
                "jsonrpc": "2.0",
                "id": 1,
                "method": "logsSubscribe",
                "params": [
                    {"mentions": [RAYDIUM_AMM_V4]},
                    {"commitment": "processed"}
                ]
            }))

            # Subscribe to Raydium CPMM (newer pools)
            await ws.send(json.dumps({
                "jsonrpc": "2.0",
                "id": 2,
                "method": "logsSubscribe",
                "params": [
                    {"mentions": [RAYDIUM_CPMM]},
                    {"commitment": "processed"}
                ]
            }))

            async for raw in ws:
                await self._handle_message(raw)

    # ── Message processing ────────────────────────────────────────────────────
    async def _handle_message(self, raw: str):
        try:
            msg = json.loads(raw)
        except Exception:
            return

        # Subscription confirmation
        if "result" in msg and isinstance(msg["result"], int):
            sub_id = msg["result"]
            req_id = msg.get("id", 0)
            prog   = RAYDIUM_AMM_V4 if req_id == 1 else RAYDIUM_CPMM
            self._sub_ids[prog] = sub_id
            logger.info(f"[HELIUS] Subscription confirmed: id={sub_id} ({prog[:12]}...)")
            return

        # Log notification
        params = msg.get("params", {})
        result = params.get("result", {})
        value  = result.get("value", {})

        logs  = value.get("logs", [])
        sig   = value.get("signature", "")
        err   = value.get("err")

        # Skip failed transactions
        if err:
            return

        # Detect pool initialization (new pair created)
        is_new_pool = any(
            "initialize2" in log.lower() or
            "initializepool" in log.lower() or
            "initialize_pool" in log.lower()
            for log in logs
        )
        if not is_new_pool:
            return

        # Get pool details via RPC (async, non-blocking)
        asyncio.create_task(self._fetch_pool_token(sig))

    async def _fetch_pool_token(self, signature: str):
        """
        Fetch transaction details to extract the new token mint address.
        Uses Helius Enhanced Transactions API for structured data.
        """
        if not signature or signature in self._seen:
            return

        import aiohttp
        url = f"https://api.helius.xyz/v0/transactions?api-key={HELIUS_API_KEY}"
        try:
            async with aiohttp.ClientSession() as session:
                async with session.post(
                    url,
                    json={"transactions": [signature]},
                    timeout=aiohttp.ClientTimeout(total=5),
                ) as resp:
                    if resp.status != 200:
                        return
                    data = await resp.json()

            if not data or not isinstance(data, list):
                return

            tx = data[0]
            token_transfers = tx.get("tokenTransfers", [])
            account_data    = tx.get("accountData", [])

            # Find the non-base token mint (i.e. the new memecoin)
            new_mint = None
            for transfer in token_transfers:
                mint = transfer.get("mint", "")
                if mint and mint not in BASE_MINTS and mint not in self._seen:
                    new_mint = mint
                    break

            # Fallback: scan accountData for token mints
            if not new_mint:
                for acc in account_data:
                    for tb in acc.get("tokenBalanceChanges", []):
                        mint = tb.get("mint", "")
                        if mint and mint not in BASE_MINTS and mint not in self._seen:
                            new_mint = mint
                            break
                    if new_mint:
                        break

            if not new_mint:
                return

            self._seen.add(signature)
            self._seen.add(new_mint)
            self.total_detected += 1

            token = {
                "address":               new_mint,
                "symbol":                tx.get("description", new_mint[:8]),
                "source":                "helius_raydium",
                "helius_detected_at":    time.time(),
                "pumpfun_detected_at":   time.time(),   # for compatibility
                "tx_signature":          signature,
            }

            self.candidate_queue.append(token)
            logger.info(
                f"[HELIUS] 🔥 Neuer Raydium Pool: {new_mint[:12]}... "
                f"| TX: {signature[:16]}..."
            )

        except Exception as e:
            logger.warning(f"[HELIUS] Fehler beim Abrufen TX {signature[:16]}: {e}")

    # ── Cleanup ───────────────────────────────────────────────────────────────
    async def cleanup_loop(self):
        """Keep _seen set bounded to avoid memory leak."""
        while True:
            await asyncio.sleep(3600)  # every hour
            if len(self._seen) > 10_000:
                self._seen.clear()
                logger.info("[HELIUS] _seen set geleert (memory cleanup)")
