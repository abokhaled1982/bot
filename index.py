"""
MarketPulse Live Engine v1.0
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
Architecture : WebSocket (real-time prices) + TA-Lib (indicators)
Signals      : STRONG_BUY / BUY / NEUTRAL / SELL / STRONG_SELL
Display      : Color-coded terminal dashboard, refreshes live
Runs 24/7    : Auto-reconnect WebSocket, never stops
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
"""

import yfinance as yf
import talib
import numpy as np
import threading
import time
import os
import sys
import logging
from datetime import datetime, timezone
from dataclasses import dataclass
from collections import deque

# ══════════════════════════════════════════════════════════
# ANSI COLORS
# ══════════════════════════════════════════════════════════
class C:
    RESET      = "\033[0m"
    BOLD       = "\033[1m"
    DIM        = "\033[2m"

    # text colors
    WHITE      = "\033[97m"
    GRAY       = "\033[90m"
    RED        = "\033[91m"
    GREEN      = "\033[92m"
    YELLOW     = "\033[93m"
    BLUE       = "\033[94m"
    MAGENTA    = "\033[95m"
    CYAN       = "\033[96m"

    # background colors
    BG_RED     = "\033[41m"
    BG_GREEN   = "\033[42m"
    BG_YELLOW  = "\033[43m"
    BG_BLUE    = "\033[44m"
    BG_MAGENTA = "\033[45m"
    BG_CYAN    = "\033[46m"
    BG_WHITE   = "\033[47m"


def colored(text: str, *codes: str) -> str:
    return "".join(codes) + str(text) + C.RESET


# ══════════════════════════════════════════════════════════
# LOGGING — suppress yfinance noise, keep our messages clean
# ══════════════════════════════════════════════════════════
logging.basicConfig(level=logging.WARNING)
log = logging.getLogger("MarketPulse")
log.setLevel(logging.INFO)

handler = logging.StreamHandler()
handler.setFormatter(logging.Formatter(
    f"{C.DIM}%(asctime)s{C.RESET} %(message)s",
    datefmt="%H:%M:%S"
))
log.addHandler(handler)
log.propagate = False


# ══════════════════════════════════════════════════════════
# SIGNAL DEFINITIONS
# ══════════════════════════════════════════════════════════
SIGNAL_STYLE = {
    "STRONG_BUY":  (C.BOLD + C.GREEN,  "▲▲ STRONG BUY "),
    "BUY":         (C.GREEN,            "▲  BUY        "),
    "NEUTRAL":     (C.GRAY,             "── NEUTRAL    "),
    "SELL":        (C.RED,              "▼  SELL       "),
    "STRONG_SELL": (C.BOLD + C.RED,     "▼▼ STRONG SELL"),
}

RSI_STYLE = {
    (0,  30):  (C.BOLD + C.GREEN,  "OVERSOLD "),
    (30, 45):  (C.GREEN,           "LOW      "),
    (45, 55):  (C.GRAY,            "NEUTRAL  "),
    (55, 70):  (C.YELLOW,          "HIGH     "),
    (70, 100): (C.BOLD + C.RED,    "OVERBOUGHT"),
}

def rsi_style(rsi: float):
    for (lo, hi), style in RSI_STYLE.items():
        if lo <= rsi < hi:
            return style
    return (C.GRAY, "NEUTRAL  ")


# ══════════════════════════════════════════════════════════
# DATA MODEL
# ══════════════════════════════════════════════════════════
@dataclass
class Snapshot:
    ticker    : str
    price     : float
    prev_price: float     # last known price (to show change)
    RSI       : float
    MACD      : float
    MACD_sig  : float
    EMA_20    : float
    EMA_50    : float
    BB_upper  : float
    BB_lower  : float
    ATR       : float
    signal    : str
    updated_at: str


