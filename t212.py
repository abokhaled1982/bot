"""
Trading 212 Trading Engine
══════════════════════════
Connects to the Trading 212 Public API (v0) and provides:
  • Account summary (cash, invested, P&L)
  • Open positions (portfolio)
  • Instrument search
  • Place market orders (buy / sell)
  • Place limit orders (buy / sell)
  • List & cancel open orders
  • Interactive CLI menu

Credentials are loaded from .env:
  T212_API_KEY    — your API key
  T212_API_SECRET — your API secret
  T212_MODE       — "demo" (paper) or "live" (real money)
"""

import os
import base64
import json
import time
import logging
import requests
from datetime import datetime, timezone
from dotenv import load_dotenv

load_dotenv()

# ══════════════════════════════════════════════════════════
# LOGGING
# ══════════════════════════════════════════════════════════
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
)
log = logging.getLogger("T212")


# ══════════════════════════════════════════════════════════
# ANSI COLORS  (terminal styling)
# ══════════════════════════════════════════════════════════
class C:
    R  = "\033[0m"
    B  = "\033[1m"
    DIM= "\033[2m"
    GR = "\033[92m"
    RD = "\033[91m"
    YL = "\033[93m"
    CY = "\033[96m"
    WH = "\033[97m"
    GY = "\033[90m"

def g(text, *codes):
    return "".join(codes) + str(text) + C.R


# ══════════════════════════════════════════════════════════
# TRADING 212 CLIENT
# ══════════════════════════════════════════════════════════
class T212Client:
    """
    Thin wrapper around the Trading 212 Public API v0.

    Authentication: HTTP Basic Auth — Base64(API_KEY:API_SECRET)
    Base URL switches between demo and live based on T212_MODE env var.
    """

    BASE_URLS = {
        "demo": "https://demo.trading212.com/api/v0",
        "live": "https://live.trading212.com/api/v0",
    }

    def __init__(self):
        key    = os.getenv("T212_API_KEY",    "").strip()
        secret = os.getenv("T212_API_SECRET", "").strip()
        mode   = os.getenv("T212_MODE",       "live").lower()

        if not key or not secret:
            raise ValueError("T212_API_KEY and T212_API_SECRET must both be set in .env")

        if mode not in self.BASE_URLS:
            raise ValueError("T212_MODE must be 'demo' or 'live'")

        self.mode     = mode
        self.base_url = self.BASE_URLS[mode]

        # Build Basic Auth: Base64(key:secret)
        encoded = base64.b64encode(f"{key}:{secret}".encode()).decode()
        self._headers = {
            "Authorization": f"Basic {encoded}",
            "Content-Type":  "application/json",
        }

        log.info(g(f"T212 Client ready — MODE: {mode.upper()}  →  {self.base_url}", C.CY))

    # ── internal HTTP helpers ──────────────────────────────

    def _get(self, path: str, params: dict = None) -> dict | list:
        url = self.base_url + path
        r   = requests.get(url, headers=self._headers, params=params, timeout=15)
        self._raise(r)
        return r.json()

    def _post(self, path: str, body: dict) -> dict:
        url = self.base_url + path
        r   = requests.post(url, headers=self._headers, json=body, timeout=15)
        self._raise(r)
        return r.json()

    def _delete(self, path: str) -> dict | None:
        url = self.base_url + path
        r   = requests.delete(url, headers=self._headers, timeout=15)
        if r.status_code == 200:
            try:
                return r.json()
            except Exception:
                return {}
        self._raise(r)

    @staticmethod
    def _raise(r: requests.Response):
        if r.status_code not in (200, 201, 204):
            try:
                detail = r.json()
            except Exception:
                detail = r.text
            raise RuntimeError(
                f"HTTP {r.status_code} — {detail}"
            )

    # ── Account ───────────────────────────────────────────

    def account_summary(self) -> dict:
        """Cash, invested, result, total value."""
        return self._get("/equity/account/summary")

    # ── Portfolio ─────────────────────────────────────────

    def portfolio(self) -> list:
        """All open positions."""
        data = self._get("/equity/portfolio")
        # API may return {"items": [...]}  or  a raw list
        if isinstance(data, list):
            return data
        return data.get("items", data)

    def position(self, ticker: str) -> dict | None:
        """Single position by ticker."""
        try:
            return self._get(f"/equity/portfolio/{ticker}")
        except RuntimeError:
            return None

    # ── Instruments ───────────────────────────────────────

    def instruments(self) -> list:
        """Full list of tradable instruments."""
        data = self._get("/equity/metadata/instruments")
        if isinstance(data, list):
            return data
        return data.get("items", data)

    def search_instrument(self, query: str) -> list:
        """Filter instruments by ticker or name (case-insensitive)."""
        q    = query.upper()
        instr = self.instruments()
        return [
            i for i in instr
            if q in i.get("ticker", "").upper()
            or q in i.get("name", "").upper()
        ]

    # ── Orders ────────────────────────────────────────────

    def open_orders(self) -> list:
        """All open / pending orders."""
        data = self._get("/equity/orders")
        if isinstance(data, list):
            return data
        return data.get("items", data)

    def place_market_order(self, ticker: str, quantity: float) -> dict:
        """
        Market order.
        quantity > 0  → BUY
        quantity < 0  → SELL
        """
        body = {"ticker": ticker, "quantity": quantity}
        return self._post("/equity/orders/market", body)

    def place_limit_order(
        self,
        ticker:    str,
        quantity:  float,
        limit_price: float,
        time_validity: str = "DAY",   # DAY | GOOD_TILL_CANCEL
    ) -> dict:
        """
        Limit order.
        quantity > 0  → BUY limit
        quantity < 0  → SELL limit
        """
        body = {
            "ticker":       ticker,
            "quantity":     quantity,
            "limitPrice":   limit_price,
            "timeValidity": time_validity,
        }
        return self._post("/equity/orders/limit", body)

    def place_stop_order(
        self,
        ticker:    str,
        quantity:  float,
        stop_price: float,
        time_validity: str = "DAY",
    ) -> dict:
        body = {
            "ticker":       ticker,
            "quantity":     quantity,
            "stopPrice":    stop_price,
            "timeValidity": time_validity,
        }
        return self._post("/equity/orders/stop", body)

    def cancel_order(self, order_id: int) -> dict | None:
        """Cancel a pending order by its ID."""
        return self._delete(f"/equity/orders/{order_id}")

    # ── Exchanges ─────────────────────────────────────────

    def exchanges(self) -> list:
        data = self._get("/equity/metadata/exchanges")
        if isinstance(data, list):
            return data
        return data.get("items", data)


