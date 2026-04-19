"""
AlphaEngine v2.0  —  MarketPulse Decision Brain
════════════════════════════════════════════════════════════════════
What's new in v2.0 (based on expert feedback):

  FIX 1 — AI Sentiment (Gemini 2.0 Flash)
    • SentimentScorer sends batches of headlines to Gemini
    • Returns a precise float between -1.0 and +1.0
    • Understands nuance: "beats but warns of losses" → negative
    • Falls back to keyword scoring if Gemini is unavailable

  FIX 2 — yfinance Rate-Limit Protection
    • Random jitter between thread launches (0.1–0.5s)
    • Exponential backoff on 429 / connection errors (3 retries)
    • Pluggable provider: swap _fetch_raw() for Polygon/Alpaca

  FIX 3 — In-Memory News Cache
    • NewsCache loads CSV once at startup
    • Background thread tails file for new rows only
    • No disk read per cycle — O(1) deque lookup
    • Ready to swap for Redis/SQLite with one line

.env keys needed:
  T212_API_KEY      Trading 212 API key
  T212_API_SECRET   Trading 212 secret (optional)
  T212_MODE         demo | live
  GEMINI_API_KEY    Free key from aistudio.google.com

Usage:
  python alpha_engine.py
════════════════════════════════════════════════════════════════════
"""

from __future__ import annotations

import csv
import json
import logging
import os
import random
import threading
import time
from collections import defaultdict, deque
from dataclasses import dataclass, field
from datetime import datetime, timezone, timedelta
from pathlib import Path

import base64
import numpy as np
import requests
import yfinance as yf
import talib
from dotenv import load_dotenv

load_dotenv()

# ════════════════════════════════════════════════════════════════
# LOGGING
# ════════════════════════════════════════════════════════════════
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[
        logging.StreamHandler(),
        logging.FileHandler("alpha_engine.log", encoding="utf-8"),
    ],
)
log = logging.getLogger("AlphaEngine")


# ════════════════════════════════════════════════════════════════
# ANSI COLORS
# ════════════════════════════════════════════════════════════════
class C:
    R  = "\033[0m";  B   = "\033[1m";  DIM = "\033[2m"
    GR = "\033[92m"; RD  = "\033[91m"; YL  = "\033[93m"
    CY = "\033[96m"; WH  = "\033[97m"; GY  = "\033[90m"
    MG = "\033[95m"

def g(text, *codes):
    return "".join(codes) + str(text) + C.R


# ════════════════════════════════════════════════════════════════
# CONFIG
# ════════════════════════════════════════════════════════════════
class Config:
    TICKER_MAP: dict = {
        "AAPL":  "AAPL_US_EQ",
        "MSFT":  "MSFT_US_EQ",
        "NVDA":  "NVDA_US_EQ",
        "TSLA":  "TSLA_US_EQ",
        "GOOGL": "GOOGL_US_EQ",
        "AMZN":  "AMZN_US_EQ",
        "META":  "META_US_EQ",
        "AMD":   "AMD_US_EQ",
    }
    # Signal weights — must sum to 1.0
    WEIGHT_TA         = 0.50
    WEIGHT_SENTIMENT  = 0.40   # higher now Gemini is accurate
    WEIGHT_MOMENTUM   = 0.10

    BUY_THRESHOLD     = 0.62
    SELL_THRESHOLD    = 0.38

    MAX_POSITION_PCT  = 0.10   # 10% of free cash per trade
    MAX_OPEN_TRADES   = 5
    STOP_LOSS_PCT     = 0.03
    TAKE_PROFIT_PCT   = 0.06
    MIN_SHARES        = 0.01

    SCAN_INTERVAL_SEC = 60
    COOLDOWN_SEC      = 300
    NEWS_LOOKBACK_MIN = 60

    # yfinance rate-limit protection
    YF_JITTER         = (0.1, 0.5)   # (min_sec, max_sec) random sleep per thread
    YF_MAX_RETRIES    = 3
    YF_RETRY_BACKOFF  = 2.0

    # Gemini AI
    GEMINI_API_KEY    = os.getenv("GEMINI_API_KEY", "")
    GEMINI_MODEL      = "gemini-2.0-flash"
    GEMINI_MAX_TITLES = 10
    GEMINI_TIMEOUT    = 10

    # Paths
    NEWS_CSV          = "market_news_warehouse.csv"
    AUDIT_CSV         = "alpha_audit.csv"

    # T212
    T212_MODE         = os.getenv("T212_MODE", "demo")
    T212_KEY          = os.getenv("T212_API_KEY", "")
    T212_SECRET       = os.getenv("T212_API_SECRET", "")