def compute_signal(rsi: float, macd: float, macd_sig: float,
                   price: float, ema20: float, ema50: float) -> str:
    """
    Multi-factor signal:
      STRONG_BUY  — RSI oversold + MACD bullish cross + price above both EMAs
      BUY         — 2 of 3 bullish conditions
      STRONG_SELL — RSI overbought + MACD bearish cross + price below both EMAs
      SELL        — 2 of 3 bearish conditions
      NEUTRAL     — everything else
    """
    bullish = 0
    bearish = 0

    if rsi < 35:             bullish += 1
    elif rsi > 65:           bearish += 1

    if macd > macd_sig:      bullish += 1
    elif macd < macd_sig:    bearish += 1

    if price > ema20 > ema50: bullish += 1
    elif price < ema20 < ema50: bearish += 1

    if bullish == 3: return "STRONG_BUY"
    if bullish == 2: return "BUY"
    if bearish == 3: return "STRONG_SELL"
    if bearish == 2: return "SELL"
    return "NEUTRAL"


# ══════════════════════════════════════════════════════════
# INDICATOR ENGINE
# ══════════════════════════════════════════════════════════
class IndicatorEngine:

    def __init__(self, interval: str = "5m", period: str = "5d"):
        self.interval = interval
        self.period   = period

    def fetch(self, ticker: str, prev_price: float = 0.0) -> Snapshot | None:
        try:
            df = yf.Ticker(ticker).history(
                period=self.period, interval=self.interval
            )
            if len(df) < 30:
                return None

            c = df["Close"].values.astype(float)
            h = df["High"].values.astype(float)
            l = df["Low"].values.astype(float)
            v = df["Volume"].values.astype(float)

            rsi               = talib.RSI(c, 14)[-1]
            macd, sig, _      = talib.MACD(c, 12, 26, 9)
            ema20             = talib.EMA(c, 20)[-1]
            ema50             = talib.EMA(c, 50)[-1]
            bb_up, _, bb_lo   = talib.BBANDS(c, 20)
            atr               = talib.ATR(h, l, c, 14)[-1]

            signal = compute_signal(rsi, macd[-1], sig[-1],
                                    c[-1], ema20, ema50)

            return Snapshot(
                ticker     = ticker,
                price      = round(float(c[-1]), 4),
                prev_price = prev_price or round(float(c[-1]), 4),
                RSI        = round(float(rsi), 2),
                MACD       = round(float(macd[-1]), 4),
                MACD_sig   = round(float(sig[-1]), 4),
                EMA_20     = round(float(ema20), 4),
                EMA_50     = round(float(ema50), 4),
                BB_upper   = round(float(bb_up[-1]), 4),
                BB_lower   = round(float(bb_lo[-1]), 4),
                ATR        = round(float(atr), 4),
                signal     = signal,
                updated_at = datetime.now(timezone.utc).strftime("%H:%M:%S"),
            )
        except Exception as e:
            log.warning(f"[{ticker}] fetch error: {e}")
            return None


# ══════════════════════════════════════════════════════════
# WEBSOCKET PRICE STREAM
# ══════════════════════════════════════════════════════════
class PriceStream:
    """
    Streams real-time prices via yfinance WebSocket.
    Stores latest price per ticker in self.prices dict.
    Auto-reconnects on disconnect.
    """

    def __init__(self, tickers: list):
        self.tickers   = tickers
        self.prices    = {t: 0.0 for t in tickers}
        self.changes   = {t: 0.0 for t in tickers}
        self._running  = False
        self._thread   = None

    def _on_message(self, msg: dict):
        ticker = msg.get("id") or msg.get("symbol") or ""
        price  = msg.get("price") or msg.get("regularMarketPrice") or 0.0
        change = msg.get("changePercent") or msg.get("regularMarketChangePercent") or 0.0
        if ticker and price:
            self.prices[ticker]  = float(price)
            self.changes[ticker] = float(change)

    def _stream_loop(self):
        while self._running:
            try:
                log.info(colored("WebSocket connecting...", C.CYAN))
                with yf.WebSocket() as ws:
                    ws.subscribe(self.tickers)
                    log.info(colored(
                        f"WebSocket live — {len(self.tickers)} tickers",
                        C.BOLD + C.GREEN
                    ))
                    ws.listen(self._on_message)
            except Exception as e:
                log.warning(colored(f"WebSocket error: {e} — reconnecting in 5s", C.YELLOW))
                time.sleep(5)

    def start(self):
        self._running = True
        self._thread  = threading.Thread(
            target=self._stream_loop, daemon=True, name="WebSocket"
        )
        self._thread.start()

    def stop(self):
        self._running = False


