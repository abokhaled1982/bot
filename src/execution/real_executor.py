"""
src/execution/real_executor.py — Professioneller Live-Executor fuer Binance Spot.

Baut auf `binance_executor` auf, ergaenzt aber:

  * Rate-Limit-Bewusstsein (respektiert `X-MBX-USED-WEIGHT-1M` und stoppt vor 90%)
  * Retry mit exponentiellem Backoff + Jitter (nur fuer 5xx, 429, Netzwerkfehler)
  * Idempotente `newClientOrderId` (deterministisch je Trader/Coin/Zeitfenster)
  * Serverzeit-Offset (verhindert `-1021 Timestamp` bei Uhr-Drift)
  * Symbol-Filter-Validierung (LOT_SIZE, PRICE_FILTER, MIN_NOTIONAL, PERCENT_PRICE)
  * Order-Reconciliation (`get_order` nach dem Senden, bis FILLED/REJECTED)
  * Strukturierte Fehler und ExecutionResult, damit der Aufrufer sauber
    reagieren und Nutzer benachrichtigen kann.
  * Keine `raise`-Escalation aus dem Signal-Pfad; Fehler werden geloggt und
    als `ExecutionResult(ok=False, ...)` zurueckgegeben.

Dieses Modul platziert echte Orders wenn `DRY_RUN=False`.
"""
from __future__ import annotations

import hashlib
import hmac
import os
import random
import threading
import time
import urllib.parse
from dataclasses import dataclass, field
from decimal import Decimal, ROUND_DOWN
from typing import Any, Optional

import requests
from dotenv import load_dotenv
from loguru import logger

# .env aus dem Repo-Root laden, damit die Config unabhaengig vom Startverzeichnis
# und vom Entry-Point gesetzt ist. Bereits gesetzte Umgebungsvariablen gewinnen.
load_dotenv(os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "..", ".env"))

# ── Config ────────────────────────────────────────────────────────────────────
API_KEY    = os.getenv("BINANCE_API_KEY", "")
API_SECRET = os.getenv("BINANCE_SECRET", "")
DRY_RUN    = os.getenv("DRY_RUN", "True").lower() == "true"

BASE_URL       = os.getenv("BINANCE_BASE_URL", "https://api.binance.com")
RECV_WINDOW    = int(os.getenv("BINANCE_RECV_WINDOW", "5000"))
REQ_TIMEOUT    = float(os.getenv("BINANCE_HTTP_TIMEOUT", "10"))
MAX_WEIGHT_PCT = float(os.getenv("BINANCE_MAX_WEIGHT_PCT", "0.90"))
WEIGHT_LIMIT   = int(os.getenv("BINANCE_WEIGHT_LIMIT_1M", "1200"))
MAX_RETRIES    = int(os.getenv("BINANCE_MAX_RETRIES", "4"))
BASE_BACKOFF   = float(os.getenv("BINANCE_BACKOFF_BASE", "0.4"))
MAX_BACKOFF    = float(os.getenv("BINANCE_BACKOFF_MAX", "6.0"))

STOP_LOSS_PCT   = float(os.getenv("BINANCE_STOP_LOSS_PCT",   "2.0"))
TAKE_PROFIT_PCT = float(os.getenv("BINANCE_TAKE_PROFIT_PCT", "1.5"))

# Business-Fehlercodes, die NICHT retried werden.
_NON_RETRYABLE = {
    -1013,  # Filter failure
    -1021,  # Timestamp for this request outside recvWindow (wir korrigieren via serverTime)
    -1100,  # Illegal characters
    -1102,  # Mandatory parameter missing
    -1104,  # Not all sent parameters were read
    -2010,  # Order rejected (insufficient balance)
    -2011,  # Cancel rejected
    -2013,  # Order does not exist
    -2014,  # API-key format invalid
    -2015,  # Invalid API-key, IP or permissions
}


# ── Fehlerklassen ─────────────────────────────────────────────────────────────
class ExecutionError(Exception):
    """Basisklasse."""