# ════════════════════════════════════════════════════════════════
# DATA MODELS
# ════════════════════════════════════════════════════════════════
@dataclass
class TASnapshot:
    ticker: str;   price: float;    rsi: float
    macd: float;   macd_sig: float; ema20: float
    ema50: float;  bb_upper: float; bb_lower: float; atr: float

@dataclass
class SentimentResult:
    ticker: str;    score: float;  item_count: int
    top_title: str = "";  method: str = "keyword"

@dataclass
class TradeDecision:
    ticker: str;         t212_ticker: str;    action: str
    combined_score: float; ta_score: float;   sent_score: float
    momentum: float;     quantity: float;     price: float
    reason: str
    timestamp: str = field(
        default_factory=lambda: datetime.now(timezone.utc).isoformat()
    )


# ════════════════════════════════════════════════════════════════
# FIX 3 — IN-MEMORY NEWS CACHE
# ════════════════════════════════════════════════════════════════
class NewsCache:
    """
    Loads CSV once at startup, then tails the file on a background
    thread so new articles are appended to memory without re-reading
    the whole file from disk each cycle.

    Scale-up path: replace _tail_loop() with Redis XREAD or Kafka consumer.
    """

    def __init__(self, csv_path: str, maxlen: int = 50_000):
        self.path      = Path(csv_path)
        self._lock     = threading.Lock()
        self._items    = deque(maxlen=maxlen)
        self._file_pos = 0
        self._load()
        threading.Thread(target=self._tail_loop, daemon=True, name="NewsTail").start()

    def _load(self):
        if not self.path.exists():
            log.warning(f"NewsCache: {self.path} not found — start main.py first")
            return
        try:
            with open(self.path, encoding="utf-8") as f:
                count = sum(1 for row in csv.DictReader(f)
                            if not self._items.append(row))
                self._file_pos = f.tell()
            log.info(g(f"NewsCache: {len(self._items):,} articles loaded", C.CY))
        except Exception as e:
            log.error(f"NewsCache load error: {e}")

    def _tail_loop(self):
        while True:
            try:
                if self.path.exists():
                    with open(self.path, encoding="utf-8") as f:
                        f.seek(self._file_pos)
                        new_rows = list(csv.DictReader(f))
                        if new_rows:
                            with self._lock:
                                for row in new_rows:
                                    self._items.append(row)
                        self._file_pos = f.tell()
            except Exception:
                pass
            time.sleep(5)

    def get_recent(self, ticker: str, minutes: int) -> list[dict]:
        cutoff = datetime.now(timezone.utc) - timedelta(minutes=minutes)
        out = []
        with self._lock:
            for item in self._items:
                try:
                    ts = datetime.fromisoformat(
                        item.get("fetched_at", "").replace("Z", "+00:00")
                    )
                    if ts >= cutoff and ticker.upper() in item.get("title", "").upper():
                        out.append(item)
                except Exception:
                    pass
        return out