# ══════════════════════════════════════════════════════════
# DISPLAY HELPERS
# ══════════════════════════════════════════════════════════

def sep(char="═", width=65):
    print(g(char * width, C.CY))

def header(title: str):
    sep()
    print(g(f"  {title}", C.B + C.WH))
    sep("─")

def fmt_money(v) -> str:
    try:
        f = float(v)
        col = C.GR if f >= 0 else C.RD
        return g(f"{f:>12,.2f}", col)
    except (TypeError, ValueError):
        return str(v)

def fmt_pct(v) -> str:
    try:
        f = float(v)
        col = C.GR if f >= 0 else C.RD
        sign = "+" if f >= 0 else ""
        return g(f"{sign}{f:.2f}%", col)
    except (TypeError, ValueError):
        return str(v)


def show_account(summary: dict):
    header("ACCOUNT SUMMARY")
    cash = summary.get("cash", {})
    inv  = summary.get("investments", {})

    rows = [
        ("Available to Trade", cash.get("availableToTrade", "N/A")),
        ("Reserved for Orders", cash.get("reservedForOrders", "N/A")),
        ("Cash in Pies",       cash.get("inPies", "N/A")),
        ("Investments Cost",   inv.get("totalCost", "N/A")),
        ("Current Value",      inv.get("currentValue", "N/A")),
        ("Realized P&L",       inv.get("realizedProfitLoss", "N/A")),
        ("Unrealized P&L",     inv.get("unrealizedProfitLoss", "N/A")),
        ("Total Account Value",summary.get("totalValue", "N/A")),
    ]
    for label, val in rows:
        print(f"  {g(label + ':', C.GY):<32} {fmt_money(val)}")
    sep("─")