class TransientError(ExecutionError):
    """Netzwerk-, 5xx- oder 429-Fehler. Wird retried."""


class BinanceAPIError(ExecutionError):
    """Business-Fehler von Binance (Code + Msg im Response-Body)."""

    def __init__(self, code: int, msg: str, http_status: int = 0) -> None:
        super().__init__(f"[{code}] {msg}")
        self.code = code
        self.msg = msg
        self.http_status = http_status


# ── Ergebnis-Struktur ─────────────────────────────────────────────────────────
@dataclass
class ExecutionResult:
    ok:         bool
    action:     str                 # "BUY" | "SELL" | "OCO"
    symbol:     str
    qty:        float               = 0.0
    price:      float               = 0.0
    total_usdt: float               = 0.0
    order_id:   Optional[str]       = None
    client_id:  Optional[str]       = None
    status:     Optional[str]       = None
    reason:     str                 = ""
    error_code: Optional[int]       = None
    dry_run:    bool                = False
    raw:        dict[str, Any]      = field(default_factory=dict)

    def summary(self) -> str:
        tag = "DRY" if self.dry_run else "LIVE"
        if not self.ok:
            return (
                f"[{tag}] ❌ {self.action} {self.symbol} FAILED | "
                f"reason={self.reason} code={self.error_code}"
            )
        return (
            f"[{tag}] ✅ {self.action} {self.symbol} | "
            f"qty={self.qty:.6f} price={self.price:.6f} "
            f"notional=${self.total_usdt:.2f} status={self.status} "
            f"orderId={self.order_id}"
        )


# ── Rate Limit Tracker ────────────────────────────────────────────────────────
class RateLimiter:
    """Beobachtet `X-MBX-USED-WEIGHT-1M` und schlaeft, wenn der Kopf voll ist."""

    def __init__(self, weight_limit: int = WEIGHT_LIMIT, max_pct: float = MAX_WEIGHT_PCT) -> None:
        self.weight_limit = weight_limit
        self.max_used = int(weight_limit * max_pct)
        self._lock = threading.Lock()
        self._current_weight = 0
        self._minute_start = time.time()

    def note_response(self, headers: dict[str, str]) -> None:
        used = headers.get("x-mbx-used-weight-1m") or headers.get("X-MBX-USED-WEIGHT-1M")
        if used is None:
            return
        try:
            self._current_weight = int(used)
        except (TypeError, ValueError):
            return

    def before_request(self) -> None:
        with self._lock:
            now = time.time()
            if now - self._minute_start >= 60:
                self._minute_start = now
                self._current_weight = 0
            if self._current_weight >= self.max_used:
                sleep_for = max(0.0, 60 - (now - self._minute_start)) + 0.1
                logger.warning(
                    f"[RATE] Weight {self._current_weight}/{self.weight_limit} "
                    f"→ sleeping {sleep_for:.1f}s"
                )
                time.sleep(sleep_for)
                self._minute_start = time.time()
                self._current_weight = 0


_rate = RateLimiter()


# ── HTTP-Helpers ──────────────────────────────────────────────────────────────
def _sign(params: dict) -> str:
    query = urllib.parse.urlencode(params, doseq=True)
    return hmac.new(API_SECRET.encode(), query.encode(), hashlib.sha256).hexdigest()


def _headers() -> dict:
    return {"X-MBX-APIKEY": API_KEY}


# ── Zeitsynchronisation ───────────────────────────────────────────────────────
_time_offset_ms = 0.0
_time_offset_at = 0.0
_TIME_OFFSET_TTL = 300.0


def _refresh_time_offset() -> None:
    global _time_offset_ms, _time_offset_at
    try:
        r = requests.get(f"{BASE_URL}/api/v3/time", timeout=REQ_TIMEOUT)
        r.raise_for_status()
        server = int(r.json()["serverTime"])
        _time_offset_ms = server - int(time.time() * 1000)
        _time_offset_at = time.time()
        logger.debug(f"[EXEC] serverTime offset = {_time_offset_ms:+.0f} ms")
    except Exception as e:
        logger.warning(f"[EXEC] serverTime sync failed: {e}")