# ══════════════════════════════════════════════════════════
# DISPLAY
# ══════════════════════════════════════════════════════════
def clear():
    os.system("cls" if os.name == "nt" else "clear")


def price_color(price: float, prev: float) -> str:
    if price > prev:  return C.GREEN
    if price < prev:  return C.RED
    return C.WHITE


def format_price(ticker: str, price: float) -> str:
    """Format price with correct decimal places per asset type."""
    if "BTC" in ticker or "ETH" in ticker:
        return f"${price:>12,.2f}"
    if price > 1000:
        return f"${price:>12,.2f}"
    return f"${price:>12,.4f}"


def render_dashboard(snapshots: dict, stream: PriceStream,
                     signals_log: deque, cycle: int):
    clear()

    now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
    ws_status = colored("● LIVE", C.BOLD + C.GREEN) if any(
        v > 0 for v in stream.prices.values()
    ) else colored("● CONNECTING", C.YELLOW)

    # ── header ──────────────────────────────────────────────
    width = 100
    print(colored("═" * width, C.CYAN))
    print(colored("  MarketPulse Live Engine v1.0", C.BOLD + C.WHITE) +
          f"  {ws_status}  " +
          colored(now, C.DIM) +
          colored(f"  cycle #{cycle}", C.DIM))
    print(colored("═" * width, C.CYAN))

    # ── column headers ───────────────────────────────────────
    print(
        colored(f"  {'TICKER':<12}", C.BOLD + C.WHITE) +
        colored(f"{'PRICE':>14}", C.BOLD + C.WHITE) +
        colored(f"{'CHG%':>8}", C.BOLD + C.WHITE) +
        colored(f"{'RSI':>8}", C.BOLD + C.WHITE) +
        colored(f"{'RSI STATE':>12}", C.BOLD + C.WHITE) +
        colored(f"{'MACD':>10}", C.BOLD + C.WHITE) +
        colored(f"{'EMA20':>10}", C.BOLD + C.WHITE) +
        colored(f"{'BB UPPER':>11}", C.BOLD + C.WHITE) +
        colored(f"{'SIGNAL':>16}", C.BOLD + C.WHITE) +
        colored(f"{'TIME':>10}", C.BOLD + C.WHITE)
    )
    print(colored("─" * width, C.DIM))

    # ── rows ─────────────────────────────────────────────────
    for ticker, snap in snapshots.items():
        if snap is None:
            print(f"  {colored(ticker, C.GRAY):<22} {colored('no data', C.DIM)}")
            continue

        # use WebSocket live price if available
        live_price = stream.prices.get(ticker, 0.0)
        display_price = live_price if live_price > 0 else snap.price
        pct_change    = stream.changes.get(ticker, 0.0)

        pcol  = price_color(display_price, snap.prev_price)
        scol, slabel = SIGNAL_STYLE.get(snap.signal, (C.GRAY, "NEUTRAL    "))
        rcol, rlabel = rsi_style(snap.RSI)

        chg_col = C.GREEN if pct_change >= 0 else C.RED
        chg_str = f"{'+' if pct_change >= 0 else ''}{pct_change:.2f}%"

        macd_col = C.GREEN if snap.MACD > snap.MACD_sig else C.RED

        print(
            colored(f"  {ticker:<12}", C.BOLD + C.WHITE) +
            colored(format_price(ticker, display_price), pcol) +
            colored(f"{chg_str:>8}", chg_col) +
            colored(f"{snap.RSI:>8.1f}", rcol) +
            colored(f"{rlabel:>12}", rcol) +
            colored(f"{snap.MACD:>10.4f}", macd_col) +
            colored(f"{snap.EMA_20:>10.2f}", C.WHITE) +
            colored(f"{snap.BB_upper:>11.2f}", C.DIM) +
            colored(f"  {slabel}", scol) +
            colored(f"{snap.updated_at:>10}", C.DIM)
        )

    print(colored("─" * width, C.DIM))

    # ── signal legend ────────────────────────────────────────
    print()
    print(colored("  SIGNALS: ", C.BOLD + C.WHITE), end="")
    for sig, (col, label) in SIGNAL_STYLE.items():
        print(colored(f" {label.strip()} ", col), end="  ")
    print()
    print()

    # ── recent signal alerts ─────────────────────────────────
    if signals_log:
        print(colored("  RECENT ALERTS", C.BOLD + C.WHITE))
        print(colored("  " + "─" * 70, C.DIM))
        for alert in list(signals_log)[-6:]:
            print(f"  {alert}")
    print()
    print(colored(
        "  Press Ctrl+C to stop",
        C.DIM
    ))


