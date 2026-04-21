"""
src/execution/executor.py — Professional Trade Executor

Responsibilities:
  • Build Jupiter buy/sell quotes with multi-endpoint fallback
  • Sign and send transactions via multi-RPC fallback
  • Wait for on-chain confirmation with timeout
  • Log every trade action to the trades table
  • Emit structured events to bot_events table

Public API: TradeExecutor.execute_trade()
"""
from __future__ import annotations

import os
import base58
import base64
import sqlite3
import asyncio
import json as _json
import uuid as _uuid
import aiohttp
import requests
from datetime import datetime
from typing import Optional
from loguru import logger
from dotenv import load_dotenv
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry
from solders.keypair import Keypair
from solders.transaction import VersionedTransaction

from src.execution import events as _events

load_dotenv()

# ── Configuration ─────────────────────────────────────────────────────────────
SOL_MINT         = "So11111111111111111111111111111111111111112"
JUPITER_TIMEOUT  = int(os.getenv("JUPITER_TIMEOUT",  "12"))
JUPITER_RETRIES  = int(os.getenv("JUPITER_RETRIES",  "3"))
CONFIRM_TIMEOUT  = int(os.getenv("CONFIRM_TIMEOUT", "90"))

# Jupiter API endpoints — primary first, fallbacks after
JUPITER_QUOTE_URLS = [
    "https://api.jup.ag/swap/v1/quote",
    "https://quote-api.jup.ag/v6/quote",
]
JUPITER_SWAP_URLS = [
    "https://api.jup.ag/swap/v1/swap",
    "https://quote-api.jup.ag/v6/swap",
]

# Jupiter Ultra API (preferred — handles send + confirm in one call)
JUPITER_ULTRA_ORDER_URL   = "https://api.jup.ag/ultra/v1/order"
JUPITER_ULTRA_EXECUTE_URL = "https://api.jup.ag/ultra/v1/execute"
ULTRA_EXECUTE_TIMEOUT     = int(os.getenv("ULTRA_EXECUTE_TIMEOUT", "30"))

# Solana RPC endpoints — primary first, fallbacks after
_RPC_ENV = os.getenv("SOLANA_RPC_URL", "https://api.mainnet-beta.solana.com")
RPC_ENDPOINTS: list[str] = list(dict.fromkeys([
    _RPC_ENV,
    "https://api.mainnet-beta.solana.com",
    "https://solana-rpc.publicnode.com",
    "https://solana.publicnode.com",
]))

_TOKEN_PROGRAMS = [
    "TokenzQdBNbLqP5VEhdkAS6EPFLC1PHnBqCXEpPxuEb",   # Token-2022 (pump.fun)
    "TokenkegQfeZyiNwAJbNbGKPFXCWuBvf9Ss623VQ5DA",   # legacy SPL
]


# ── Module-level helpers ──────────────────────────────────────────────────────

def _build_http_session() -> requests.Session:
    session = requests.Session()
    adapter = HTTPAdapter(max_retries=Retry(
        total=JUPITER_RETRIES,
        backoff_factor=1,
        status_forcelist=[429, 500, 502, 503, 504],
        allowed_methods=["GET", "POST"],
        raise_on_status=False,
    ))
    session.mount("https://", adapter)
    session.mount("http://",  adapter)
    return session


def _sol_price_sync(http: requests.Session) -> float:
    """Fetch SOL/USD with CoinGecko primary → DexScreener fallback."""
    try:
        r = http.get(
            "https://api.coingecko.com/api/v3/simple/price?ids=solana&vs_currencies=usd",
            timeout=8,
        )
        return float(r.json()["solana"]["usd"])
    except Exception:
        pass
    try:
        r = http.get(
            f"https://api.dexscreener.com/latest/dex/tokens/{SOL_MINT}",
            timeout=8,
        )
        pairs = r.json().get("pairs", [])
        if pairs:
            return float(pairs[0].get("priceUsd", 150.0))
    except Exception:
        pass
    logger.warning("[EXECUTOR] SOL price unavailable — using $150 fallback")
    return 150.0


def _is_jupiter_reachable() -> bool:
    import socket
    try:
        socket.getaddrinfo("api.jup.ag", 443)
        return True
    except Exception:
        return False


# ── Transaction primitives ────────────────────────────────────────────────────