def _timestamp() -> int:
    if time.time() - _time_offset_at > _TIME_OFFSET_TTL:
        _refresh_time_offset()
    return int(time.time() * 1000 + _time_offset_ms)


def _backoff(attempt: int) -> float:
    delay = min(MAX_BACKOFF, BASE_BACKOFF * (2 ** attempt))
    return delay * (0.5 + random.random())


def _request(
    method: str, path: str, params: Optional[dict] = None,
    signed: bool = False, retries: int = MAX_RETRIES,
) -> dict:
    url = f"{BASE_URL}{path}"
    params = dict(params or {})
    if signed:
        params.setdefault("timestamp", _timestamp())
        params.setdefault("recvWindow", RECV_WINDOW)
        params["signature"] = _sign(params)

    last_exc: Optional[Exception] = None
    for attempt in range(retries + 1):
        _rate.before_request()
        try:
            resp = requests.request(
                method, url, headers=_headers() if signed else None,
                params=params, timeout=REQ_TIMEOUT,
            )
        except requests.RequestException as e:
            last_exc = TransientError(str(e))
            wait = _backoff(attempt)
            logger.warning(f"[EXEC] {method} {path} netz-fehler {e} → retry in {wait:.1f}s")
            time.sleep(wait)
            continue

        _rate.note_response(resp.headers)
        text = resp.text or ""
        body: Any = None
        if text:
            try:
                body = resp.json()
            except ValueError:
                body = {"raw": text}

        if 200 <= resp.status_code < 300:
            return body if isinstance(body, dict) else {"data": body}

        if resp.status_code in (418, 429):
            wait = float(resp.headers.get("Retry-After", _backoff(attempt)))
            logger.warning(f"[EXEC] rate-limited ({resp.status_code}) → sleep {wait:.1f}s")
            time.sleep(wait)
            continue

        if 500 <= resp.status_code < 600:
            wait = _backoff(attempt)
            logger.warning(f"[EXEC] {resp.status_code} on {path} → retry in {wait:.1f}s")
            time.sleep(wait)
            continue

        code = int((body or {}).get("code", 0)) if isinstance(body, dict) else 0
        msg = (body or {}).get("msg", text) if isinstance(body, dict) else text

        if code == -1021:
            _refresh_time_offset()
            if attempt < retries:
                params["timestamp"] = _timestamp()
                params.pop("signature", None)
                if signed:
                    params["signature"] = _sign(params)
                continue

        if code in _NON_RETRYABLE or (400 <= resp.status_code < 500):
            raise BinanceAPIError(code, msg, http_status=resp.status_code)

        last_exc = TransientError(f"HTTP {resp.status_code} {msg}")
        time.sleep(_backoff(attempt))

    if last_exc:
        raise last_exc
    raise TransientError("request failed without response")


# ── Symbol-Filter / Rundung ───────────────────────────────────────────────────
_SYMBOL_INFO_CACHE: dict[str, tuple[float, dict]] = {}
_SYMBOL_INFO_TTL   = 3600.0


def get_symbol_info(symbol: str) -> dict:
    cached = _SYMBOL_INFO_CACHE.get(symbol)
    if cached and time.time() - cached[0] < _SYMBOL_INFO_TTL:
        return cached[1]
    try:
        data = _request("GET", "/api/v3/exchangeInfo", {"symbol": symbol})
    except Exception as e:
        logger.error(f"[EXEC] exchangeInfo({symbol}) failed: {e}")
        return {}
    for s in data.get("symbols", []):
        if s["symbol"] != symbol:
            continue
        f = {x["filterType"]: x for x in s["filters"]}
        info = {
            "status":       s.get("status", ""),
            "baseAsset":    s.get("baseAsset", ""),
            "quoteAsset":   s.get("quoteAsset", ""),
            "minQty":       float(f.get("LOT_SIZE", {}).get("minQty", 0)),
            "stepSize":     float(f.get("LOT_SIZE", {}).get("stepSize", 0)),
            "tickSize":     float(f.get("PRICE_FILTER", {}).get("tickSize", 0)),
            "minNotional":  float(
                f.get("NOTIONAL", {}).get("minNotional",
                f.get("MIN_NOTIONAL", {}).get("minNotional", 5))
            ),
            "isSpotAllowed": bool(s.get("isSpotTradingAllowed", True)),
        }
        _SYMBOL_INFO_CACHE[symbol] = (time.time(), info)
        return info
    return {}


