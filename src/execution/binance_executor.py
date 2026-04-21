"""
src/execution/binance_executor.py — Binance REST API Order Executor

Handles signed order placement via Binance REST API.
Supports DRY_RUN mode (log only) and LIVE mode (real orders).
"""
from __future__ import annotations

import hashlib
import hmac
import os
import time
import urllib.parse

import requests
from loguru import logger

# ── Config ────────────────────────────────────────────────────────────────────
API_KEY    = os.getenv("BINANCE_API_KEY",    "")
API_SECRET = os.getenv("BINANCE_SECRET",     "")
DRY_RUN    = os.getenv("DRY_RUN", "True").lower() == "true"

BASE_URL   = "https://api.binance.com"
RECV_WINDOW = 5000

STOP_LOSS_PCT   = float(os.getenv("BINANCE_STOP_LOSS_PCT",   "2.0"))
TAKE_PROFIT_PCT = float(os.getenv("BINANCE_TAKE_PROFIT_PCT", "1.5"))


def _sign(params: dict) -> str:
    """Create HMAC-SHA256 signature for Binance API."""
    query = urllib.parse.urlencode(params)
    return hmac.new(API_SECRET.encode(), query.encode(), hashlib.sha256).hexdigest()


def _headers() -> dict:
    return {"X-MBX-APIKEY": API_KEY}


def _timestamp() -> int:
    return int(time.time() * 1000)


def get_account_balance(asset: str = "USDT") -> float:
    """Return free balance for given asset."""
    try:
        params = {"timestamp": _timestamp(), "recvWindow": RECV_WINDOW}
        params["signature"] = _sign(params)
        r = requests.get(f"{BASE_URL}/api/v3/account", headers=_headers(), params=params, timeout=8)
        data = r.json()
        for b in data.get("balances", []):
            if b["asset"] == asset:
                return float(b["free"])
    except Exception as e:
        logger.error(f"[EXECUTOR] Balance fetch error: {e}")
    return 0.0


def get_symbol_info(symbol: str) -> dict:
    """Fetch symbol filters (lot size, min notional, tick size)."""
    try:
        r = requests.get(f"{BASE_URL}/api/v3/exchangeInfo?symbol={symbol}", timeout=8)
        data = r.json()
        for s in data.get("symbols", []):
            if s["symbol"] == symbol:
                filters = {f["filterType"]: f for f in s["filters"]}
                return {
                    "minQty":       float(filters.get("LOT_SIZE", {}).get("minQty", 0)),
                    "stepSize":     float(filters.get("LOT_SIZE", {}).get("stepSize", 0)),
                    "minNotional":  float(filters.get("MIN_NOTIONAL", {}).get("minNotional", 5)),
                    "tickSize":     float(filters.get("PRICE_FILTER", {}).get("tickSize", 0)),
                    "baseAsset":    s.get("baseAsset", ""),
                    "quoteAsset":   s.get("quoteAsset", ""),
                }
    except Exception as e:
        logger.error(f"[EXECUTOR] Symbol info error: {e}")
    return {}


def _round_step(value: float, step: float) -> float:
    """Round value to nearest step size."""
    if step == 0:
        return value
    precision = len(str(step).rstrip("0").split(".")[-1]) if "." in str(step) else 0
    return round(round(value / step) * step, precision)


