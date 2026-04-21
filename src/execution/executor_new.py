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

# Solana RPC endpoints — primary first, fallbacks after
_RPC_ENV = os.getenv("SOLANA_RPC_URL", "https://solana-rpc.publicnode.com")
RPC_ENDPOINTS: list[str] = list(dict.fromkeys([
    _RPC_ENV,
    "https://solana-rpc.publicnode.com",
    "https://rpc.ankr.com/solana",
    "https://api.mainnet-beta.solana.com",
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
            "skipPreflight":       True,
            "preflightCommitment": "confirmed",
            "maxRetries":          3,
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

        # Re-fetch SOL price at confirmation time for accurate USD tracking
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
        raw_amount, found = self._get_token_balance(address, sell_fraction)
        if not found or raw_amount <= 0:
            msg = f"No token balance for {symbol} in wallet"
            logger.error(f"[SELL] {msg}")
            return {"status": "error", "message": msg}

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

    def _log_to_db(
        self,
        symbol, address, price, size, score, decision,
        rejection_reason=None, ai_reasoning=None, funnel_stage="FINAL",
        gates_passed=None, pair_created_at=None,
        tx_signature=None, tx_status=None,
        buy_amount_usd=None, sell_amount_usd=None,
    ) -> None:
        try:
            conn = sqlite3.connect(self.db_path)
            c    = conn.cursor()
            # Auto-migrate missing columns
            for col, dtype in [
                ("pair_created_at", "INTEGER"), ("tx_signature",    "TEXT"),
                ("tx_status",       "TEXT"),    ("buy_amount_usd",  "REAL"),
                ("sell_amount_usd", "REAL"),
            ]:
                try:
                    c.execute(f"ALTER TABLE trades ADD COLUMN {col} {dtype}")
                    conn.commit()
                except Exception:
                    pass
            c.execute(
                "INSERT INTO trades "
                "(token_address, symbol, entry_price, position_size, score, decision, "
                " rejection_reason, ai_reasoning, funnel_stage, timestamp, gates_passed, "
                " pair_created_at, tx_signature, tx_status, buy_amount_usd, sell_amount_usd) "
                "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (address, symbol, price, size, score, decision,
                 rejection_reason, ai_reasoning, funnel_stage, datetime.now().isoformat(),
                 gates_passed, pair_created_at, tx_signature, tx_status,
                 buy_amount_usd, sell_amount_usd),
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
    ) -> dict:
        """
        Main public entry point.  Routes to _execute_buy / _execute_sell,
        logs to DB, and emits a structured event regardless of outcome.
        """
        position_size = self._position_size(confidence, position_size_override)
        slippage_bps  = self._slippage_bps(liquidity_usd)

        # ── Non-trade decisions ───────────────────────────────────────────────
        if decision not in ("BUY", "SELL"):
            self._log_to_db(
                token_symbol, token_address, price, 0, score, decision,
                rejection_reason, ai_reasoning, funnel_stage, gates_passed, pair_created_at,
            )
            return {}

        trade_label = decision if not self.dry_run else f"{decision} (SIMULATED)"

        # ── DRY RUN ───────────────────────────────────────────────────────────
        if self.dry_run:
            logger.info(f"[DRY-RUN] {decision} ${position_size:.4f} | {token_symbol} @ ${price:.8f}")
            self._log_to_db(
                token_symbol, token_address, price, position_size,
                score, trade_label, rejection_reason, ai_reasoning, funnel_stage,
                gates_passed, pair_created_at,
            )
            _events.emit(
                f"{decision}_SIMULATED", token_symbol, token_address,
                price_usd=price, stage=funnel_stage,
                buy_amount_usd=position_size if decision == "BUY" else None,
                message=f"DRY-RUN {decision} | ${position_size:.4f} | score {score:.0f}",
            )
            return {"status": "success", "dry_run": True}

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
                tx_status="error",
            )
            _events.emit(
                f"{decision}_FAILED", token_symbol, token_address,
                price_usd=price, stage=funnel_stage, message=str(e),
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
            )
            _events.emit(
                f"{decision}_SUCCESS", token_symbol, token_address,
                tx_signature=result.get("tx"),
                buy_amount_usd=result.get("buy_amount_usd"),
                sell_amount_usd=result.get("sell_amount_usd"),
                price_usd=price, stage=funnel_stage,
                message=f"✅ {decision} confirmed | {(result.get('tx') or '')[:20]}...",
            )
        else:
            self._log_to_db(
                token_symbol, token_address, price, 0, score,
                f"{decision}_UNCONFIRMED",
                result.get("message"), ai_reasoning, funnel_stage,
                gates_passed, pair_created_at,
                tx_signature=result.get("tx"), tx_status="unconfirmed",
            )
            _events.emit(
                f"{decision}_FAILED", token_symbol, token_address,
                tx_signature=result.get("tx"), price_usd=price,
                stage=funnel_stage, message=result.get("message", "TX unconfirmed"),
            )

        return result
