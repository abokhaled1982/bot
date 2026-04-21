import os
import base58
import base64
import sqlite3
import aiohttp
from datetime import datetime
from loguru import logger
from dotenv import load_dotenv
from solders.keypair import Keypair
from solders.transaction import VersionedTransaction
import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

load_dotenv()

JUPITER_QUOTE_URL = "https://api.jup.ag/swap/v1/quote"
JUPITER_SWAP_URL  = "https://api.jup.ag/swap/v1/swap"
JUPITER_TIMEOUT   = int(os.getenv("JUPITER_TIMEOUT", "10"))
JUPITER_RETRIES   = int(os.getenv("JUPITER_RETRIES", "3"))
SOL_MINT          = "So11111111111111111111111111111111111111112"
SOLANA_RPC_URL    = os.getenv("SOLANA_RPC_URL", "https://solana-rpc.publicnode.com")


def _check_jupiter_reachable() -> bool:
    import socket
    try:
        socket.getaddrinfo("api.jup.ag", 443)
        return True
    except Exception:
        return False


def _build_session() -> requests.Session:
    session = requests.Session()
    retry   = Retry(
        total=JUPITER_RETRIES,
        backoff_factor=1,
        status_forcelist=[429, 500, 502, 503, 504],
        allowed_methods=["GET", "POST"],
        raise_on_status=False,
    )
    adapter = HTTPAdapter(max_retries=retry)
    session.mount("https://", adapter)
    session.mount("http://",  adapter)
    return session


def _get_sol_price(http: requests.Session) -> float:
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
    return 150.0


async def _send_transaction_http(signed_tx_bytes: bytes) -> str:
    """
    Sendet eine signierte Transaktion DIREKT via HTTP RPC.
    Kein solana-py — vermeidet den Rust Panic Bug komplett.
    """
    tx_b64 = base64.b64encode(signed_tx_bytes).decode("utf-8")

    rpc_endpoints = [
        SOLANA_RPC_URL,
        "https://solana-rpc.publicnode.com",
        "https://rpc.ankr.com/solana",
        "https://api.mainnet-beta.solana.com",
    ]
    # Deduplizieren
    seen = set()
    rpc_endpoints = [x for x in rpc_endpoints if not (x in seen or seen.add(x))]

    payload = {
        "jsonrpc": "2.0",
        "id":      1,
        "method":  "sendTransaction",
        "params":  [
            tx_b64,
            {
                "encoding":            "base64",
                "skipPreflight":       True,
                "preflightCommitment": "confirmed",
                "maxRetries":          3,
            }
        ]
    }

    async with aiohttp.ClientSession() as session:
        for endpoint in rpc_endpoints:
            try:
                async with session.post(
                    endpoint,
                    json=payload,
                    timeout=aiohttp.ClientTimeout(total=15),
                ) as resp:
                    data = await resp.json()

                    if "error" in data:
                        code = data["error"].get("code", 0)
                        msg  = data["error"].get("message", "")
                        # 429 → nächsten Endpunkt versuchen
                        if code == 429:
                            logger.warning(f"[TX] 429 auf {endpoint} → nächster...")
                            continue
                        # Andere Fehler
                        logger.error(f"[TX] RPC Fehler: {msg}")
                        raise Exception(f"RPC Fehler: {msg}")

                    tx_id = data.get("result")
                    if not tx_id:
                        logger.error(f"[TX] Kein TX-ID in Antwort: {data}")
                        raise Exception("Kein TX-ID")

                    return tx_id

            except aiohttp.ClientError as e:
                logger.warning(f"[TX] Verbindungsfehler auf {endpoint}: {e}")
                continue

    raise Exception("Alle RPC Endpunkte fehlgeschlagen")