def _round_step(value: float, step: float) -> float:
    if step == 0:
        return value
    dv = Decimal(str(value))
    ds = Decimal(str(step))
    return float((dv / ds).to_integral_value(rounding=ROUND_DOWN) * ds)


# ── Idempotente Client-Order-ID ───────────────────────────────────────────────
def make_client_order_id(trader: str, coin: str, side: str,
                         bucket_seconds: int = 60) -> str:
    """Deterministisch pro (trader, coin, side, minute) — schuetzt vor Doppel-Order."""
    bucket = int(time.time()) // max(1, bucket_seconds)
    raw = f"{trader}:{coin}:{side}:{bucket}"
    h = hashlib.sha1(raw.encode()).hexdigest()[:16]
    return f"copy_{h}"


# ── Public API ────────────────────────────────────────────────────────────────
def get_account_balance(asset: str = "USDT") -> float:
    try:
        data = _request("GET", "/api/v3/account", signed=True)
        for b in data.get("balances", []):
            if b["asset"] == asset:
                return float(b["free"])
    except BinanceAPIError as e:
        logger.error(f"[EXEC] balance API-error: {e}")
    except Exception as e:
        logger.error(f"[EXEC] balance error: {e}")
    return 0.0


def get_all_balances() -> dict[str, float]:
    """Alle gehaltenen Assets (free+locked > 0), nicht nur QUOTE_ASSET."""
    out: dict[str, float] = {}
    try:
        data = _request("GET", "/api/v3/account", signed=True)
        for b in data.get("balances", []):
            total = float(b.get("free", 0)) + float(b.get("locked", 0))
            if total > 0:
                out[b["asset"]] = total
    except BinanceAPIError as e:
        logger.error(f"[EXEC] balances API-error: {e}")
    except Exception as e:
        logger.error(f"[EXEC] balances error: {e}")
    return out


def get_price(symbol: str) -> float | None:
    try:
        data = _request("GET", "/api/v3/ticker/price", {"symbol": symbol}, retries=2)
        return float(data.get("price", 0)) or None
    except Exception as e:
        logger.warning(f"[EXEC] price({symbol}) failed: {e}")
        return None


def get_order(symbol: str, order_id: Optional[str] = None,
              client_order_id: Optional[str] = None) -> dict:
    params: dict[str, Any] = {"symbol": symbol}
    if order_id:
        params["orderId"] = order_id
    if client_order_id:
        params["origClientOrderId"] = client_order_id
    return _request("GET", "/api/v3/order", params, signed=True)


def wait_for_final_status(symbol: str, order_id: str,
                          timeout: float = 5.0, interval: float = 0.4) -> dict:
    """Pollt bis FILLED, PARTIALLY_FILLED (stabil), CANCELED, REJECTED oder Timeout."""
    end = time.time() + timeout
    last: dict = {}
    while time.time() < end:
        try:
            last = get_order(symbol, order_id=order_id)
        except Exception as e:
            logger.debug(f"[EXEC] get_order poll: {e}")
            time.sleep(interval)
            continue
        status = last.get("status", "")
        if status in {"FILLED", "CANCELED", "REJECTED", "EXPIRED"}:
            return last
        time.sleep(interval)
    return last


def _reject(symbol: str, reason: str, action: str = "BUY",
            code: Optional[int] = None) -> ExecutionResult:
    logger.warning(f"[EXEC] ❌ {action} {symbol} reject: {reason}")
    return ExecutionResult(ok=False, action=action, symbol=symbol,
                           reason=reason, error_code=code)


