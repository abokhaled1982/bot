"""
Robuster Solana RPC Client mit:
- Mehrere Fallback-Endpunkte
- Auto-Retry bei 429 mit Backoff
- Rate-Limit Erkennung
"""
import time
import requests
import os
from loguru import logger

# Reihenfolge: bester zuerst
RPC_ENDPOINTS = [
    os.getenv("SOLANA_RPC_URL", "https://solana-rpc.publicnode.com"),
    "https://solana-rpc.publicnode.com",
    "https://rpc.ankr.com/solana",
    "https://api.mainnet-beta.solana.com",
]

# Deduplizieren aber Reihenfolge behalten
seen = set()
RPC_ENDPOINTS = [x for x in RPC_ENDPOINTS if not (x in seen or seen.add(x))]


def rpc_call(method: str, params: list, timeout: int = 8) -> dict:
    """
    Macht einen Solana RPC Call mit automatischem Fallback und Retry.
    Gibt das 'result' Feld zurück oder None bei Fehler.
    """
    payload = {"jsonrpc": "2.0", "id": 1, "method": method, "params": params}

    for attempt in range(3):
        for endpoint in RPC_ENDPOINTS:
            try:
                r = requests.post(endpoint, json=payload, timeout=timeout)

                # 429 — kurz warten und nächsten RPC versuchen
                if r.status_code == 429:
                    wait = 2 ** attempt  # 1s, 2s, 4s
                    logger.warning(
                        f"[RPC] 429 Rate-Limit auf {endpoint} — "
                        f"warte {wait}s und versuche nächsten Endpunkt..."
                    )
                    time.sleep(wait)
                    continue

                if r.status_code != 200:
                    logger.warning(f"[RPC] HTTP {r.status_code} auf {endpoint}")
                    continue

                data = r.json()

                if "error" in data:
                    code = data["error"].get("code", 0)
                    msg  = data["error"].get("message", "")
                    if code == 429:
                        wait = 2 ** attempt
                        logger.warning(f"[RPC] Rate-Limit ({msg}) — warte {wait}s...")
                        time.sleep(wait)
                        continue
                    logger.warning(f"[RPC] Fehler: {msg}")
                    continue

                return data.get("result")

            except requests.exceptions.Timeout:
                logger.warning(f"[RPC] Timeout auf {endpoint}")
                continue
            except requests.exceptions.ConnectionError as e:
                logger.warning(f"[RPC] Verbindungsfehler auf {endpoint}: {e}")
                continue
            except Exception as e:
                logger.error(f"[RPC] Unbekannter Fehler: {e}")
                continue

        # Alle Endpunkte versucht — kurz warten
        if attempt < 2:
            time.sleep(2 ** attempt)

    logger.error("[RPC] Alle Endpunkte fehlgeschlagen!")
    return None
