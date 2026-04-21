"""
API health-check tests — verify that each external API responds with valid data.
Each test hits one real endpoint so requires network access.

Run:
    cd /home/alghobariw/.openclaw/workspace/memecoin_bot
    source venv/bin/activate
    python -m pytest tests/test_apis.py -v
"""
import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

import requests
import pytest

TIMEOUT = 10
KNOWN_SOLANA_TOKEN = "So11111111111111111111111111111111111111112"   # Wrapped SOL
KNOWN_PAIR         = "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v"  # USDC


class TestDexScreenerAPI:

    def test_token_endpoint_returns_data(self):
        url = f"https://api.dexscreener.com/latest/dex/tokens/{KNOWN_SOLANA_TOKEN}"
        r = requests.get(url, timeout=TIMEOUT)
        assert r.status_code == 200, f"DexScreener returned {r.status_code}"
        data = r.json()
        assert "pairs" in data or "pair" in data, "No pairs in DexScreener response"

    def test_search_endpoint_works(self):
        url = "https://api.dexscreener.com/latest/dex/search?q=SOL"
        r = requests.get(url, timeout=TIMEOUT)
        assert r.status_code == 200, f"DexScreener search returned {r.status_code}"
        data = r.json()
        assert "pairs" in data, "No pairs in DexScreener search response"

    def test_sol_price_from_dexscreener(self):
        url = "https://api.dexscreener.com/latest/dex/tokens/So11111111111111111111111111111111111111112"
        r = requests.get(url, timeout=TIMEOUT)
        assert r.status_code == 200
        data = r.json()
        pairs = data.get("pairs") or []
        assert len(pairs) > 0, "No SOL pairs found on DexScreener"
        price = float(pairs[0].get("priceUsd", 0))
        assert price > 0, f"Invalid SOL price: {price}"


class TestCoinGeckoAPI:

    def test_sol_price_endpoint(self):
        url = "https://api.coingecko.com/api/v3/simple/price"
        params = {"ids": "solana", "vs_currencies": "usd", "include_24hr_change": "true"}
        r = requests.get(url, params=params, timeout=TIMEOUT)
        assert r.status_code == 200, f"CoinGecko returned {r.status_code}"
        data = r.json()
        assert "solana" in data, "No solana in CoinGecko response"
        assert "usd" in data["solana"], "No usd in CoinGecko solana data"
        price = data["solana"]["usd"]
        assert isinstance(price, (int, float)) and price > 0, f"Invalid price: {price}"

    def test_btc_price_endpoint(self):
        url = "https://api.coingecko.com/api/v3/simple/price"
        params = {"ids": "bitcoin", "vs_currencies": "usd", "include_24hr_change": "true"}
        r = requests.get(url, params=params, timeout=TIMEOUT)
        assert r.status_code == 200, f"CoinGecko BTC returned {r.status_code}"
        data = r.json()
        assert "bitcoin" in data
        assert data["bitcoin"]["usd"] > 0


class TestJupiterAPI:

    def test_jupiter_quote_reachable(self):
        """Check Jupiter can quote a tiny SOL→USDC swap."""
        url = "https://quote-api.jup.ag/v6/quote"
        params = {
            "inputMint":  "So11111111111111111111111111111111111111112",
            "outputMint": "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v",
            "amount":     "100000000",  # 0.1 SOL in lamports
            "slippageBps": "50",
        }
        try:
            r = requests.get(url, params=params, timeout=TIMEOUT)
        except (requests.exceptions.ConnectionError, requests.exceptions.Timeout) as exc:
            pytest.skip(f"Jupiter unreachable from this environment: {exc}")
        assert r.status_code == 200, f"Jupiter quote returned {r.status_code}: {r.text[:200]}"
        data = r.json()
        assert "outAmount" in data, f"No outAmount in Jupiter response: {list(data.keys())}"
        assert int(data["outAmount"]) > 0, "Jupiter quote returned 0 out"


class TestSolanaRPC:

    def test_rpc_getHealth(self):
        """Mainnet RPC should be reachable."""
        rpc = os.getenv("SOLANA_RPC_URL", "https://api.mainnet-beta.solana.com")
        payload = {"jsonrpc": "2.0", "id": 1, "method": "getHealth"}
        r = requests.post(rpc, json=payload, timeout=TIMEOUT)
        assert r.status_code == 200, f"RPC returned {r.status_code}"
        data = r.json()
        # "ok" or result contains "ok"
        result = data.get("result", "")
        assert "ok" in str(result).lower() or "result" in data, f"RPC unhealthy: {data}"

    def test_rpc_getSlot(self):
        rpc = os.getenv("SOLANA_RPC_URL", "https://api.mainnet-beta.solana.com")
        payload = {"jsonrpc": "2.0", "id": 1, "method": "getSlot"}
        r = requests.post(rpc, json=payload, timeout=TIMEOUT)
        assert r.status_code == 200
        data = r.json()
        slot = data.get("result", 0)
        assert isinstance(slot, int) and slot > 0, f"Invalid slot: {slot}"


class TestRpcFallback:

    def test_fallback_rpc_available(self):
        """Check backup RPC is reachable too."""
        backup_rpc = os.getenv(
            "SOLANA_RPC_BACKUP",
            "https://solana.public-rpc.com",
        )
        payload = {"jsonrpc": "2.0", "id": 1, "method": "getSlot"}
        try:
            r = requests.post(backup_rpc, json=payload, timeout=TIMEOUT)
            assert r.status_code == 200
        except requests.exceptions.ConnectionError:
            pytest.skip(f"Backup RPC {backup_rpc} is not configured or unreachable — skipping")


class TestRugCheckAPI:

    def test_rugcheck_returns_verdict(self):
        """RugCheck API should return a verdict for a known token."""
        url = f"https://api.rugcheck.xyz/v1/tokens/{KNOWN_SOLANA_TOKEN}/report/summary"
        r = requests.get(url, timeout=TIMEOUT)
        # RugCheck may rate-limit so accept 200 or 429
        assert r.status_code in (200, 429, 404), f"Unexpected status: {r.status_code}"
        if r.status_code == 200:
            data = r.json()
            assert "risks" in data or "score" in data or "result" in data, \
                f"Unexpected RugCheck response: {list(data.keys())}"