def show_portfolio(positions: list):
    header(f"OPEN POSITIONS  ({len(positions)})")
    if not positions:
        print(g("  No open positions.", C.GY))
        sep("─")
        return

    col_w = [14, 10, 12, 12, 12, 12, 10]
    cols  = ["TICKER", "QTY", "AVG PRICE", "CUR PRICE", "P&L", "P&L %", "VALUE"]
    header_row = "  " + "".join(
        g(c.ljust(col_w[i]), C.B + C.WH) for i, c in enumerate(cols)
    )
    print(header_row)
    print(g("  " + "─" * sum(col_w), C.DIM))

    for p in positions:
        ticker    = p.get("ticker", "?")
        qty       = p.get("quantity", 0)
        avg_price = p.get("averagePrice", 0)
        cur_price = p.get("currentPrice", 0)
        pnl       = p.get("ppl", p.get("result", 0))
        pnl_pct   = (float(pnl) / (float(avg_price) * float(qty)) * 100) if avg_price and qty else 0
        value     = float(cur_price) * float(qty) if cur_price and qty else 0

        row = (
            f"  {g(str(ticker)[:13], C.WH):<23}"
            f"{str(qty)[:9]:<10}"
            f"{fmt_money(avg_price):<22}"
            f"{fmt_money(cur_price):<22}"
            f"{fmt_money(pnl):<22}"
            f"{fmt_pct(pnl_pct):<18}"
            f"{fmt_money(value)}"
        )
        print(row)

    sep("─")


def show_orders(orders: list):
    header(f"OPEN ORDERS  ({len(orders)})")
    if not orders:
        print(g("  No open orders.", C.GY))
        sep("─")
        return

    for o in orders:
        oid    = o.get("id", "?")
        ticker = o.get("ticker", "?")
        otype  = o.get("type", "?")
        qty    = o.get("quantity", "?")
        price  = o.get("limitPrice", o.get("stopPrice", "MARKET"))
        status = o.get("status", "?")
        col    = C.GR if float(qty if qty else 0) >= 0 else C.RD
        side   = g("BUY", C.GR) if float(qty if qty else 0) >= 0 else g("SELL", C.RD)

        print(
            f"  {g(str(oid), C.GY):<12}"
            f"{g(ticker, C.WH):<18}"
            f"{side:<16}"
            f"{otype:<12}"
            f"qty={g(str(qty), col):<14}"
            f"@{str(price):<12}"
            f"[{g(status, C.YL)}]"
        )
    sep("─")


def show_instruments(results: list, limit: int = 20):
    header(f"INSTRUMENT SEARCH  (showing {min(len(results), limit)} of {len(results)})")
    if not results:
        print(g("  No instruments found.", C.GY))
        sep("─")
        return

    print(f"  {g('TICKER', C.B+C.WH):<24}{g('NAME', C.B+C.WH):<40}{g('TYPE', C.B+C.WH):<14}{g('CURRENCY', C.B+C.WH)}")
    print(g("  " + "─" * 80, C.DIM))
    for i in results[:limit]:
        ticker   = str(i.get("ticker",       "?"))[:12]
        name     = str(i.get("name",         "?"))[:38]
        itype    = str(i.get("type",         "?"))[:12]
        currency = str(i.get("currencyCode", "?"))[:8]
        print(f"  {g(ticker, C.CY):<24}{name:<40}{g(itype, C.GY):<14}{currency}")
    sep("─")


# ══════════════════════════════════════════════════════════
# INTERACTIVE CLI MENU
# ══════════════════════════════════════════════════════════