async def _confirm_transaction(tx_id: str, timeout_sec: int = 90) -> tuple[bool, str]:
    """
    Polls getSignatureStatuses until the TX is confirmed/finalized or timeout.
    Tries all RPC endpoints per poll cycle before sleeping.
    Returns (confirmed: bool, err_msg: str).
    """
    import asyncio
    rpc_endpoints = [
        SOLANA_RPC_URL,
        "https://solana-rpc.publicnode.com",
        "https://rpc.ankr.com/solana",
        "https://api.mainnet-beta.solana.com",
    ]
    seen = set()
    rpc_endpoints = [x for x in rpc_endpoints if not (x in seen or seen.add(x))]

    deadline = asyncio.get_event_loop().time() + timeout_sec
    poll_interval = 4  # seconds between full poll rounds

    async with aiohttp.ClientSession() as session:
        while asyncio.get_event_loop().time() < deadline:
            for endpoint in rpc_endpoints:
                try:
                    async with session.post(
                        endpoint,
                        json={
                            "jsonrpc": "2.0",
                            "id": 1,
                            "method": "getSignatureStatuses",
                            "params": [[tx_id], {"searchTransactionHistory": True}],
                        },
                        timeout=aiohttp.ClientTimeout(total=8),
                    ) as resp:
                        data = await resp.json()
                        statuses = (data.get("result") or {}).get("value", [None])
                        status = statuses[0] if statuses else None
                        if status is None:
                            # TX not yet visible on this endpoint — try next
                            continue
                        err = status.get("err")
                        if err is not None:
                            return False, f"TX on-chain error: {err}"
                        conf = status.get("confirmationStatus", "")
                        if conf in ("confirmed", "finalized"):
                            return True, ""
                        # TX visible but still "processed" — continue polling
                        break  # no need to try other endpoints, TX is seen
                except Exception:
                    continue
            await asyncio.sleep(poll_interval)

    # Final check across all endpoints before declaring timeout
    async with aiohttp.ClientSession() as session:
        for endpoint in rpc_endpoints:
            try:
                async with session.post(
                    endpoint,
                    json={
                        "jsonrpc": "2.0",
                        "id": 1,
                        "method": "getSignatureStatuses",
                        "params": [[tx_id], {"searchTransactionHistory": True}],
                    },
                    timeout=aiohttp.ClientTimeout(total=12),
                ) as resp:
                    data = await resp.json()
                    statuses = (data.get("result") or {}).get("value", [None])
                    status = statuses[0] if statuses else None
                    if status is None:
                        continue
                    err = status.get("err")
                    if err is not None:
                        return False, f"TX on-chain error: {err}"
                    conf = status.get("confirmationStatus", "")
                    if conf in ("confirmed", "finalized"):
                        return True, ""
            except Exception:
                continue

    return False, f"TX not confirmed within {timeout_sec}s (may still land, check Solscan)"