# ════════════════════════════════════════════════════════════════
# FIX 1 — GEMINI AI SENTIMENT
# ════════════════════════════════════════════════════════════════
class GeminiScorer:
    """
    Sends news headlines to Gemini 2.0 Flash, gets back a float
    between -1.0 and +1.0 with full context awareness.

    Example nuance keyword scoring gets wrong:
      "Apple beats estimates but warns of severe future losses"
      Keywords: +1 (beats) → positive
      Gemini:   -0.4 → negative (future warning outweighs beat)
    """

    ENDPOINT = (
        "https://generativelanguage.googleapis.com/v1beta/models/"
        "{model}:generateContent?key={key}"
    )

    SYSTEM_PROMPT = """You are a financial sentiment analysis expert.
You will receive a list of news headlines about a specific stock ticker.
Analyze the OVERALL sentiment and return ONLY a JSON object:
{"score": <float -1.0 to 1.0>, "reasoning": "<one sentence>"}

Score guide:
  -1.0 = extremely bearish (major scandal, bankruptcy, huge loss)
  -0.5 = moderately bearish (missed earnings, guidance cut, lawsuit)
   0.0 = neutral
  +0.5 = moderately bullish (beat earnings, new product, upgrade)
  +1.0 = extremely bullish (record profits, major win, acquisition)

Important: Use full context. "beats but warns" = negative or neutral.
Return ONLY raw JSON — no markdown, no explanation outside the JSON."""

    def __init__(self, cfg: Config):
        self.cfg       = cfg
        self.available = bool(cfg.GEMINI_API_KEY)
        if not self.available:
            log.warning(g(
                "GEMINI_API_KEY not set — sentiment uses keyword fallback. "
                "Free key: aistudio.google.com", C.YL
            ))

    def score(self, ticker: str, titles: list) -> float | None:
        if not self.available or not titles:
            return None

        prompt = (
            f"Ticker: {ticker}\nHeadlines:\n" +
            "\n".join(f"- {t}" for t in titles[: self.cfg.GEMINI_MAX_TITLES])
        )
        url  = self.ENDPOINT.format(
            model=self.cfg.GEMINI_MODEL, key=self.cfg.GEMINI_API_KEY
        )
        body = {
            "system_instruction": {"parts": [{"text": self.SYSTEM_PROMPT}]},
            "contents":           [{"parts": [{"text": prompt}]}],
            "generationConfig":   {"temperature": 0.1, "maxOutputTokens": 150},
        }
        try:
            r    = requests.post(url, json=body, timeout=self.cfg.GEMINI_TIMEOUT)
            r.raise_for_status()
            text = (r.json()
                    .get("candidates", [{}])[0]
                    .get("content", {})
                    .get("parts", [{}])[0]
                    .get("text", "").strip()
                    .replace("```json", "").replace("```", "").strip())
            data  = json.loads(text)
            score = max(-1.0, min(1.0, float(data["score"])))
            log.debug(f"Gemini [{ticker}] score={score:.2f}  {data.get('reasoning','')[:60]}")
            return score
        except requests.exceptions.Timeout:
            log.warning(f"Gemini timeout [{ticker}] — keyword fallback")
            return None
        except Exception as e:
            log.debug(f"Gemini error [{ticker}]: {e}")
            return None


# ════════════════════════════════════════════════════════════════
# SENTIMENT SCORER (Gemini + keyword fallback)
# ════════════════════════════════════════════════════════════════
class SentimentScorer:

    BULLISH = {
        "beats","beat","record","profit","surge","soars","jumps","rallies",
        "upgrades","upgrade","buy","outperform","strong","growth","raises",
        "guidance","breakout","bullish","boom","partnership","deal","acquire",
        "rate cut","stimulus","dovish","eases","recovery","etf approved",
        "adoption","halving",
    }
    BEARISH = {
        "misses","miss","loss","losses","falls","drops","plunges","crash",
        "downgrade","downgrades","sell","underperform","weak","cuts","warning",
        "bearish","bankruptcy","fraud","investigation","lawsuit","fine",
        "rate hike","inflation","hawkish","recession","layoffs","tariff",
        "ban","hack","exploit","sec charges",
    }

    def __init__(self, cfg: Config, cache: NewsCache):
        self.cfg    = cfg
        self.cache  = cache
        self.gemini = GeminiScorer(cfg)

    def score_ticker(self, ticker: str) -> SentimentResult:
        items = self.cache.get_recent(ticker, self.cfg.NEWS_LOOKBACK_MIN)
        if not items:
            return SentimentResult(ticker=ticker, score=0.0,
                                   item_count=0, method="none")

        titles = [i.get("title", "") for i in items if i.get("title")]
        top    = max(titles, key=len, default="")

        # Try Gemini first
        gscore = self.gemini.score(ticker, titles)
        if gscore is not None:
            return SentimentResult(ticker=ticker, score=gscore,
                                   item_count=len(items),
                                   top_title=top[:80], method="gemini")

        # Keyword fallback
        scores = [self._kw(t) for t in titles]
        avg    = float(np.mean(scores)) if scores else 0.0
        return SentimentResult(ticker=ticker,
                               score=max(-1.0, min(1.0, avg)),
                               item_count=len(items),
                               top_title=top[:80], method="keyword")

    def _kw(self, title: str) -> float:
        t = title.lower()
        s = sum(1.0 for kw in self.BULLISH if kw in t)
        s -= sum(1.0 for kw in self.BEARISH if kw in t)
        return max(-3.0, min(3.0, s)) / 3.0