async def _get_quote(
    http:         requests.Session,
    input_mint:   str,
    output_mint:  str,
    amount:       int,
    slippage_bps: int,
) -> dict:
    """
    Fetch a Jupiter swap quote with endpoint fallback.
    Raises RuntimeError if all endpoints fail.
    """
    last_err = ""
    for url in JUPITER_QUOTE_URLS:
        try:
            r = http.get(
                url,
                params={
                    "inputMint":   input_mint,
                    "outputMint":  output_mint,
                    "amount":      amount,
                    "slippageBps": slippage_bps,
                },
                timeout=JUPITER_TIMEOUT,
            )
            if r.status_code == 200:
                data = r.json()
                if "outAmount" in data:
                    return data
                last_err = f"missing outAmount: {list(data.keys())}"
            else:
                last_err = f"HTTP {r.status_code}: {r.text[:80]}"
        except Exception as e:
            last_err = str(e)
            logger.warning(f"[QUOTE] {url} failed: {e}")
    raise RuntimeError(f"All Jupiter quote endpoints failed. Last error: {last_err}")


async def _build_swap_tx(
    http:          requests.Session,
    quote_data:    dict,
    wallet_pubkey: str,
) -> str:
    """
    Build a swap transaction via Jupiter (multi-endpoint fallback).
    Returns base64-encoded transaction string.
    """
    payload = {
        "quoteResponse":             quote_data,
        "userPublicKey":             wallet_pubkey,
        "wrapAndUnwrapSol":          True,
        "dynamicComputeUnitLimit":   True,
        "prioritizationFeeLamports": "auto",
    }
    last_err = ""
    for url in JUPITER_SWAP_URLS:
        try:
            r = http.post(url, json=payload, timeout=JUPITER_TIMEOUT)
            if r.status_code == 200:
                data = r.json()
                if "swapTransaction" in data:
                    return data["swapTransaction"]
                last_err = f"missing swapTransaction: {list(data.keys())}"
            else:
                last_err = f"HTTP {r.status_code}: {r.text[:80]}"
        except Exception as e:
            last_err = str(e)
            logger.warning(f"[SWAP-BUILD] {url} failed: {e}")
    raise RuntimeError(f"All Jupiter swap endpoints failed. Last error: {last_err}")


async def _send_transaction(signed_tx_bytes: bytes) -> str:
    """
    Broadcast a signed TX to all RPC endpoints (first success wins).
    Returns the transaction signature.
    """
    tx_b64 = base64.b64encode(signed_tx_bytes).decode()
    payload = {
        "jsonrpc": "2.0", "id": 1,
        "method": "sendTransaction",
        "params": [tx_b64, {
            "encoding":            "base64",
            "skipPreflight":       False,
            "preflightCommitment": "confirmed",
            "maxRetries":          5,
        }],
    }
    async with aiohttp.ClientSession() as session:
        for endpoint in RPC_ENDPOINTS:
            try:
                async with session.post(
                    endpoint, json=payload,
                    timeout=aiohttp.ClientTimeout(total=15),
                ) as resp:
                    data = await resp.json()
                    if "error" in data:
                        err_code = data["error"].get("code", 0)
                        if err_code == 429:
                            logger.warning(f"[TX] 429 on {endpoint} → next RPC")
                            continue
                        raise RuntimeError(f"RPC error: {data['error'].get('message')}")
                    tx_id = data.get("result")
                    if tx_id:
                        return tx_id
                    logger.warning(f"[TX] No TX id from {endpoint}: {data}")
            except aiohttp.ClientError as e:
                logger.warning(f"[TX] Connection error {endpoint}: {e}")
    raise RuntimeError("All RPC send endpoints failed")


async def _confirm_transaction(tx_id: str, timeout_sec: int = CONFIRM_TIMEOUT) -> tuple[bool, str]:
    """
    Poll all RPC endpoints until TX is confirmed/finalized or timeout.
    Returns (confirmed, error_message).
    """
    poll_payload = {
        "jsonrpc": "2.0", "id": 1,
        "method": "getSignatureStatuses",
        "params": [[tx_id], {"searchTransactionHistory": True}],
    }
    deadline      = asyncio.get_event_loop().time() + timeout_sec
    poll_interval = 4

    async with aiohttp.ClientSession() as session:
        while asyncio.get_event_loop().time() < deadline:
            for endpoint in RPC_ENDPOINTS:
                try:
                    async with session.post(
                        endpoint, json=poll_payload,
                        timeout=aiohttp.ClientTimeout(total=8),
                    ) as resp:
                        data     = await resp.json()
                        statuses = (data.get("result") or {}).get("value", [None])
                        status   = statuses[0] if statuses else None
                        if status is None:
                            continue
                        if status.get("err") is not None:
                            return False, f"TX on-chain error: {status['err']}"
                        if status.get("confirmationStatus") in ("confirmed", "finalized"):
                            return True, ""
                        break   # TX is visible, still processing — wait
                except Exception:
                    continue
            await asyncio.sleep(poll_interval)

    # Final sweep
    async with aiohttp.ClientSession() as session:
        for endpoint in RPC_ENDPOINTS:
            try:
                async with session.post(
                    endpoint, json=poll_payload,
                    timeout=aiohttp.ClientTimeout(total=12),
                ) as resp:
                    data     = await resp.json()
                    statuses = (data.get("result") or {}).get("value", [None])
                    status   = statuses[0] if statuses else None
                    if not status:
                        continue
                    if status.get("err") is not None:
                        return False, f"TX error: {status['err']}"
                    if status.get("confirmationStatus") in ("confirmed", "finalized"):
                        return True, ""
            except Exception:
                continue
    return False, f"TX not confirmed after {timeout_sec}s — check solscan.io/tx/{tx_id}"