class TradeExecutor:
    def __init__(self):
        self.dry_run          = os.getenv("DRY_RUN", "True").lower() == "true"
        self.max_position_usd = float(os.getenv("TRADE_MAX_POSITION_USD", "0.20"))
        self.min_position_usd = float(os.getenv("TRADE_MIN_POSITION_USD", "0.10"))
        self.db_path          = "memecoin_bot.db"
        self.http             = _build_session()

        # Keypair laden
        pk = os.getenv("SOLANA_PRIVATE_KEY")
        self.keypair = None
        if pk:
            try:
                decoded      = base58.b58decode(pk.strip())
                self.keypair = (
                    Keypair.from_bytes(decoded) if len(decoded) == 64
                    else Keypair.from_seed(decoded)
                )
                logger.info(f"[EXECUTOR] Wallet: {str(self.keypair.pubkey())}")
            except Exception as e:
                logger.error(f"[EXECUTOR] Keypair Fehler: {e}")

        # Jupiter beim Start prüfen
        if not self.dry_run:
            if _check_jupiter_reachable():
                logger.info("✅ Jupiter erreichbar — Live Trading aktiv")
            else:
                logger.warning("⚠️ Jupiter nicht erreichbar → DRY_RUN aktiviert")
                self.dry_run = True

    # ── DB ─────────────────────────────────────────────────────────────────────
    def _log_to_db(self, symbol, address, price, size, score, decision,
                   rejection_reason=None, ai_reasoning=None, funnel_stage="FINAL",
                   gates_passed=None, pair_created_at=None,
                   tx_signature=None, tx_status=None,
                   buy_amount_usd=None, sell_amount_usd=None):
        try:
            conn = sqlite3.connect(self.db_path)
            c    = conn.cursor()
            # Auto-migrate: add columns if missing
            for col, col_type in [
                ("pair_created_at",  "INTEGER"),
                ("tx_signature",     "TEXT"),
                ("tx_status",        "TEXT"),
                ("buy_amount_usd",   "REAL"),
                ("sell_amount_usd",  "REAL"),
            ]:
                try:
                    c.execute(f"ALTER TABLE trades ADD COLUMN {col} {col_type}")
                    conn.commit()
                except Exception:
                    pass  # column already exists
            c.execute(
                "INSERT INTO trades (token_address, symbol, entry_price, position_size, "
                "score, decision, rejection_reason, ai_reasoning, funnel_stage, timestamp, "
                "gates_passed, pair_created_at, tx_signature, tx_status, "
                "buy_amount_usd, sell_amount_usd) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (address, symbol, price, size, score, decision,
                 rejection_reason, ai_reasoning, funnel_stage, datetime.now().isoformat(),
                 gates_passed, pair_created_at, tx_signature, tx_status,
                 buy_amount_usd, sell_amount_usd)
            )
            conn.commit()
            conn.close()
        except Exception as e:
            logger.error(f"[DB] Fehler: {e}")

    def _calculate_position_size(self, confidence: str) -> float:
        """Dynamic position sizing based on conviction level."""
        if   confidence == "HIGH":   return self.max_position_usd
        elif confidence == "MEDIUM": return (self.max_position_usd + self.min_position_usd) / 2
        else:                        return self.min_position_usd

    def _calculate_slippage_bps(self, liquidity_usd: float) -> int:
        """Dynamic slippage based on token liquidity."""
        if   liquidity_usd >= 500_000: return 100   # 1% — deep liquidity
        elif liquidity_usd >= 100_000: return 200   # 2%
        elif liquidity_usd >= 50_000:  return 300   # 3%
        elif liquidity_usd >= 20_000:  return 500   # 5%
        else:                          return 800   # 8% — thin liquidity

    # ── Haupt-Funktion ──────────────────────────────────────────────────────────
    def _get_token_raw_amount(self, token_address: str, sell_fraction: float) -> tuple[int, bool]:
        """
        Returns (raw_amount_to_sell, success).
        Queries wallet via RPC for Token-2022 AND legacy program.
        """
        wallet = str(self.keypair.pubkey())
        for program_id in [
            "TokenzQdBNbLqP5VEhdkAS6EPFLC1PHnBqCXEpPxuEb",   # Token-2022 (pump.fun)
            "TokenkegQfeZyiNwAJbNbGKPFXCWuBvf9Ss623VQ5DA",   # legacy SPL
        ]:
            try:
                resp = self.http.post(
                    SOLANA_RPC_URL,
                    json={
                        "jsonrpc": "2.0", "id": 1,
                        "method": "getTokenAccountsByOwner",
                        "params": [
                            wallet,
                            {"programId": program_id},
                            {"encoding": "jsonParsed"},
                        ],
                    },
                    timeout=8,
                )
                if not resp.text.strip():
                    logger.warning(f"[EXECUTOR] Leere RPC-Antwort für {program_id[:12]}")
                    continue
                data = resp.json()
                for acc in data.get("result", {}).get("value", []):
                    info = acc["account"]["data"]["parsed"]["info"]
                    if info["mint"] != token_address:
                        continue
                    decimals   = int(info["tokenAmount"]["decimals"])
                    ui_amount  = float(info["tokenAmount"]["uiAmount"] or 0)
                    if ui_amount <= 0:
                        return 0, False
                    raw_total  = int(ui_amount * (10 ** decimals))
                    raw_sell   = int(raw_total * sell_fraction)
                    logger.info(
                        f"[EXECUTOR] 🔍 Token balance: {ui_amount:.4f} "
                        f"(decimals={decimals}) → selling {sell_fraction:.0%} = {raw_sell} raw"
                    )
                    return raw_sell, True
            except Exception as e:
                logger.warning(f"[EXECUTOR] RPC balance check failed ({program_id[:12]}): {e}")
                continue
        return 0, False

    async def execute_trade(
        self,
        token_symbol:          str,
        token_address:         str,
        score:                 float,
        decision:              str,
        price:                 float = 0.0,
        rejection_reason:      str   = None,
        ai_reasoning:          str   = None,
        funnel_stage:          str   = "FINAL",
        confidence:            str   = "LOW",
        liquidity_usd:         float = 0.0,
        gates_passed:          str   = None,
        pair_created_at:       int   = None,
        sell_fraction:         float = 1.0,
        position_size_override: float = None,
    ) -> dict:

        position_size = position_size_override if position_size_override else self._calculate_position_size(confidence)
        slippage_bps  = self._calculate_slippage_bps(liquidity_usd)

        # ── Kein Trade ─────────────────────────────────────────────────────────
        if decision not in ["BUY", "SELL"]:
            self._log_to_db(token_symbol, token_address, price, 0, score,
                            decision, rejection_reason, ai_reasoning, funnel_stage,
                            gates_passed, pair_created_at)
            return None

        trade_label = decision if not self.dry_run else f"{decision} (SIMULATED)"

        # ── DRY-RUN ────────────────────────────────────────────────────────────
        if self.dry_run:
            logger.info(f"[DRY-RUN] {decision} ${position_size} | {token_symbol} @ ${price}")
            self._log_to_db(token_symbol, token_address, price, position_size,
                            score, trade_label, rejection_reason, ai_reasoning, funnel_stage,
                            gates_passed, pair_created_at)
            return {"status": "success", "dry_run": True}

        # ── LIVE ───────────────────────────────────────────────────────────────
        if not self.keypair:
            logger.error("[EXECUTOR] Kein Keypair — Trade nicht möglich")
            return None

        if not _check_jupiter_reachable():
            msg = "Jupiter nicht erreichbar"
            logger.warning(f"[{token_symbol}] {msg} → Simulation")
            self._log_to_db(token_symbol, token_address, price, position_size,
                            score, f"{decision} (SIMULATED - NO NETWORK)",
                            msg, ai_reasoning, funnel_stage, gates_passed, pair_created_at)
            return {"status": "success", "dry_run": True}

        logger.info(f"[LIVE] {decision} {token_symbol} @ ${price}")

        try:
            # ── 1. QUOTE ───────────────────────────────────────────────────────
            if decision == "BUY":
                sol_price       = _get_sol_price(self.http)
                amount_lamports = int((position_size / sol_price) * 1_000_000_000)
                logger.info(f"[LIVE] ${position_size} = {amount_lamports} lamports @ SOL ${sol_price:.2f}")
                logger.info(f"[LIVE] Slippage: {slippage_bps}bps ({slippage_bps/100:.1f}%) für Liq ${liquidity_usd:,.0f}")
                q = self.http.get(
                    JUPITER_QUOTE_URL,
                    params={
                        "inputMint":   SOL_MINT,
                        "outputMint":  token_address,
                        "amount":      amount_lamports,
                        "slippageBps": slippage_bps,
                    },
                    timeout=JUPITER_TIMEOUT,
                )
                if q.status_code != 200:
                    msg = f"Quote HTTP {q.status_code}: {q.text[:100]}"
                    logger.error(msg)
                    return {"status": "error", "message": msg}
                quote_data = q.json()
                logger.info(
                    f"[LIVE] ✅ BUY Quote: {quote_data['inAmount']} lamports "
                    f"→ {quote_data['outAmount']} {token_symbol}"
                )

            else:  # SELL
                raw_amount, found = self._get_token_raw_amount(token_address, sell_fraction)
                if not found or raw_amount <= 0:
                    msg = f"Kein Token-Guthaben im Wallet für {token_symbol}"
                    logger.error(f"[LIVE] {msg}")
                    self._log_to_db(token_symbol, token_address, price, 0, score,
                                    "SELL_FAILED", msg, ai_reasoning, funnel_stage,
                                    gates_passed, pair_created_at)
                    return {"status": "error", "message": msg}

                logger.info(f"[LIVE] Slippage: {slippage_bps}bps für SELL {token_symbol}")
                q = self.http.get(
                    JUPITER_QUOTE_URL,
                    params={
                        "inputMint":   token_address,
                        "outputMint":  SOL_MINT,
                        "amount":      raw_amount,
                        "slippageBps": slippage_bps,
                    },
                    timeout=JUPITER_TIMEOUT,
                )
                if q.status_code != 200:
                    msg = f"SELL Quote HTTP {q.status_code}: {q.text[:100]}"
                    logger.error(msg)
                    return {"status": "error", "message": msg}
                quote_data = q.json()
                sol_out = int(quote_data.get("outAmount", 0)) / 1e9
                logger.info(
                    f"[LIVE] ✅ SELL Quote: {raw_amount} {token_symbol} "
                    f"→ {sol_out:.6f} SOL"
                )

            # ── 2. SWAP TRANSAKTION BAUEN ──────────────────────────────────────
            tx_r = self.http.post(
                JUPITER_SWAP_URL,
                json={
                    "quoteResponse":             quote_data,
                    "userPublicKey":             str(self.keypair.pubkey()),
                    "wrapAndUnwrapSol":          True,
                    "dynamicComputeUnitLimit":   True,
                    "prioritizationFeeLamports": "auto",
                },
                timeout=JUPITER_TIMEOUT,
            )

            if tx_r.status_code != 200:
                msg = f"Swap HTTP {tx_r.status_code}: {tx_r.text[:100]}"
                logger.error(msg)
                return {"status": "error", "message": msg}

            swap_data = tx_r.json()
            if "swapTransaction" not in swap_data:
                msg = f"Kein swapTransaction: {list(swap_data.keys())}"
                logger.error(msg)
                return {"status": "error", "message": msg}

            # ── 3. SIGNIEREN ───────────────────────────────────────────────
            logger.info(f"[LIVE] Signiere TX für {token_symbol}...")
            raw_tx    = base64.b64decode(swap_data["swapTransaction"])
            tx        = VersionedTransaction.from_bytes(raw_tx)
            signed_tx = VersionedTransaction(tx.message, [self.keypair])

            # ── 4. SENDEN ──────────────────────────────────────────────────
            logger.info(f"[LIVE] Sende TX via HTTP RPC...")
            tx_id = await _send_transaction_http(bytes(signed_tx))
            logger.info(f"[LIVE] TX gesendet: https://solscan.io/tx/{tx_id}")

            # ── 5. ON-CHAIN BESTÄTIGUNG ABWARTEN ───────────────────────────
            logger.info(f"[LIVE] Warte auf On-Chain Bestätigung (max 90s)...")
            confirmed, confirm_err = await _confirm_transaction(tx_id, timeout_sec=90)

            if confirmed:
                logger.info(f"[LIVE] ✅ {decision} ON-CHAIN BESTÄTIGT!")
                logger.info(f"[LIVE] 🔗 https://solscan.io/tx/{tx_id}")
                final_decision = trade_label
                tx_status      = "confirmed"
            else:
                # TX gesendet aber nicht bestätigt — NICHT als BUY in DB loggen
                logger.error(f"[LIVE] ❌ TX nicht bestätigt: {confirm_err}")
                logger.error(f"[LIVE] Solscan: https://solscan.io/tx/{tx_id}")
                final_decision = f"{decision}_UNCONFIRMED"
                tx_status      = "unconfirmed"
                self._log_to_db(
                    token_symbol, token_address, price, 0, score,
                    final_decision, confirm_err, ai_reasoning, funnel_stage,
                    gates_passed, pair_created_at,
                    tx_signature=tx_id, tx_status=tx_status,
                )
                return {"status": "error", "message": confirm_err, "tx": tx_id}

            # ── 6. DB LOGGING (nur bei bestätigter TX) ─────────────────────
            # Calculate actual USD amounts from the quote
            sol_price_now = _get_sol_price(self.http)
            if decision == "BUY":
                actual_buy_usd  = int(quote_data.get("inAmount", 0)) / 1e9 * sol_price_now
                actual_sell_usd = None
            else:
                actual_buy_usd  = None
                actual_sell_usd = int(quote_data.get("outAmount", 0)) / 1e9 * sol_price_now

            self._log_to_db(
                token_symbol, token_address, price, position_size,
                score, final_decision, rejection_reason, ai_reasoning, funnel_stage,
                gates_passed, pair_created_at,
                tx_signature=tx_id, tx_status=tx_status,
                buy_amount_usd=actual_buy_usd, sell_amount_usd=actual_sell_usd,
            )
            return {"status": "success", "tx": tx_id,
                    "buy_amount_usd": actual_buy_usd, "sell_amount_usd": actual_sell_usd}

        except Exception as e:
            logger.error(f"[LIVE] Trade Fehler für {token_symbol}: {e}")
            self._log_to_db(token_symbol, token_address, price, 0, score,
                            "ERROR", str(e), ai_reasoning, funnel_stage, gates_passed,
                            pair_created_at, tx_signature=None, tx_status="error")
            return {"status": "error", "message": str(e)}