def cli_menu(client: T212Client):
    MENU = """
  1  →  Account summary
  2  →  Portfolio (open positions)
  3  →  Open orders
  4  →  Search instrument
  5  →  Place MARKET order
  6  →  Place LIMIT order
  7  →  Cancel order
  8  →  List exchanges
  0  →  Exit
"""
    while True:
        sep()
        mode_label = g(client.mode.upper(), C.GR if client.mode == "live" else C.YL)
        print(g(f"  MarketPulse  ·  Trading 212  [{mode_label}]", C.B + C.WH))
        print(MENU)
        choice = input(g("  Your choice: ", C.CY)).strip()

        try:
            # ── 1. Account ───────────────────────────────────
            if choice == "1":
                show_account(client.account_summary())

            # ── 2. Portfolio ─────────────────────────────────
            elif choice == "2":
                show_portfolio(client.portfolio())

            # ── 3. Open orders ───────────────────────────────
            elif choice == "3":
                show_orders(client.open_orders())

            # ── 4. Search instrument ─────────────────────────
            elif choice == "4":
                q = input(g("  Search (ticker or name): ", C.CY)).strip()
                if q:
                    results = client.search_instrument(q)
                    show_instruments(results)

            # ── 5. Market order ──────────────────────────────
            elif choice == "5":
                ticker = input(g("  Ticker (e.g. AAPL_US_EQ): ", C.CY)).strip().upper()
                qty_str = input(g("  Quantity (positive=BUY, negative=SELL): ", C.CY)).strip()
                qty = float(qty_str)
                confirm = input(
                    g(f"\n  ⚠  {'BUY' if qty>0 else 'SELL'} {abs(qty)} × {ticker} at MARKET.  Confirm? (yes/no): ", C.YL)
                ).strip().lower()
                if confirm == "yes":
                    result = client.place_market_order(ticker, qty)
                    print(g("\n  ✅ Order placed:", C.GR))
                    print(json.dumps(result, indent=4))
                else:
                    print(g("  Cancelled.", C.GY))

            # ── 6. Limit order ───────────────────────────────
            elif choice == "6":
                ticker  = input(g("  Ticker (e.g. AAPL_US_EQ): ", C.CY)).strip().upper()
                qty_str = input(g("  Quantity (positive=BUY, negative=SELL): ", C.CY)).strip()
                lp_str  = input(g("  Limit price: ", C.CY)).strip()
                tv      = input(g("  Time validity [DAY / GOOD_TILL_CANCEL] (default DAY): ", C.CY)).strip() or "DAY"
                qty     = float(qty_str)
                lp      = float(lp_str)
                confirm = input(
                    g(f"\n  ⚠  {'BUY' if qty>0 else 'SELL'} {abs(qty)} × {ticker} @ limit {lp}.  Confirm? (yes/no): ", C.YL)
                ).strip().lower()
                if confirm == "yes":
                    result = client.place_limit_order(ticker, qty, lp, tv)
                    print(g("\n  ✅ Order placed:", C.GR))
                    print(json.dumps(result, indent=4))
                else:
                    print(g("  Cancelled.", C.GY))

            # ── 7. Cancel order ──────────────────────────────
            elif choice == "7":
                show_orders(client.open_orders())
                oid_str = input(g("  Order ID to cancel: ", C.CY)).strip()
                if oid_str:
                    confirm = input(g(f"  Cancel order {oid_str}? (yes/no): ", C.YL)).strip().lower()
                    if confirm == "yes":
                        client.cancel_order(int(oid_str))
                        print(g("  ✅ Order cancelled.", C.GR))
                    else:
                        print(g("  Cancelled.", C.GY))

            # ── 8. Exchanges ─────────────────────────────────
            elif choice == "8":
                header("EXCHANGES")
                for ex in client.exchanges():
                    print(f"  {g(str(ex.get('name','?')), C.WH):<30} {g(str(ex.get('id','?')), C.GY)}")
                sep("─")

            # ── 0. Exit ──────────────────────────────────────
            elif choice == "0":
                print(g("\n  Goodbye.\n", C.YL))
                break

            else:
                print(g("  Unknown option.", C.GY))

        except RuntimeError as e:
            print(g(f"\n  ❌ API Error: {e}\n", C.RD))
        except KeyboardInterrupt:
            print(g("\n\n  Interrupted.\n", C.YL))
            break
        except Exception as e:
            print(g(f"\n  ❌ Error: {e}\n", C.RD))

        input(g("  [Press Enter to continue]", C.DIM))


# ══════════════════════════════════════════════════════════
# ENTRY POINT
# ══════════════════════════════════════════════════════════
if __name__ == "__main__":
    print(g("\n  Starting Trading 212 client…\n", C.CY))
    try:
        c = T212Client()
        cli_menu(c)
    except ValueError as e:
        print(g(f"\n  ❌ Config error: {e}\n", C.RD))