# ════════════════════════════════════════════════════════════════
# FIX 2 — TA SCORER WITH RATE-LIMIT PROTECTION
# ════════════════════════════════════════════════════════════════
class TAScorer:
    """
    Wraps yfinance with:
    - Random jitter between threads to avoid IP bans
    - Exponential backoff on 429 / network errors
    - Pluggable: swap _fetch_raw() for any price feed
    """

    def __init__(self, cfg: Config):
        self.cfg = cfg

    def score_ticker(self, ticker: str) -> tuple:
        snap = self._with_retry(ticker)
        if snap is None:
            return 0.5, None

        score = 0.5
        if snap.rsi < 30:                          score += 0.20
        elif snap.rsi < 45:                        score += 0.10
        elif snap.rsi > 70:                        score -= 0.20
        elif snap.rsi > 55:                        score -= 0.10
        if snap.macd > snap.macd_sig:              score += 0.15
        elif snap.macd < snap.macd_sig:            score -= 0.15
        if snap.price > snap.ema20 > snap.ema50:   score += 0.15
        elif snap.price < snap.ema20 < snap.ema50: score -= 0.15
        if snap.price < snap.bb_lower:             score += 0.10
        elif snap.price > snap.bb_upper:           score -= 0.10
        return max(0.0, min(1.0, score)), snap

    def _with_retry(self, ticker: str) -> TASnapshot | None:
        delay = 1.0
        for attempt in range(self.cfg.YF_MAX_RETRIES):
            try:
                # Jitter prevents all threads hitting Yahoo at the same millisecond
                time.sleep(random.uniform(*self.cfg.YF_JITTER))
                return self._fetch_raw(ticker)
            except Exception as e:
                err = str(e).lower()
                is_rate_limit = any(x in err for x in ["429", "rate", "too many", "blocked"])
                if attempt < self.cfg.YF_MAX_RETRIES - 1:
                    wait = delay if is_rate_limit else delay * 0.5
                    log.warning(g(f"yfinance [{ticker}] retry {attempt+1} in {wait:.1f}s: {e}", C.YL))
                    time.sleep(wait)
                    delay *= self.cfg.YF_RETRY_BACKOFF
                else:
                    log.warning(f"TAScorer [{ticker}] gave up after {self.cfg.YF_MAX_RETRIES} tries")
        return None

    def _fetch_raw(self, ticker: str) -> TASnapshot | None:
        """
        Data provider. Replace this method with Polygon.io / Alpaca / IBKR
        to upgrade the data feed without touching anything else.
        """
        df = yf.Ticker(ticker).history(period="5d", interval="5m")
        if len(df) < 50:
            return None
        c = df["Close"].values.astype(float)
        h = df["High"].values.astype(float)
        l = df["Low"].values.astype(float)
        rsi             = float(talib.RSI(c, 14)[-1])
        macd, sig, _    = talib.MACD(c, 12, 26, 9)
        ema20           = float(talib.EMA(c, 20)[-1])
        ema50           = float(talib.EMA(c, 50)[-1])
        bb_up, _, bb_lo = talib.BBANDS(c, 20)
        atr             = float(talib.ATR(h, l, c, 14)[-1])
        return TASnapshot(
            ticker=ticker, price=round(float(c[-1]), 4),
            rsi=round(rsi, 2), macd=round(float(macd[-1]), 4),
            macd_sig=round(float(sig[-1]), 4), ema20=round(ema20, 4),
            ema50=round(ema50, 4), bb_upper=round(float(bb_up[-1]), 4),
            bb_lower=round(float(bb_lo[-1]), 4), atr=round(atr, 4),
        )