# ══════════════════════════════════════════════════════════
# ALERT LOGGER
# ══════════════════════════════════════════════════════════
def format_alert(snap: Snapshot) -> str | None:
    """Returns a colored alert string for notable signals."""
    if snap.signal in ("STRONG_BUY", "STRONG_SELL"):
        col, label = SIGNAL_STYLE[snap.signal]
        now = datetime.now(timezone.utc).strftime("%H:%M:%S")
        return (
            colored(f"[{now}]", C.DIM) + " " +
            colored(f"{snap.ticker:<10}", C.BOLD + C.WHITE) +
            colored(f"{label}", col) +
            colored(f"  RSI={snap.RSI:.1f}", C.WHITE) +
            colored(f"  MACD={'▲' if snap.MACD > snap.MACD_sig else '▼'}", C.WHITE) +
            colored(f"  ${snap.price}", C.WHITE)
        )
    return None


# ══════════════════════════════════════════════════════════
# MAIN LOOP
# ══════════════════════════════════════════════════════════
class LiveEngine:

    TICKERS = [
        # stocks
        "AAPL", "MSFT", "NVDA", "TSLA", "GOOGL",
        "AMZN", "META", "AMD",  "SPY",  "QQQ",
        # crypto
        "BTC-USD", "ETH-USD",
    ]

    def __init__(self,
                 refresh_sec: int  = 30,
                 interval:    str  = "5m",
                 period:      str  = "5d"):
        self.refresh_sec = refresh_sec
        self.engine      = IndicatorEngine(interval, period)
        self.stream      = PriceStream(self.TICKERS)
        self.snapshots   = {t: None for t in self.TICKERS}
        self.signals_log = deque(maxlen=20)
        self.cycle       = 0
        self._prev_prices = {t: 0.0 for t in self.TICKERS}

    def _refresh_indicators(self):
        """Fetch fresh indicators for all tickers in parallel."""
        threads = []
        results = {}

        def _fetch(ticker):
            results[ticker] = self.engine.fetch(
                ticker,
                prev_price=self._prev_prices.get(ticker, 0.0)
            )

        for t in self.TICKERS:
            th = threading.Thread(target=_fetch, args=(t,), daemon=True)
            threads.append(th)
            th.start()
        for th in threads:
            th.join(timeout=15)

        for ticker, snap in results.items():
            if snap:
                # save prev price before updating
                if self.snapshots[ticker]:
                    self._prev_prices[ticker] = self.snapshots[ticker].price
                self.snapshots[ticker] = snap

                # log strong signals
                alert = format_alert(snap)
                if alert:
                    self.signals_log.append(alert)

    def start(self):
        # start WebSocket in background
        self.stream.start()

        # initial fetch before first render
        log.info(colored("Fetching initial indicators...", C.CYAN))
        self._refresh_indicators()

        self.cycle = 1
        last_refresh = time.time()

        try:
            while True:
                # render dashboard
                render_dashboard(
                    self.snapshots,
                    self.stream,
                    self.signals_log,
                    self.cycle
                )

                # refresh indicators every refresh_sec
                elapsed = time.time() - last_refresh
                if elapsed >= self.refresh_sec:
                    self._refresh_indicators()
                    last_refresh = time.time()
                    self.cycle  += 1

                # re-render every 2s to show live WebSocket prices
                time.sleep(2)

        except KeyboardInterrupt:
            self.stream.stop()
            clear()
            print(colored("\n  MarketPulse stopped.\n", C.YELLOW))
            sys.exit(0)


# ══════════════════════════════════════════════════════════
# ENTRY POINT
# ══════════════════════════════════════════════════════════
if __name__ == "__main__":
    print(colored("Starting MarketPulse Live Engine...", C.CYAN))
    LiveEngine(
        refresh_sec = 30,    # re-fetch indicators every 30s
        interval    = "5m",  # candle size for indicator calc
        period      = "5d",  # how much history to load
    ).start()