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

JUPITER_QUOTE_URL = "https://lite-api.jup.ag/swap/v1/quote"
JUPITER_SWAP_URL  = "https://lite-api.jup.ag/swap/v1/swap"
JUPITER_TIMEOUT   = int(os.getenv("JUPITER_TIMEOUT", "10"))
JUPITER_RETRIES   = int(os.getenv("JUPITER_RETRIES", "3"))
SOL_MINT          = "So11111111111111111111111111111111111111112"
SOLANA_RPC_URL    = os.getenv("SOLANA_RPC_URL", "https://solana-rpc.publicnode.com")


def _check_jupiter_reachable() -> bool:
    import socket
    try:
        socket.getaddrinfo("lite-api.jup.ag", 443)
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
                   gates_passed=None):
        try:
            conn = sqlite3.connect(self.db_path)
            c    = conn.cursor()
            c.execute(
                "INSERT INTO trades (token_address, symbol, entry_price, position_size, "
                "score, decision, rejection_reason, ai_reasoning, funnel_stage, timestamp, gates_passed) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (address, symbol, price, size, score, decision,
                 rejection_reason, ai_reasoning, funnel_stage, datetime.now(),
                 gates_passed)
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
    async def execute_trade(
        self,
        token_symbol:     str,
        token_address:    str,
        score:            float,
        decision:         str,
        price:            float = 0.0,
        rejection_reason: str   = None,
        ai_reasoning:     str   = None,
        funnel_stage:     str   = "FINAL",
        confidence:       str   = "LOW",
        liquidity_usd:    float = 0.0,
        gates_passed:     str   = None,
    ) -> dict:

        position_size = self._calculate_position_size(confidence)
        slippage_bps  = self._calculate_slippage_bps(liquidity_usd)

        # ── Kein Trade ─────────────────────────────────────────────────────────
        if decision not in ["BUY", "SELL"]:
            self._log_to_db(token_symbol, token_address, price, 0, score,
                            decision, rejection_reason, ai_reasoning, funnel_stage,
                            gates_passed)
            return None

        trade_label = decision if not self.dry_run else f"{decision} (SIMULATED)"

        # ── DRY-RUN ────────────────────────────────────────────────────────────
        if self.dry_run:
            logger.info(f"[DRY-RUN] {decision} ${position_size} | {token_symbol} @ ${price}")
            self._log_to_db(token_symbol, token_address, price, position_size,
                            score, trade_label, rejection_reason, ai_reasoning, funnel_stage,
                            gates_passed)
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
                            msg, ai_reasoning, funnel_stage, gates_passed)
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
                    f"[LIVE] ✅ Quote: {quote_data['inAmount']} lamports "
                    f"→ {quote_data['outAmount']} {token_symbol}"
                )

            else:
                quote_data = None

            # ── 2. SWAP TRANSAKTION BAUEN ──────────────────────────────────────
            if quote_data:
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

                # ── 4. DIREKT VIA HTTP SENDEN (kein solana-py!) ────────────────
                logger.info(f"[LIVE] Sende TX via HTTP RPC...")
                tx_id = await _send_transaction_http(bytes(signed_tx))

                logger.info(f"[LIVE] ✅ ERFOLGREICH!")
                logger.info(f"[LIVE] 🔗 https://solscan.io/tx/{tx_id}")

            else:
                tx_id = "SELL_PLACEHOLDER"

            # ── 5. DB LOGGING ──────────────────────────────────────────────────
            self._log_to_db(
                token_symbol, token_address, price, position_size,
                score, trade_label, rejection_reason, ai_reasoning, funnel_stage,
                gates_passed,
            )
            return {"status": "success", "tx": tx_id}

        except Exception as e:
            logger.error(f"[LIVE] Trade Fehler für {token_symbol}: {e}")
            self._log_to_db(token_symbol, token_address, price, 0, score,
                            "ERROR", str(e), ai_reasoning, funnel_stage, gates_passed)
            return {"status": "error", "message": str(e)}