# ════════════════════════════════════════════════════════════════
# RISK MANAGER
# ════════════════════════════════════════════════════════════════
class RiskManager:

    def __init__(self, cfg: Config):
        self.cfg = cfg

    def calc_quantity(self, price: float, free_cash: float, open_count: int) -> float:
        if open_count >= self.cfg.MAX_OPEN_TRADES or price <= 0:
            return 0.0
        return round(max(self.cfg.MIN_SHARES,
                         (free_cash * self.cfg.MAX_POSITION_PCT) / price), 4)

    def stop_price(self, entry: float) -> float:
        return round(entry * (1 - self.cfg.STOP_LOSS_PCT), 4)

    def is_allowed(self, ticker, action, positions, free_cash, price):
        if action == "BUY":
            if any(p.get("ticker","").startswith(ticker[:4]) for p in positions):
                return False, f"Already holding {ticker}"
            if free_cash < price * self.cfg.MIN_SHARES:
                return False, "Insufficient free cash"
            if len(positions) >= self.cfg.MAX_OPEN_TRADES:
                return False, f"Max {self.cfg.MAX_OPEN_TRADES} positions"
        elif action == "SELL":
            if not any(p.get("ticker","").startswith(ticker[:4]) for p in positions):
                return False, f"No position in {ticker}"
        return True, "OK"


# ════════════════════════════════════════════════════════════════
# DECISION ENGINE
# ════════════════════════════════════════════════════════════════
class DecisionEngine:

    def __init__(self, cfg: Config, cache: NewsCache):
        self.cfg       = cfg
        self.ta        = TAScorer(cfg)
        self.sentiment = SentimentScorer(cfg, cache)
        self._prices   = defaultdict(lambda: deque(maxlen=10))

    def evaluate(self, ticker: str, free_cash: float, positions: list) -> TradeDecision:
        t212 = Config.TICKER_MAP.get(ticker, ticker + "_US_EQ")

        ta_score, snap = self.ta.score_ticker(ticker)
        price = snap.price if snap else 0.0

        sent = self.sentiment.score_ticker(ticker)
        sent_score = (sent.score + 1.0) / 2.0     # [-1,1] → [0,1]

        if price > 0:
            self._prices[ticker].append(price)
        hist = list(self._prices[ticker])
        mom  = 0.5
        if len(hist) >= 3:
            pct  = (hist[-1] - hist[0]) / hist[0]
            mom  = 0.5 + max(-0.5, min(0.5, pct * 10))

        combined = (self.cfg.WEIGHT_TA       * ta_score   +
                    self.cfg.WEIGHT_SENTIMENT * sent_score  +
                    self.cfg.WEIGHT_MOMENTUM  * mom)

        action = ("BUY"  if combined >= self.cfg.BUY_THRESHOLD  else
                  "SELL" if combined <= self.cfg.SELL_THRESHOLD else "HOLD")

        rm  = RiskManager(self.cfg)
        qty = 0.0
        if action == "BUY":
            qty = rm.calc_quantity(price, free_cash, len(positions))
        elif action == "SELL":
            held = [p for p in positions if p.get("ticker","").startswith(ticker[:4])]
            qty  = float(held[0].get("quantity", 0)) if held else 0.0

        return TradeDecision(
            ticker=ticker, t212_ticker=t212, action=action,
            combined_score=round(combined, 4), ta_score=round(ta_score, 4),
            sent_score=round(sent_score, 4), momentum=round(mom, 4),
            quantity=qty, price=price,
            reason=(f"TA={ta_score:.3f} SENT={sent_score:.3f}"
                    f"[{sent.method}]({sent.item_count}) "
                    f"MOM={mom:.3f} → {combined:.3f}"),
        )


# ════════════════════════════════════════════════════════════════
# T212 CLIENT
# ════════════════════════════════════════════════════════════════
class T212Client:
    URLS = {
        "demo": "https://demo.trading212.com/api/v0",
        "live": "https://live.trading212.com/api/v0",
    }

    def __init__(self, cfg: Config):
        if not cfg.T212_KEY:
            raise ValueError("T212_API_KEY not set in .env")
        self.base = self.URLS.get(cfg.T212_MODE, self.URLS["demo"])
        auth = (
            "Basic " + base64.b64encode(
                f"{cfg.T212_KEY}:{cfg.T212_SECRET}".encode()
            ).decode()
            if cfg.T212_SECRET else cfg.T212_KEY
        )
        self._h = {"Authorization": auth, "Content-Type": "application/json"}

    def _get(self, path):
        r = requests.get(self.base + path, headers=self._h, timeout=15)
        r.raise_for_status(); return r.json()

    def _post(self, path, body):
        r = requests.post(self.base + path, headers=self._h, json=body, timeout=15)
        r.raise_for_status(); return r.json()

    def get_cash(self):        return self._get("/equity/account/cash")
    def get_portfolio(self):
        d = self._get("/equity/portfolio")
        return d if isinstance(d, list) else d.get("items", [])
    def place_market_order(self, ticker, qty):
        return self._post("/equity/orders/market", {"ticker": ticker, "quantity": qty})
    def place_stop_order(self, ticker, qty, stop):
        return self._post("/equity/orders/stop", {
            "ticker": ticker, "quantity": -abs(qty),
            "stopPrice": stop, "timeValidity": "GOOD_TILL_CANCEL",
        })


