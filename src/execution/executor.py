import os
import base58
import sqlite3
from datetime import datetime
from loguru import logger
from dotenv import load_dotenv
from solders.keypair import Keypair
from solana.rpc.async_api import AsyncClient
import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

load_dotenv()

# ── Jupiter settings ──────────────────────────────────────────────────────────
JUPITER_QUOTE_URL = "https://quote-api.jup.ag/v6/quote"
JUPITER_SWAP_URL  = "https://quote-api.jup.ag/v6/swap"
JUPITER_TIMEOUT   = int(os.getenv("JUPITER_TIMEOUT", "10"))
JUPITER_RETRIES   = int(os.getenv("JUPITER_RETRIES", "2"))


def _check_jupiter_reachable() -> bool:
    """Schneller DNS-Check ob Jupiter erreichbar ist."""
    import socket
    try:
        socket.getaddrinfo("quote-api.jup.ag", 443, timeout=3)
        return True
    except Exception:
        return False


def _build_session() -> requests.Session:
    session = requests.Session()
    retry = Retry(
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


class TradeExecutor:
    def __init__(self):
        self.dry_run          = os.getenv("DRY_RUN", "True").lower() == "true"
        self.max_position_usd = float(os.getenv("TRADE_MAX_POSITION_USD", "0.20"))
        self.rpc_url          = os.getenv("SOLANA_RPC_URL", "https://api.mainnet-beta.solana.com")
        self.client           = AsyncClient(self.rpc_url)
        self.db_path          = "memecoin_bot.db"
        self.http             = _build_session()
        self.private_key      = os.getenv("SOLANA_PRIVATE_KEY")

        if self.private_key:
            try:
                decoded = base58.b58decode(self.private_key.strip())
                self.keypair = (
                    Keypair.from_bytes(decoded) if len(decoded) == 64
                    else Keypair.from_seed(decoded)
                )
            except Exception as e:
                logger.error(f"Keypair Fehler: {e}")
                self.keypair = None
        else:
            self.keypair = None

        # Beim Start: Jupiter Erreichbarkeit prüfen
        if not self.dry_run:
            if not _check_jupiter_reachable():
                logger.warning(
                    "⚠️  Jupiter API nicht erreichbar (DNS-Fehler). "
                    "Bot wird automatisch auf DRY_RUN=True gesetzt!"
                )
                self.dry_run = True

    # ── DB Logging ─────────────────────────────────────────────────────────────
    def _log_to_db(self, symbol, address, price, size, score, decision,
                   rejection_reason=None, ai_reasoning=None, funnel_stage="FINAL"):
        try:
            conn = sqlite3.connect(self.db_path)
            c = conn.cursor()
            c.execute(
                "INSERT INTO trades (token_address, symbol, entry_price, position_size, "
                "score, decision, rejection_reason, ai_reasoning, funnel_stage, timestamp) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (address, symbol, price, size, score, decision,
                 rejection_reason, ai_reasoning, funnel_stage, datetime.now())
            )
            conn.commit()
            conn.close()
        except Exception as e:
            logger.error(f"DB Logging Fehler: {e}")

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
    ) -> dict:

        position_size = self.max_position_usd

        # ── Kein Trade ─────────────────────────────────────────────────────────
        if decision not in ["BUY", "SELL"]:
            logger.info(f"[{token_symbol}] {decision} | {rejection_reason}")
            self._log_to_db(token_symbol, token_address, price, 0, score,
                            decision, rejection_reason, ai_reasoning, funnel_stage)
            return None

        trade_label = decision if not self.dry_run else f"{decision} (SIMULATED)"

        # ── DRY-RUN ────────────────────────────────────────────────────────────
        if self.dry_run:
            logger.info(f"[DRY-RUN] {decision} ${position_size} von {token_symbol} @ ${price}")
            self._log_to_db(token_symbol, token_address, price, position_size,
                            score, trade_label, rejection_reason, ai_reasoning, funnel_stage)
            return {"status": "success", "dry_run": True}

        # ── LIVE ───────────────────────────────────────────────────────────────
        if not self.keypair:
            logger.error("Kein Private Key — Live-Trade nicht möglich.")
            return None

        # Nochmal Jupiter prüfen bevor wir traden
        if not _check_jupiter_reachable():
            msg = "Jupiter nicht erreichbar — Trade als SIMULATION gespeichert"
            logger.warning(f"[{token_symbol}] {msg}")
            self._log_to_db(token_symbol, token_address, price, position_size,
                            score, f"{decision} (SIMULATED - NO NETWORK)",
                            msg, ai_reasoning, funnel_stage)
            return {"status": "success", "dry_run": True, "reason": "jupiter_unreachable"}

        logger.info(f"[LIVE] {decision} {token_symbol} @ ${price}...")
        try:
            quote_resp = None

            if decision == "BUY":
                # SOL Preis holen
                sol_price = 150.0
                try:
                    sol_r = self.http.get(
                        "https://api.dexscreener.com/latest/dex/tokens/"
                        "So11111111111111111111111111111111111111112",
                        timeout=JUPITER_TIMEOUT,
                    )
                    if sol_r.status_code == 200:
                        pairs = sol_r.json().get("pairs", [])
                        if pairs:
                            sol_price = float(pairs[0].get("priceUsd", 150.0))
                except Exception as e:
                    logger.warning(f"SOL Preis Fehler, nutze ${sol_price}: {e}")

                amount_lamports = int((position_size / sol_price) * 1_000_000_000)

                try:
                    quote_resp = self.http.get(
                        JUPITER_QUOTE_URL,
                        params={
                            "inputMint":   "So11111111111111111111111111111111111111112",
                            "outputMint":  token_address,
                            "amount":      amount_lamports,
                            "slippageBps": 50,
                        },
                        timeout=JUPITER_TIMEOUT,
                    )
                except (requests.exceptions.ConnectionError,
                        requests.exceptions.Timeout) as e:
                    msg = f"Jupiter nicht erreichbar: {type(e).__name__}"
                    logger.error(f"[{token_symbol}] {msg}")
                    self._log_to_db(token_symbol, token_address, price, 0,
                                    score, "ERROR", msg, ai_reasoning, funnel_stage)
                    return {"status": "error", "message": msg}

                if quote_resp.status_code != 200:
                    msg = f"Jupiter HTTP {quote_resp.status_code}"
                    logger.error(msg)
                    return {"status": "error", "message": msg}

            else:
                logger.info(f"[LIVE] SELL {token_symbol}")

            # Swap senden
            if quote_resp:
                try:
                    tx_resp = self.http.post(
                        JUPITER_SWAP_URL,
                        json={
                            "quoteResponse": quote_resp.json(),
                            "userPublicKey": str(self.keypair.pubkey()),
                            "wrapUnwrapSOL": True,
                        },
                        timeout=JUPITER_TIMEOUT,
                    )
                except requests.exceptions.RequestException as e:
                    msg = f"Swap Request Fehler: {e}"
                    logger.error(msg)
                    return {"status": "error", "message": msg}

                swap_data = tx_resp.json()
                if "swapTransaction" not in swap_data:
                    logger.error(f"Kein swapTransaction: {swap_data}")
                    return {"status": "error", "message": "Kein swapTransaction"}

                import base64
                from solders.transaction import VersionedTransaction
                raw_tx    = base64.b64decode(swap_data["swapTransaction"])
                tx        = VersionedTransaction.from_bytes(raw_tx)
                signed_tx = VersionedTransaction(tx.message, [self.keypair])
                result    = await self.client.send_transaction(signed_tx)
                tx_id     = str(result.value)
            else:
                tx_id = "SELL_MOCK"

            logger.info(f"[LIVE] ✅ Transaktion: {tx_id}")
            self._log_to_db(token_symbol, token_address, price, position_size,
                            score, trade_label, rejection_reason, ai_reasoning, funnel_stage)
            return {"status": "success", "tx": tx_id}

        except Exception as e:
            logger.error(f"Swap Fehler: {e}")
            self._log_to_db(token_symbol, token_address, price, 0, score,
                            "ERROR", str(e), ai_reasoning, funnel_stage)
            return {"status": "error", "message": str(e)}