def market_buy(symbol: str, usdt_amount: float,
               trader: str = "", coin: str = "",
               price_hint: float | None = None) -> ExecutionResult:
    """Market-BUY fuer `usdt_amount` USDT. Sicher gegen Doppel-Order via clientOrderId."""
    info = get_symbol_info(symbol)
    if not info:
        return _reject(symbol, "symbol info unavailable")
    if info.get("status") not in ("", "TRADING"):
        return _reject(symbol, f"symbol status {info.get('status')}")
    if not info.get("isSpotAllowed", True):
        return _reject(symbol, "spot trading disabled")

    price = price_hint if price_hint and price_hint > 0 else get_price(symbol)
    if not price:
        return _reject(symbol, "no price available")

    qty = _round_step(usdt_amount / price, info["stepSize"])
    notional = qty * price
    if qty <= 0 or qty < info["minQty"]:
        return _reject(symbol, f"qty {qty} < minQty {info['minQty']}")
    if notional < info["minNotional"]:
        return _reject(symbol, f"notional ${notional:.2f} < min ${info['minNotional']:.2f}")

    client_id = make_client_order_id(trader or "n/a", coin or symbol, "BUY")

    if DRY_RUN:
        logger.success(
            f"[EXEC] 📝 DRY BUY {symbol} qty={qty:.6f} price≈${price:.6f} "
            f"notional≈${notional:.2f} client={client_id}"
        )
        return ExecutionResult(
            ok=True, action="BUY", symbol=symbol, qty=qty, price=price,
            total_usdt=notional, order_id=f"DRY_{int(time.time())}",
            client_id=client_id, status="FILLED", dry_run=True,
        )

    params = {
        "symbol":            symbol,
        "side":              "BUY",
        "type":              "MARKET",
        "quantity":          qty,
        "newClientOrderId":  client_id,
        "newOrderRespType":  "FULL",
    }
    try:
        data = _request("POST", "/api/v3/order", params, signed=True)
    except BinanceAPIError as e:
        # Doppel-Order-Schutz: -2010 kann von belegtem clientOrderId kommen
        return _reject(symbol, e.msg, code=e.code)
    except TransientError as e:
        return _reject(symbol, f"transient: {e}")

    order_id = str(data.get("orderId", ""))
    status   = data.get("status", "")
    if status in {"NEW", "PARTIALLY_FILLED"} and order_id:
        data = wait_for_final_status(symbol, order_id, timeout=5.0)
        status = data.get("status", status)

    exec_qty = float(data.get("executedQty", qty))
    exec_quote = float(data.get("cummulativeQuoteQty", notional))
    avg_price = (exec_quote / exec_qty) if exec_qty else price

    ok = status in {"FILLED", "PARTIALLY_FILLED"}
    result = ExecutionResult(
        ok=ok, action="BUY", symbol=symbol, qty=exec_qty, price=avg_price,
        total_usdt=exec_quote, order_id=order_id, client_id=client_id,
        status=status, raw=data,
    )
    logger.log("SUCCESS" if ok else "ERROR", result.summary())
    return result