# ════════════════════════════════════════════════════════════════
# TRADE EXECUTOR
# ════════════════════════════════════════════════════════════════
class TradeExecutor:

    def __init__(self, client: T212Client, cfg: Config):
        self.client     = client
        self.cfg        = cfg
        self.risk       = RiskManager(cfg)
        self._cooldowns = {}
        self._init_audit()

    def execute(self, dec: TradeDecision) -> str:
        if dec.action == "HOLD" or dec.quantity <= 0:
            self._audit(dec, "SKIPPED", "HOLD/qty=0"); return "SKIPPED"
        if time.time() - self._cooldowns.get(dec.ticker, 0) < self.cfg.COOLDOWN_SEC:
            self._audit(dec, "SKIPPED", "cooldown");   return "SKIPPED"
        try:
            cash      = self.client.get_cash()
            portfolio = self.client.get_portfolio()
            free      = float(cash.get("free", cash.get("freeForInvest", 0)))
        except Exception as e:
            self._audit(dec, "ERROR", str(e)); return "ERROR"

        ok, reason = self.risk.is_allowed(
            dec.ticker, dec.action, portfolio, free, dec.price)
        if not ok:
            self._audit(dec, "BLOCKED", reason)
            log.warning(g(f"  BLOCKED {dec.ticker}: {reason}", C.YL))
            return "BLOCKED"
        try:
            qty = dec.quantity if dec.action == "BUY" else -abs(dec.quantity)
            res = self.client.place_market_order(dec.t212_ticker, qty)
            if dec.action == "BUY" and dec.price > 0:
                try:
                    self.client.place_stop_order(
                        dec.t212_ticker, dec.quantity,
                        self.risk.stop_price(dec.price)
                    )
                except Exception as e:
                    log.warning(f"  Stop-loss failed: {e}")
            self._cooldowns[dec.ticker] = time.time()
            self._audit(dec, "EXECUTED", f"id={res.get('id','?')}")
            col = C.GR if dec.action == "BUY" else C.RD
            log.info(g(
                f"  ✅ {dec.action} {dec.quantity} × {dec.ticker} "
                f"@ ${dec.price:.2f}  score={dec.combined_score:.3f}", col))
            return "EXECUTED"
        except Exception as e:
            self._audit(dec, "ERROR", str(e))
            log.error(g(f"  ❌ [{dec.ticker}]: {e}", C.RD))
            return "ERROR"

    def _init_audit(self):
        p = Path(self.cfg.AUDIT_CSV)
        if not p.exists():
            with open(p, "w", newline="", encoding="utf-8") as f:
                csv.writer(f).writerow([
                    "timestamp","ticker","action","combined_score",
                    "ta_score","sent_score","momentum","quantity",
                    "price","status","detail","reason",
                ])

    def _audit(self, d: TradeDecision, status: str, detail: str):
        with open(self.cfg.AUDIT_CSV, "a", newline="", encoding="utf-8") as f:
            csv.writer(f).writerow([
                d.timestamp, d.ticker, d.action, d.combined_score,
                d.ta_score, d.sent_score, d.momentum, d.quantity,
                d.price, status, detail, d.reason,
            ])