# ── Main Executor class ───────────────────────────────────────────────────────

class TradeExecutor:

    def __init__(self):
        self.dry_run          = os.getenv("DRY_RUN", "True").lower() == "true"
        self.max_position_usd = float(os.getenv("TRADE_MAX_POSITION_USD", "0.20"))
        self.min_position_usd = float(os.getenv("TRADE_MIN_POSITION_USD", "0.10"))
        self.db_path          = "memecoin_bot.db"
        self.http             = _build_http_session()
        self.keypair: Optional[Keypair] = None

        pk = os.getenv("SOLANA_PRIVATE_KEY", "")
        if pk:
            try:
                decoded = base58.b58decode(pk.strip())
                self.keypair = (
                    Keypair.from_bytes(decoded) if len(decoded) == 64
                    else Keypair.from_seed(decoded)
                )
                logger.info(f"[EXECUTOR] Wallet: {self.keypair.pubkey()}")
            except Exception as e:
                logger.error(f"[EXECUTOR] Keypair load error: {e}")

        _events.init(self.db_path)

        if not self.dry_run:
            if _is_jupiter_reachable():
                logger.info("✅ Jupiter reachable — live trading active")
            else:
                logger.warning("⚠️ Jupiter unreachable → DRY_RUN activated")
                self.dry_run = True

    # ── Position sizing / slippage ────────────────────────────────────────────

    def _position_size(
        self, confidence: str, override: Optional[float] = None
    ) -> float:
        if override:
            return override
        if   confidence == "HIGH":   return self.max_position_usd
        elif confidence == "MEDIUM": return (self.max_position_usd + self.min_position_usd) / 2
        else:                        return self.min_position_usd

    def _slippage_bps(self, liquidity_usd: float) -> int:
        if   liquidity_usd >= 500_000: return 100
        elif liquidity_usd >= 100_000: return 200
        elif liquidity_usd >= 50_000:  return 300
        elif liquidity_usd >= 20_000:  return 500
        else:                          return 800

    # ── Dry-run sell simulation ───────────────────────────────────────────────

    def _simulate_sell_amount(
        self,
        token_address: str,
        position_size: float,
        sell_fraction: float,
        current_price: float,
    ) -> float:
        """
        Calculate the simulated USD received for a dry-run SELL.
        Reads entry_price from positions.json to compute realistic P/L.
        """
        try:
            with open("positions.json") as f:
                positions = _json.load(f)
            entry_price = float(positions.get(token_address, {}).get("entry_price", 0))
        except Exception:
            entry_price = 0.0

        if entry_price > 0 and current_price > 0:
            return position_size * sell_fraction * (current_price / entry_price)
        # Fallback: assume break-even
        return position_size * sell_fraction

    # ── Token balance (multi-RPC + multi-program fallback) ───────────────────

    def _get_token_balance(
        self, token_address: str, sell_fraction: float
    ) -> tuple[int, bool]:
        """Return (raw_amount_to_sell, success)."""
        wallet = str(self.keypair.pubkey())
        for rpc in RPC_ENDPOINTS:
            for program in _TOKEN_PROGRAMS:
                try:
                    resp = self.http.post(
                        rpc,
                        json={
                            "jsonrpc": "2.0", "id": 1,
                            "method": "getTokenAccountsByOwner",
                            "params": [wallet, {"programId": program}, {"encoding": "jsonParsed"}],
                        },
                        timeout=8,
                    )
                    if not resp.text.strip():
                        continue
                    for acc in resp.json().get("result", {}).get("value", []):
                        info = acc["account"]["data"]["parsed"]["info"]
                        if info["mint"] != token_address:
                            continue
                        decimals  = int(info["tokenAmount"]["decimals"])
                        ui_amount = float(info["tokenAmount"]["uiAmount"] or 0)
                        if ui_amount <= 0:
                            return 0, False
                        raw_total = int(ui_amount * (10 ** decimals))
                        raw_sell  = int(raw_total * sell_fraction)
                        logger.info(
                            f"[EXECUTOR] Balance {ui_amount:.4f} (dec={decimals}) "
                            f"→ sell {sell_fraction:.0%} = {raw_sell} raw"
                        )
                        return raw_sell, True
                except Exception as e:
                    logger.debug(f"[EXECUTOR] balance check {rpc[:28]}: {e}")
        logger.warning(f"[EXECUTOR] No balance found for {token_address} on any RPC/program")
        return 0, False

    # ── Jupiter Ultra API (handles order + sign + send + confirm) ────────────

    def _ultra_swap(
        self, input_mint: str, output_mint: str, amount: int,
        slippage_bps: int = 300,
    ) -> dict:
        """
        Execute a swap via Jupiter Ultra API.
        Jupiter handles TX sending + confirmation — no manual RPC polling.
        Returns {"status":"Success","signature":...,"totalInputAmount":...,...}
        """
        wallet = str(self.keypair.pubkey())

        r = self.http.get(
            JUPITER_ULTRA_ORDER_URL,
            params={
                "inputMint":  input_mint,
                "outputMint": output_mint,
                "amount":     str(amount),
                "taker":      wallet,
                "slippageBps": str(slippage_bps),
            },
            timeout=JUPITER_TIMEOUT,
        )
        if r.status_code != 200:
            raise RuntimeError(f"Ultra order failed: HTTP {r.status_code} {r.text[:80]}")

        order = r.json()
        if "transaction" not in order or "requestId" not in order:
            raise RuntimeError(f"Ultra order missing fields: {list(order.keys())}")

        req_id     = order["requestId"]
        raw_tx     = base64.b64decode(order["transaction"])
        tx         = VersionedTransaction.from_bytes(raw_tx)
        signed_tx  = VersionedTransaction(tx.message, [self.keypair])
        signed_b64 = base64.b64encode(bytes(signed_tx)).decode()

        r2 = self.http.post(
            JUPITER_ULTRA_EXECUTE_URL,
            json={
                "signedTransaction": signed_b64,
                "requestId":         req_id,
            },
            timeout=ULTRA_EXECUTE_TIMEOUT,
        )
        if r2.status_code != 200:
            raise RuntimeError(f"Ultra execute failed: HTTP {r2.status_code} {r2.text[:80]}")

        result = r2.json()
        if result.get("status") != "Success":
            err = result.get("error") or result.get("code") or result.get("status")
            raise RuntimeError(f"Ultra execute error: {err}")

        return result

    # ── Internal buy / sell ───────────────────────────────────────────────────

    async def _execute_buy(
        self,
        symbol:        str,
        address:       str,
        position_size: float,
        slippage_bps:  int,
    ) -> dict:
        sol_price       = _sol_price_sync(self.http)
        amount_lamports = int((position_size / sol_price) * 1_000_000_000)

        logger.info(
            f"[BUY] ${position_size:.4f} = {amount_lamports:,} lamports "
            f"(SOL ${sol_price:.2f}) | slippage {slippage_bps / 100:.1f}%"
        )

        # ── Primary: Jupiter Ultra API (send + confirm in one call) ───────────
        try:
            result = await asyncio.to_thread(
                self._ultra_swap, SOL_MINT, address, amount_lamports, slippage_bps,
            )
            tx_id        = result["signature"]
            in_amount    = int(result.get("totalInputAmount", amount_lamports))
            sol_price_now = _sol_price_sync(self.http)
            actual_buy_usd = in_amount / 1e9 * sol_price_now
            logger.success(f"[BUY] ✅ Ultra confirmed | paid ${actual_buy_usd:.4f} | TX: {tx_id}")
            return {
                "status":          "success",
                "tx":              tx_id,
                "buy_amount_usd":  actual_buy_usd,
                "sell_amount_usd": None,
                "tx_status":       "confirmed",
            }
        except Exception as e:
            logger.warning(f"[BUY] Ultra API failed: {e} — falling back to legacy Swap API")

        # ── Fallback: legacy Swap API + manual send/confirm ───────────────────
        quote_data  = await _get_quote(self.http, SOL_MINT, address, amount_lamports, slippage_bps)
        logger.info(f"[BUY] Quote: {quote_data['inAmount']} lam → {quote_data['outAmount']} {symbol}")

        swap_tx_b64 = await _build_swap_tx(self.http, quote_data, str(self.keypair.pubkey()))
        raw_tx      = base64.b64decode(swap_tx_b64)
        tx          = VersionedTransaction.from_bytes(raw_tx)
        signed_tx   = VersionedTransaction(tx.message, [self.keypair])

        tx_id = await _send_transaction(bytes(signed_tx))
        logger.info(f"[BUY] TX sent → https://solscan.io/tx/{tx_id}")

        confirmed, err = await _confirm_transaction(tx_id)
        if not confirmed:
            logger.error(f"[BUY] TX unconfirmed: {err}")
            return {"status": "error", "message": err, "tx": tx_id, "tx_status": "unconfirmed"}

        sol_price_confirmed = _sol_price_sync(self.http)
        actual_buy_usd = int(quote_data.get("inAmount", 0)) / 1e9 * sol_price_confirmed
        logger.success(f"[BUY] ✅ Confirmed | paid ${actual_buy_usd:.4f} | TX: {tx_id}")

        return {
            "status":          "success",
            "tx":              tx_id,
            "buy_amount_usd":  actual_buy_usd,
            "sell_amount_usd": None,
            "tx_status":       "confirmed",
        }

    async def _execute_sell(
        self,
        symbol:        str,
        address:       str,
        sell_fraction: float,
        slippage_bps:  int,
    ) -> dict:
        # Retry balance check up to 3 times (handles RPC cache delay after buy)
        raw_amount, found = 0, False
        for attempt in range(3):
            raw_amount, found = self._get_token_balance(address, sell_fraction)
            if found and raw_amount > 0:
                break
            if attempt < 2:
                logger.debug(f"[SELL] Balance not found, retry {attempt+1}/3 in 3s...")
                await asyncio.sleep(3)

        if not found or raw_amount <= 0:
            msg = f"No token balance for {symbol} in wallet"
            logger.error(f"[SELL] {msg}")
            return {"status": "error", "message": msg}

        # ── Primary: Jupiter Ultra API ────────────────────────────────────────
        try:
            result = await asyncio.to_thread(
                self._ultra_swap, address, SOL_MINT, raw_amount, slippage_bps,
            )
            tx_id         = result["signature"]
            out_lamports  = int(result.get("totalOutputAmount", 0))
            sol_price_now = _sol_price_sync(self.http)
            actual_sell_usd = out_lamports / 1e9 * sol_price_now
            logger.success(f"[SELL] ✅ Ultra confirmed | received ${actual_sell_usd:.4f} | TX: {tx_id}")
            return {
                "status":          "success",
                "tx":              tx_id,
                "buy_amount_usd":  None,
                "sell_amount_usd": actual_sell_usd,
                "tx_status":       "confirmed",
            }
        except Exception as e:
            logger.warning(f"[SELL] Ultra API failed: {e} — falling back to legacy Swap API")

        # ── Fallback: legacy Swap API ─────────────────────────────────────────
        quote_data = await _get_quote(self.http, address, SOL_MINT, raw_amount, slippage_bps)
        sol_out    = int(quote_data.get("outAmount", 0)) / 1e9
        logger.info(f"[SELL] Quote: {raw_amount} {symbol} → {sol_out:.6f} SOL")

        swap_tx_b64 = await _build_swap_tx(self.http, quote_data, str(self.keypair.pubkey()))
        raw_tx      = base64.b64decode(swap_tx_b64)
        tx          = VersionedTransaction.from_bytes(raw_tx)
        signed_tx   = VersionedTransaction(tx.message, [self.keypair])

        tx_id = await _send_transaction(bytes(signed_tx))
        logger.info(f"[SELL] TX sent → https://solscan.io/tx/{tx_id}")

        confirmed, err = await _confirm_transaction(tx_id)
        if not confirmed:
            logger.error(f"[SELL] TX unconfirmed: {err}")
            return {"status": "error", "message": err, "tx": tx_id, "tx_status": "unconfirmed"}

        sol_price_confirmed = _sol_price_sync(self.http)
        actual_sell_usd     = sol_out * sol_price_confirmed
        logger.success(f"[SELL] ✅ Confirmed | received ${actual_sell_usd:.4f} | TX: {tx_id}")

        return {
            "status":          "success",
            "tx":              tx_id,
            "buy_amount_usd":  None,
            "sell_amount_usd": actual_sell_usd,
            "tx_status":       "confirmed",
        }

    # ── DB logging ────────────────────────────────────────────────────────────

    # All columns that should exist in the trades table (auto-migrated)
    _EXTRA_COLUMNS = [
        # Original extras
        ("pair_created_at",   "INTEGER"),
        ("tx_signature",      "TEXT"),
        ("tx_status",         "TEXT"),
        ("buy_amount_usd",    "REAL"),
        ("sell_amount_usd",   "REAL"),
        # Discovery
        ("source",            "TEXT"),
        ("dex_url",           "TEXT"),
        # DexScreener market data
        ("market_cap",        "REAL"),
        ("fdv",               "REAL"),
        ("liquidity_usd",     "REAL"),
        ("volume_h1",         "REAL"),
        ("volume_h24",        "REAL"),
        ("volume_spike",      "REAL"),
        ("change_5m",         "REAL"),
        ("change_1h",         "REAL"),
        ("change_24h",        "REAL"),
        ("vol_mcap_ratio",    "REAL"),
        ("buys_h1",           "INTEGER"),
        ("sells_h1",          "INTEGER"),
        ("buys_h24",          "INTEGER"),
        ("sells_h24",         "INTEGER"),
        # Safety (RugCheck)
        ("mint_authority",    "TEXT"),
        ("rugcheck_score",    "INTEGER"),
        ("rugcheck_lp_locked","REAL"),
        ("rugcheck_dangers",  "TEXT"),
        ("rugcheck_warnings", "TEXT"),
        # On-chain data
        ("top_10_holder_pct", "REAL"),
        ("holder_count",      "INTEGER"),
        ("liquidity_locked",  "INTEGER"),
        # Raydium
        ("raydium_vol_24h",   "REAL"),
        ("raydium_tvl",       "REAL"),
        ("raydium_burn_pct",  "REAL"),
        # Scoring breakdown
        ("confidence",        "TEXT"),
        ("hype_score",        "INTEGER"),
        ("risk_flags",        "TEXT"),
        ("fusion_hype",       "REAL"),
        ("fusion_liq_lock",   "REAL"),
        ("fusion_vol_spike",  "REAL"),
        ("fusion_wallet",     "REAL"),
        ("fusion_buy_sell",   "REAL"),
        ("fusion_vol_mcap",   "REAL"),
        ("fusion_risk",       "REAL"),
        ("fusion_btc",        "REAL"),
        ("fusion_override",   "TEXT"),
        # Token age
        ("token_age_hours",   "REAL"),
    ]
    _migrated_dbs: set = set()  # track which DB paths have been migrated

    def _log_to_db(
        self,
        symbol, address, price, size, score, decision,
        rejection_reason=None, ai_reasoning=None, funnel_stage="FINAL",
        gates_passed=None, pair_created_at=None,
        tx_signature=None, tx_status=None,
        buy_amount_usd=None, sell_amount_usd=None,
        # ── Extended fields ────────────────────────────────────────────
        source=None, dex_url=None,
        market_cap=None, fdv=None, liquidity_usd=None,
        volume_h1=None, volume_h24=None, volume_spike=None,
        change_5m=None, change_1h=None, change_24h=None,
        vol_mcap_ratio=None,
        buys_h1=None, sells_h1=None, buys_h24=None, sells_h24=None,
        mint_authority=None, rugcheck_score=None, rugcheck_lp_locked=None,
        rugcheck_dangers=None, rugcheck_warnings=None,
        top_10_holder_pct=None, holder_count=None, liquidity_locked=None,
        raydium_vol_24h=None, raydium_tvl=None, raydium_burn_pct=None,
        confidence=None, hype_score=None, risk_flags=None,
        fusion_hype=None, fusion_liq_lock=None, fusion_vol_spike=None,
        fusion_wallet=None, fusion_buy_sell=None, fusion_vol_mcap=None,
        fusion_risk=None, fusion_btc=None, fusion_override=None,
        token_age_hours=None,
    ) -> None:
        try:
            conn = sqlite3.connect(self.db_path)
            c    = conn.cursor()
            # Auto-migrate missing columns (once per DB path per process)
            if self.db_path not in TradeExecutor._migrated_dbs:
                for col, dtype in self._EXTRA_COLUMNS:
                    try:
                        c.execute(f"ALTER TABLE trades ADD COLUMN {col} {dtype}")
                        conn.commit()
                    except Exception:
                        pass
                TradeExecutor._migrated_dbs.add(self.db_path)

            c.execute(
                "INSERT INTO trades "
                "(token_address, symbol, entry_price, position_size, score, decision, "
                " rejection_reason, ai_reasoning, funnel_stage, timestamp, gates_passed, "
                " pair_created_at, tx_signature, tx_status, buy_amount_usd, sell_amount_usd, "
                " source, dex_url, market_cap, fdv, liquidity_usd, "
                " volume_h1, volume_h24, volume_spike, "
                " change_5m, change_1h, change_24h, vol_mcap_ratio, "
                " buys_h1, sells_h1, buys_h24, sells_h24, "
                " mint_authority, rugcheck_score, rugcheck_lp_locked, "
                " rugcheck_dangers, rugcheck_warnings, "
                " top_10_holder_pct, holder_count, liquidity_locked, "
                " raydium_vol_24h, raydium_tvl, raydium_burn_pct, "
                " confidence, hype_score, risk_flags, "
                " fusion_hype, fusion_liq_lock, fusion_vol_spike, "
                " fusion_wallet, fusion_buy_sell, fusion_vol_mcap, "
                " fusion_risk, fusion_btc, fusion_override, "
                " token_age_hours) "
                "VALUES (" + ",".join(["?"] * 56) + ")",
                (address, symbol, price, size, score, decision,
                 rejection_reason, ai_reasoning, funnel_stage, datetime.now().isoformat(),
                 gates_passed, pair_created_at, tx_signature, tx_status,
                 buy_amount_usd, sell_amount_usd,
                 source, dex_url, market_cap, fdv, liquidity_usd,
                 volume_h1, volume_h24, volume_spike,
                 change_5m, change_1h, change_24h, vol_mcap_ratio,
                 buys_h1, sells_h1, buys_h24, sells_h24,
                 mint_authority, rugcheck_score, rugcheck_lp_locked,
                 rugcheck_dangers, rugcheck_warnings,
                 top_10_holder_pct, holder_count,
                 1 if liquidity_locked else (0 if liquidity_locked is not None else None),
                 raydium_vol_24h, raydium_tvl, raydium_burn_pct,
                 confidence, hype_score, risk_flags,
                 fusion_hype, fusion_liq_lock, fusion_vol_spike,
                 fusion_wallet, fusion_buy_sell, fusion_vol_mcap,
                 fusion_risk, fusion_btc, fusion_override,
                 token_age_hours),
            )
            conn.commit()
            conn.close()
        except Exception as e:
            logger.error(f"[DB] _log_to_db error: {e}")

    # ── Public API ────────────────────────────────────────────────────────────

    async def execute_trade(
        self,
        token_symbol:           str,
        token_address:          str,
        score:                  float,
        decision:               str,
        price:                  float           = 0.0,
        rejection_reason:       Optional[str]   = None,
        ai_reasoning:           Optional[str]   = None,
        funnel_stage:           str             = "FINAL",
        confidence:             str             = "LOW",
        liquidity_usd:          float           = 0.0,
        gates_passed:           Optional[str]   = None,
        pair_created_at:        Optional[int]   = None,
        sell_fraction:          float           = 1.0,
        position_size_override: Optional[float] = None,
        # ── Extended context (stored in DB for analysis) ──────────────
        extra:                  Optional[dict]  = None,
    ) -> dict:
        """
        Main public entry point.  Routes to _execute_buy / _execute_sell,
        logs to DB, and emits a structured event regardless of outcome.
        """
        position_size = self._position_size(confidence, position_size_override)
        slippage_bps  = self._slippage_bps(liquidity_usd)
        ex = extra or {}
        # Filter to only known _log_to_db parameters to avoid TypeError on unknown keys
        if not hasattr(TradeExecutor, '_log_params'):
            import inspect
            TradeExecutor._log_params = set(inspect.signature(self._log_to_db).parameters)
        ex = {k: v for k, v in ex.items() if k in TradeExecutor._log_params}

        # ── Non-trade decisions ───────────────────────────────────────────────
        if decision not in ("BUY", "SELL"):
            self._log_to_db(
                token_symbol, token_address, price, 0, score, decision,
                rejection_reason, ai_reasoning, funnel_stage, gates_passed, pair_created_at,
                confidence=confidence, **ex,
            )
            return {}

        trade_label = decision if not self.dry_run else f"{decision} (SIMULATED)"

        # ── DRY RUN (realistic simulation) ────────────────────────────────────
        if self.dry_run:
            sim_tx = f"SIM_{_uuid.uuid4().hex[:16]}"

            if decision == "BUY":
                sim_buy_usd  = position_size
                sim_sell_usd = None
                logger.info(
                    f"[DRY-RUN] 🟢 BUY ${sim_buy_usd:.4f} | {token_symbol} @ ${price:.8f} | TX: {sim_tx}"
                )
            else:
                # Calculate simulated sell value from tracked position
                sim_buy_usd  = None
                sim_sell_usd = self._simulate_sell_amount(
                    token_address, position_size, sell_fraction, price,
                )
                logger.info(
                    f"[DRY-RUN] 🔴 SELL ${sim_sell_usd:.4f} | {token_symbol} @ ${price:.8f} | TX: {sim_tx}"
                )

            self._log_to_db(
                token_symbol, token_address, price, position_size,
                score, trade_label, rejection_reason, ai_reasoning, funnel_stage,
                gates_passed, pair_created_at,
                tx_signature=sim_tx, tx_status="simulated",
                buy_amount_usd=sim_buy_usd, sell_amount_usd=sim_sell_usd,
                confidence=confidence, **ex,
            )
            _events.emit(
                f"{decision}_SIMULATED", token_symbol, token_address,
                tx_signature=sim_tx,
                price_usd=price, stage=funnel_stage,
                buy_amount_usd=sim_buy_usd,
                sell_amount_usd=sim_sell_usd,
                message=(
                    f"DRY-RUN {decision} | "
                    + (f"${sim_buy_usd:.4f}" if sim_buy_usd else f"${sim_sell_usd:.4f}")
                    + f" | score {score:.0f} | {sim_tx}"
                ),
                db_path=self.db_path,
            )
            return {
                "status":          "success",
                "dry_run":         True,
                "tx":              sim_tx,
                "tx_status":       "simulated",
                "buy_amount_usd":  sim_buy_usd,
                "sell_amount_usd": sim_sell_usd,
            }

        # ── Live guards ───────────────────────────────────────────────────────
        if not self.keypair:
            logger.error("[EXECUTOR] No keypair — cannot trade")
            return {"status": "error", "message": "No keypair configured"}

        if not _is_jupiter_reachable():
            logger.warning(f"[{token_symbol}] Jupiter unreachable → simulating")
            self._log_to_db(
                token_symbol, token_address, price, position_size, score,
                f"{decision} (SIMULATED - NO NETWORK)",
                "Jupiter unreachable", ai_reasoning, funnel_stage,
                gates_passed, pair_created_at,
                confidence=confidence, **ex,
            )
            return {"status": "success", "dry_run": True}

        logger.info(f"[LIVE] {decision} {token_symbol} @ ${price:.8f}")

        # ── Execute ───────────────────────────────────────────────────────────
        try:
            if decision == "BUY":
                result = await self._execute_buy(
                    token_symbol, token_address, position_size, slippage_bps
                )
            else:
                result = await self._execute_sell(
                    token_symbol, token_address, sell_fraction, slippage_bps
                )
        except Exception as e:
            logger.error(f"[LIVE] {decision} exception for {token_symbol}: {e}")
            self._log_to_db(
                token_symbol, token_address, price, 0, score, "ERROR",
                str(e), ai_reasoning, funnel_stage, gates_passed, pair_created_at,
                tx_status="error", confidence=confidence, **ex,
            )
            _events.emit(
                f"{decision}_FAILED", token_symbol, token_address,
                price_usd=price, stage=funnel_stage, message=str(e),
                db_path=self.db_path,
            )
            return {"status": "error", "message": str(e)}

        # ── Log outcome ───────────────────────────────────────────────────────
        if result.get("status") == "success":
            self._log_to_db(
                token_symbol, token_address, price, position_size,
                score, decision, rejection_reason, ai_reasoning, funnel_stage,
                gates_passed, pair_created_at,
                tx_signature=result.get("tx"),
                tx_status=result.get("tx_status", "confirmed"),
                buy_amount_usd=result.get("buy_amount_usd"),
                sell_amount_usd=result.get("sell_amount_usd"),
                confidence=confidence, **ex,
            )
            _events.emit(
                f"{decision}_SUCCESS", token_symbol, token_address,
                tx_signature=result.get("tx"),
                buy_amount_usd=result.get("buy_amount_usd"),
                sell_amount_usd=result.get("sell_amount_usd"),
                price_usd=price, stage=funnel_stage,
                message=f"✅ {decision} confirmed | {(result.get('tx') or '')[:20]}...",
                db_path=self.db_path,
            )
        else:
            self._log_to_db(
                token_symbol, token_address, price, 0, score,
                f"{decision}_UNCONFIRMED",
                result.get("message"), ai_reasoning, funnel_stage,
                gates_passed, pair_created_at,
                tx_signature=result.get("tx"), tx_status="unconfirmed",
                confidence=confidence, **ex,
            )
            _events.emit(
                f"{decision}_FAILED", token_symbol, token_address,
                tx_signature=result.get("tx"), price_usd=price,
                stage=funnel_stage, message=result.get("message", "TX unconfirmed"),
                db_path=self.db_path,
            )

        return result