def place_market_buy(symbol: str, usdt_amount: float) -> dict | None:
    """
    Place a market BUY order for `usdt_amount` USDT worth of `symbol`.
    Returns order response dict or None on failure.
    """
    # Fetch current price
    try:
        r = requests.get(f"{BASE_URL}/api/v3/ticker/price?symbol={symbol}", timeout=5)
        price = float(r.json()["price"])
    except Exception as e:
        logger.error(f"[EXECUTOR] Price fetch failed: {e}")
        return None

    # Get symbol filters
    info = get_symbol_info(symbol)
    step = info.get("stepSize", 0)
    min_qty  = info.get("minQty", 0)
    min_not  = info.get("minNotional", 5)

    qty = usdt_amount / price
    if step:
        qty = _round_step(qty, step)

    if qty < min_qty:
        logger.warning(f"[EXECUTOR] {symbol}: Qty {qty} < minQty {min_qty}")
        return None
    if qty * price < min_not:
        logger.warning(f"[EXECUTOR] {symbol}: Notional ${qty*price:.2f} < minNotional ${min_not}")
        return None

    if DRY_RUN:
        logger.success(
            f"[EXECUTOR] 📝 DRY-RUN MARKET BUY | {symbol} | "
            f"qty={qty:.6f} | price≈${price:.4f} | total≈${qty*price:.2f}"
        )
        return {
            "symbol":       symbol,
            "orderId":      f"DRY_{int(time.time())}",
            "side":         "BUY",
            "type":         "MARKET",
            "executedQty":  str(qty),
            "cummulativeQuoteQty": str(qty * price),
            "price":        str(price),
            "status":       "FILLED",
            "dry_run":      True,
        }

    # LIVE order
    params = {
        "symbol":    symbol,
        "side":      "BUY",
        "type":      "MARKET",
        "quantity":  qty,
        "timestamp": _timestamp(),
        "recvWindow": RECV_WINDOW,
    }
    params["signature"] = _sign(params)

    try:
        r = requests.post(
            f"{BASE_URL}/api/v3/order",
            headers=_headers(),
            params=params,
            timeout=8,
        )
        data = r.json()
        if "orderId" in data:
            logger.success(
                f"[EXECUTOR] ✅ LIVE BUY FILLED | {symbol} | "
                f"orderId={data['orderId']} | qty={data.get('executedQty')} | "
                f"total=${float(data.get('cummulativeQuoteQty', 0)):.2f}"
            )
            return data
        else:
            logger.error(f"[EXECUTOR] Order failed: {data}")
            return None
    except Exception as e:
        logger.error(f"[EXECUTOR] Request error: {e}")
        return None


def place_oco_sell(symbol: str, qty: float, entry_price: float) -> dict | None:
    """
    Place OCO (One-Cancels-Other) SELL order:
    - Stop-Loss:   entry_price * (1 - STOP_LOSS_PCT/100)
    - Take-Profit: entry_price * (1 + TAKE_PROFIT_PCT/100)
    """
    info    = get_symbol_info(symbol)
    tick    = info.get("tickSize", 0.01)
    sl_price = _round_step(entry_price * (1 - STOP_LOSS_PCT / 100), tick)
    tp_price = _round_step(entry_price * (1 + TAKE_PROFIT_PCT / 100), tick)
    sl_limit = _round_step(sl_price * 0.999, tick)  # slightly below stop price

    if DRY_RUN:
        logger.info(
            f"[EXECUTOR] 📝 DRY-RUN OCO | {symbol} | qty={qty:.6f} | "
            f"TP=${tp_price:.6f} | SL=${sl_price:.6f}"
        )
        return {"dry_run": True, "symbol": symbol, "tp": tp_price, "sl": sl_price}

    params = {
        "symbol":               symbol,
        "side":                 "SELL",
        "quantity":             qty,
        "price":                tp_price,           # Take-profit limit price
        "stopPrice":            sl_price,           # Stop trigger
        "stopLimitPrice":       sl_limit,           # Stop limit price
        "stopLimitTimeInForce": "GTC",
        "timestamp":            _timestamp(),
        "recvWindow":           RECV_WINDOW,
    }
    params["signature"] = _sign(params)

    try:
        r = requests.post(
            f"{BASE_URL}/api/v3/order/oco",
            headers=_headers(),
            params=params,
            timeout=8,
        )
        data = r.json()
        if "orderListId" in data:
            logger.success(f"[EXECUTOR] ✅ OCO SET | {symbol} | TP=${tp_price:.6f} SL=${sl_price:.6f}")
            return data
        else:
            logger.error(f"[EXECUTOR] OCO failed: {data}")
            return None
    except Exception as e:
        logger.error(f"[EXECUTOR] OCO error: {e}")
        return None