# ════════════════════════════════════════════════════════════════
# MAIN ENGINE
# ════════════════════════════════════════════════════════════════
class AlphaEngine:

    def __init__(self, cfg: Config = None):
        self.cfg      = cfg or Config()
        self.client   = T212Client(self.cfg)
        self.cache    = NewsCache(self.cfg.NEWS_CSV)
        self.decision = DecisionEngine(self.cfg, self.cache)
        self.executor = TradeExecutor(self.client, self.cfg)
        self.cycle    = 0

    def _scan(self):
        self.cycle += 1
        try:
            cash_data = self.client.get_cash()
            free_cash = float(cash_data.get("free", cash_data.get("freeForInvest", 0)))
            portfolio = self.client.get_portfolio()
        except Exception as e:
            log.error(f"Account fetch failed: {e}"); return

        os.system("cls" if os.name == "nt" else "clear")
        now  = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
        gcol = C.GR if self.cfg.GEMINI_API_KEY else C.YL
        mcol = C.GR if self.cfg.T212_MODE == "live" else C.YL

        print(g("═" * 100, C.CY))
        print(
            g("  AlphaEngine v2.0", C.B+C.WH) +
            f"  [{g(self.cfg.T212_MODE.upper(), mcol)}]" +
            f"  Sentiment: {g('Gemini AI' if self.cfg.GEMINI_API_KEY else 'keywords', gcol)}" +
            f"  {g(now, C.DIM)}" + g(f"  #{self.cycle}", C.DIM)
        )
        print(g("═" * 100, C.CY))
        print(
            g(f"  {'TICKER':<10}", C.B+C.WH) +
            g(f"{'ACTION':<8}",   C.B+C.WH) +
            g(f"{'SCORE':>8}",    C.B+C.WH) +
            g(f"{'TA':>8}",       C.B+C.WH) +
            g(f"{'SENT':>8}",     C.B+C.WH) +
            g(f"{'METHOD':>10}",  C.B+C.WH) +
            g(f"{'MOM':>8}",      C.B+C.WH) +
            g(f"{'QTY':>8}",      C.B+C.WH) +
            g(f"{'PRICE':>10}",   C.B+C.WH) +
            g(f"{'STATUS':<12}",  C.B+C.WH)
        )
        print(g("─" * 100, C.DIM))

        results: dict[str, TradeDecision] = {}

        def _eval(ticker):
            results[ticker] = self.decision.evaluate(ticker, free_cash, portfolio)

        threads = [threading.Thread(target=_eval, args=(t,), daemon=True)
                   for t in Config.TICKER_MAP]
        for th in threads: th.start()
        for th in threads: th.join(timeout=30)

        for ticker, dec in results.items():
            acol = C.GR if dec.action=="BUY" else C.RD if dec.action=="SELL" else C.GY
            sent = self.decision.sentiment.score_ticker(ticker)
            mcol2 = C.GR if sent.method == "gemini" else C.YL
            status = "──"
            if dec.action in ("BUY","SELL"):
                status = self.executor.execute(dec)

            print(
                g(f"  {ticker:<10}", C.WH) +
                g(f"{dec.action:<8}", acol) +
                g(f"{dec.combined_score:>8.3f}", C.WH) +
                g(f"{dec.ta_score:>8.3f}", C.WH) +
                g(f"{dec.sent_score:>8.3f}", C.WH) +
                g(f"{sent.method:>10}", mcol2) +
                g(f"{dec.momentum:>8.3f}", C.WH) +
                g(f"{dec.quantity:>8.4f}", C.WH) +
                g(f"{dec.price:>10.2f}", C.WH) +
                g(f"  {status}", acol)
            )

        print(g("─" * 100, C.DIM))
        print(g(
            f"\n  Cash: ${free_cash:,.2f}  |  "
            f"Positions: {len(portfolio)}/{self.cfg.MAX_OPEN_TRADES}  |  "
            f"News cached: {len(self.cache._items):,}  |  "
            f"Audit: {self.cfg.AUDIT_CSV}",
            C.GY
        ))
        print(g(f"  Next scan in {self.cfg.SCAN_INTERVAL_SEC}s  |  Ctrl+C to stop\n", C.DIM))

    def start(self):
        log.info(g("AlphaEngine v2.0 starting…", C.B + C.CY))
        log.info(g(f"  Mode: {self.cfg.T212_MODE.upper()}  "
                   f"Sentiment: {'Gemini AI' if self.cfg.GEMINI_API_KEY else 'keywords'}  "
                   f"BUY≥{self.cfg.BUY_THRESHOLD}  SELL≤{self.cfg.SELL_THRESHOLD}", C.WH))
        try:
            while True:
                self._scan()
                time.sleep(self.cfg.SCAN_INTERVAL_SEC)
        except KeyboardInterrupt:
            print(g("\n  Stopped.\n", C.YL))


# ════════════════════════════════════════════════════════════════
# ENTRY POINT
# ════════════════════════════════════════════════════════════════
if __name__ == "__main__":
    AlphaEngine().start()