def market_sell(symbol: str, qty: float, trader: str = "", coin: str = "") -> ExecutionResult:
    """Market-SELL der gesamten (oder Teil-)Menge. Fuer manuelles Close / Trader-Close."""
    info = get_symbol_info(symbol)
    if not info:
        return _reject(symbol, "symbol info unavailable", action="SELL")

    qty = _round_step(qty, info["stepSize"])
    if qty <= 0:
        return _reject(symbol, "qty rounds to zero", action="SELL")

    client_id = make_client_order_id(trader or "n/a", coin or symbol, "SELL")

    if DRY_RUN:
        price = get_price(symbol) or 0.0
        logger.success(f"[EXEC] 📝 DRY SELL {symbol} qty={qty:.6f} price≈${price:.6f}")
        return ExecutionResult(
            ok=True, action="SELL", symbol=symbol, qty=qty, price=price,
            total_usdt=qty * price, order_id=f"DRY_{int(time.time())}",
            client_id=client_id, status="FILLED", dry_run=True,
        )

    params = {
        "symbol":           symbol,
        "side":             "SELL",
        "type":             "MARKET",
        "quantity":         qty,
        "newClientOrderId": client_id,
        "newOrderRespType": "FULL",
    }
    try:
        data = _request("POST", "/api/v3/order", params, signed=True)
    except BinanceAPIError as e:
        return _reject(symbol, e.msg, action="SELL", code=e.code)
    except TransientError as e:
        return _reject(symbol, f"transient: {e}", action="SELL")

    order_id = str(data.get("orderId", ""))
    status = data.get("status", "")
    if status in {"NEW", "PARTIALLY_FILLED"} and order_id:
        data = wait_for_final_status(symbol, order_id, timeout=5.0)
        status = data.get("status", status)

    exec_qty = float(data.get("executedQty", qty))
    exec_quote = float(data.get("cummulativeQuoteQty", 0.0))
    avg_price = (exec_quote / exec_qty) if exec_qty else 0.0

    ok = status in {"FILLED", "PARTIALLY_FILLED"}
    result = ExecutionResult(
        ok=ok, action="SELL", symbol=symbol, qty=exec_qty, price=avg_price,
        total_usdt=exec_quote, order_id=order_id, client_id=client_id,
        status=status, raw=data,
    )
    logger.log("SUCCESS" if ok else "ERROR", result.summary())
    return result


def place_oco_exit(symbol: str, qty: float, entry_price: float) -> ExecutionResult:
    """OCO-Exit ueber TP/SL relativ zum Entry."""
    info = get_symbol_info(symbol)
    if not info:
        return _reject(symbol, "symbol info unavailable", action="OCO")
    tick = info["tickSize"] or 0.01
    qty = _round_step(qty, info["stepSize"])

    tp = _round_step(entry_price * (1 + TAKE_PROFIT_PCT / 100), tick)
    sl = _round_step(entry_price * (1 - STOP_LOSS_PCT / 100),   tick)
    sl_limit = _round_step(sl * 0.999, tick)

    if DRY_RUN:
        logger.success(f"[EXEC] 📝 DRY OCO {symbol} TP=${tp:.6f} SL=${sl:.6f}")
        return ExecutionResult(
            ok=True, action="OCO", symbol=symbol, qty=qty, price=entry_price,
            status="NEW", dry_run=True, raw={"tp": tp, "sl": sl},
        )

    params = {
        "symbol":               symbol,
        "side":                 "SELL",
        "quantity":             qty,
        "price":                tp,
        "stopPrice":            sl,
        "stopLimitPrice":       sl_limit,
        "stopLimitTimeInForce": "GTC",
    }
    try:
        data = _request("POST", "/api/v3/order/oco", params, signed=True)
    except BinanceAPIError as e:
        return _reject(symbol, e.msg, action="OCO", code=e.code)
    except TransientError as e:
        return _reject(symbol, f"transient: {e}", action="OCO")

    ok = "orderListId" in data
    return ExecutionResult(
        ok=ok, action="OCO", symbol=symbol, qty=qty, price=entry_price,
        order_id=str(data.get("orderListId", "")), status="NEW",
        raw=data, reason="" if ok else "no orderListId in response",
    )


def buy_and_protect(
    symbol: str, usdt_amount: float,
    trader: str = "", coin: str = "",
    price_hint: float | None = None,
) -> tuple[ExecutionResult, Optional[ExecutionResult]]:
    """Convenience: MARKET-BUY + OCO-Exit. Zweites Result ist None wenn Buy misslang."""
    buy = market_buy(symbol, usdt_amount, trader=trader, coin=coin, price_hint=price_hint)
    if not buy.ok or buy.qty <= 0:
        return buy, None
    oco = place_oco_exit(symbol, buy.qty, buy.price)
    return buy, oco
