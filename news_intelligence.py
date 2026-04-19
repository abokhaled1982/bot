"""
NewsIntelligence — LLM-powered ticker extraction and velocity scoring.
Reads raw headlines from market_news_warehouse.csv, sends batches to Gemini
to extract tickers + sentiment, then ranks candidates by news velocity.
"""
from __future__ import annotations
import csv, json, logging, os, re, threading, time, sqlite3, hashlib, uuid
from collections import defaultdict, deque
from dataclasses import dataclass, field
from datetime import datetime, timezone, timedelta
from pathlib import Path
import requests
from dotenv import load_dotenv

load_dotenv()
log = logging.getLogger("AlphaEngine")

# ═══════════════════════════════════════════════════════════════
# DATA MODELS
# ═══════════════════════════════════════════════════════════════
@dataclass
class ExtractedSignal:
    ticker: str; sentiment: float; urgency: int
    headline: str; source: str; ts: str

@dataclass
class CandidateTicker:
    ticker: str
    mention_count: int = 0
    velocity_score: float = 0.0
    avg_sentiment: float = 0.0
    headlines: list = field(default_factory=list)
    first_seen: str = ""
    sources: set = field(default_factory=set)

# ═══════════════════════════════════════════════════════════════
# NEWS CACHE (reads CSV, tails for new rows)
# ═══════════════════════════════════════════════════════════════
FIELDS = ["source","category","title","link","published","fetched_at"]

class NewsCache:
    def __init__(self, csv_path: str, maxlen: int = 60_000):
        self.path = Path(csv_path)
        self._lock = threading.Lock()
        self._items = deque(maxlen=maxlen)
        self._file_pos = 0
        self._load()
        threading.Thread(target=self._tail, daemon=True, name="NewsTail").start()

    def _load(self):
        if not self.path.exists(): return
        try:
            with open(self.path, encoding="utf-8") as f:
                for row in csv.DictReader(f, fieldnames=FIELDS):
                    self._items.append(row)
                self._file_pos = f.tell()
            log.info(f"NewsCache: {len(self._items):,} articles loaded")
        except Exception as e:
            log.error(f"NewsCache load: {e}")

    def _tail(self):
        while True:
            try:
                if self.path.exists():
                    with open(self.path, encoding="utf-8") as f:
                        f.seek(self._file_pos)
                        rows = list(csv.DictReader(f, fieldnames=FIELDS))
                        if rows:
                            with self._lock:
                                for r in rows: self._items.append(r)
                        self._file_pos = f.tell()
            except: pass
            time.sleep(5)

    def get_recent_headlines(self, minutes: int) -> list[dict]:
        cutoff = datetime.now(timezone.utc) - timedelta(minutes=minutes)
        out = []
        with self._lock:
            for item in self._items:
                try:
                    raw = item.get("fetched_at","")
                    if not raw: continue
                    ts = datetime.fromisoformat(raw.replace("Z","+00:00"))
                    if ts >= cutoff:
                        out.append(item)
                except: pass
        return out

    def get_for_ticker(self, ticker: str, minutes: int) -> list[dict]:
        cutoff = datetime.now(timezone.utc) - timedelta(minutes=minutes)
        out = []
        with self._lock:
            for item in self._items:
                try:
                    raw = item.get("fetched_at","")
                    if not raw: continue
                    ts = datetime.fromisoformat(raw.replace("Z","+00:00"))
                    title = item.get("title","").upper()
                    if ts >= cutoff and ticker.upper() in title:
                        out.append(item)
                except: pass
        return out

    @property
    def total(self): return len(self._items)

# ═══════════════════════════════════════════════════════════════
# GEMINI LLM CLIENT (free-tier aware)
# ═══════════════════════════════════════════════════════════════
class GeminiClient:
    URL = "https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent?key={key}"

    def __init__(self, api_key: str, model: str = "gemini-2.0-flash"):
        self.key = api_key
        self.model = model
        self._last_call = 0.0
        self._call_count = 0
        self._minute_start = time.time()

    def _rate_limit(self):
        now = time.time()
        # Reset counter every 60s
        if now - self._minute_start > 60:
            self._call_count = 0
            self._minute_start = now
        # Free tier: max 10 calls/min to be safe
        if self._call_count >= 10:
            wait = 62 - (now - self._minute_start)
            if wait > 0:
                log.info(f"Rate limit: waiting {wait:.0f}s for Gemini cooldown")
                time.sleep(wait)
            self._call_count = 0
            self._minute_start = time.time()
        # Enforce minimum 6s between calls
        elapsed = now - self._last_call
        if elapsed < 6.0: time.sleep(6.0 - elapsed)

    def call(self, system: str, prompt: str, temp: float = 0.1) -> str | None:
        if not self.key: return None
        url = self.URL.format(model=self.model, key=self.key)
        body = {
            "system_instruction": {"parts": [{"text": system}]},
            "contents": [{"parts": [{"text": prompt}]}],
            "generationConfig": {"temperature": temp, "maxOutputTokens": 1024},
        }
        # Retry with backoff on 429
        for attempt in range(3):
            self._rate_limit()
            try:
                self._last_call = time.time()
                self._call_count += 1
                r = requests.post(url, json=body, timeout=20)
                if r.status_code == 429:
                    wait = 15 * (attempt + 1)
                    log.info(f"Gemini 429 — backing off {wait}s (attempt {attempt+1}/3)")
                    time.sleep(wait)
                    continue
                r.raise_for_status()
                text = r.json().get("candidates",[{}])[0].get("content",{}).get("parts",[{}])[0].get("text","")
                return text.strip().replace("```json","").replace("```","").strip()
            except requests.exceptions.HTTPError as e:
                if "429" in str(e):
                    time.sleep(15 * (attempt + 1))
                    continue
                log.warning(f"Gemini HTTP error: {e}")
                return None
            except Exception as e:
                log.warning(f"Gemini error: {e}")
                return None
        log.warning("Gemini: exhausted retries")
        return None

# ═══════════════════════════════════════════════════════════════
# NEWS INTELLIGENCE ENGINE
# ═══════════════════════════════════════════════════════════════
EXTRACT_PROMPT = """You are a financial news analyst. Extract ALL stock tickers mentioned or implied.
Rules:
- "Apple" → AAPL, "Google/Alphabet" → GOOGL, "Tesla" → TSLA etc.
- Include sector ETFs if a sector is discussed (e.g. "tech stocks fall" → QQQ, XLK)
- Include rate-sensitive tickers if Fed/rates discussed (e.g. TLT, XLF)
- ONLY US-listed tickers. Skip crypto, forex, commodities.
- For each ticker give sentiment (-1.0 to 1.0) and urgency (1-5, 5=breaking)

Return ONLY a JSON array: [{"t":"AAPL","s":0.7,"u":3},{"t":"TSLA","s":-0.3,"u":2}]
If no tickers found, return: []"""

CONVICTION_PROMPT = """You are a quantitative trading analyst. Given this data about a stock, rate your conviction to {action}.

Ticker: {ticker}
Recent Headlines ({headline_count}): {headlines}
Average News Sentiment: {sentiment:.2f}
News Velocity (mentions/hour): {velocity:.1f}
RSI: {rsi:.1f} | MACD Signal: {macd_signal} | Price vs EMA20: {price_vs_ema}
Current Price: ${price:.2f}

Return ONLY JSON: {{"conviction": <float 0.0 to 1.0>, "reasoning": "<one sentence>"}}"""

# Company name → ticker mapping for fallback extraction
COMPANY_MAP = {
    "apple":  "AAPL", "microsoft": "MSFT", "google":  "GOOGL", "alphabet": "GOOGL",
    "amazon": "AMZN", "meta":      "META", "facebook":"META",  "tesla":    "TSLA",
    "nvidia": "NVDA", "amd":       "AMD",  "intel":   "INTC",  "netflix":  "NFLX",
    "disney": "DIS",  "walmart":   "WMT",  "jpmorgan":"JPM",   "goldman":  "GS",
    "boeing": "BA",   "ford":      "F",    "uber":    "UBER",  "palantir": "PLTR",
    "salesforce":"CRM","oracle":   "ORCL", "adobe":   "ADBE",  "paypal":   "PYPL",
    "coinbase":"COIN", "robinhood":"HOOD", "snowflake":"SNOW", "crowdstrike":"CRWD",
    "spotify": "SPOT", "airbnb":   "ABNB", "block":   "SQ",    "square":   "SQ",
    "shopify": "SHOP", "rivian":   "RIVN", "lucid":   "LCID",  "sofi":     "SOFI",
    "exxon":   "XOM",  "chevron":  "CVX",  "pfizer":  "PFE",   "merck":    "MRK",
    "johnson": "JNJ",  "procter":  "PG",   "coca-cola":"KO",   "pepsi":    "PEP",
    "starbucks":"SBUX","mcdonald": "MCD",  "nike":    "NKE",   "costco":   "COST",
    "berkshire":"BRK.B","broadcom": "AVGO", "qualcomm":"QCOM",  "micron":   "MU",
    "visa":    "V",    "mastercard":"MA",   "american express":"AXP",
    "bank of america":"BAC", "wells fargo":"WFC", "morgan stanley":"MS",
    "general motors":"GM",  "general electric":"GE", "eli lilly":"LLY",
    "unitedhealth":"UNH",   "home depot":"HD",      "target":"TGT",
}

BULLISH_KW = {"beats","record","profit","surge","upgrade","buy","breakout","bullish","jumps","soars","rally","rises","gains"}
BEARISH_KW = {"misses","loss","falls","downgrade","sell","warning","bearish","lawsuit","crash","plunges","drops","slumps","sinks"}

class NewsIntelligence:
    def __init__(self, cache: NewsCache, db_path: str, gemini: GeminiClient):
        self.cache = cache
        self.db_path = db_path
        self.gemini = gemini
        self._seen_hashes: set = set()
        self._signal_buffer: list[ExtractedSignal] = []
        self._lock = threading.Lock()
        self._llm_available = True  # tracks if LLM is working

    def _hash_headline(self, title: str) -> str:
        return hashlib.md5(title.encode()).hexdigest()[:12]

    def _extract_fallback(self, headline: dict) -> list[ExtractedSignal]:
        """Keyword-based ticker extraction when LLM is unavailable."""
        title = headline.get("title", "")
        source = headline.get("source", "")
        ts = datetime.now(timezone.utc).isoformat()
        signals = []

        title_lower = title.lower()

        # 1. Extract from source column (e.g., GNews-stock-AAPL, Nasdaq-TSLA)
        for prefix in ["-stock-", "Nasdaq-", "Finviz-", "Bing-stock-", "MarketBeat-"]:
            if prefix in source:
                t = source.split(prefix)[-1].split("-")[0].upper()
                if t.isalpha() and 1 < len(t) <= 5:
                    sent = self._keyword_sentiment(title_lower)
                    signals.append(ExtractedSignal(t, sent, 2, title[:120], source, ts))
                    break

        # 2. Match company names in title
        for name, ticker in COMPANY_MAP.items():
            if name in title_lower:
                sent = self._keyword_sentiment(title_lower)
                signals.append(ExtractedSignal(ticker, sent, 3, title[:120], source, ts))

        # 3. Find raw tickers in title (e.g., "(AAPL)" or "TSLA stock")
        import re
        for match in re.findall(r'\b([A-Z]{2,5})\b', title):
            if match in {"THE","AND","FOR","NEW","CEO","IPO","GDP","CPI","ETF","USD","EUR","FED","SEC","NYSE","AI"}: continue
            # Only include if it looks like a real ticker (appears near financial context)
            if any(kw in title_lower for kw in ["stock","share","price","earnings","market","trade","buy","sell","analyst"]):
                sent = self._keyword_sentiment(title_lower)
                signals.append(ExtractedSignal(match, sent, 2, title[:120], source, ts))

        return signals

    def _keyword_sentiment(self, title_lower: str) -> float:
        bull = sum(1 for kw in BULLISH_KW if kw in title_lower)
        bear = sum(1 for kw in BEARISH_KW if kw in title_lower)
        raw = (bull - bear) / max(1, bull + bear)
        return max(-1.0, min(1.0, raw))

    def discover(self, lookback_min: int = 120, batch_size: int = 30) -> list[CandidateTicker]:
        headlines = self.cache.get_recent_headlines(lookback_min)
        if not headlines:
            log.info("NewsIntelligence: No recent headlines")
            return []

        # Filter unseen headlines
        unseen = []
        for h in headlines:
            title = h.get("title","")
            if not title: continue
            hsh = self._hash_headline(title)
            if hsh not in self._seen_hashes:
                unseen.append(h)
                self._seen_hashes.add(hsh)

        if len(self._seen_hashes) > 100_000:
            self._seen_hashes = set(list(self._seen_hashes)[-50_000:])

        signals = []
        batch_id = uuid.uuid4().hex[:8]

        # Try LLM first
        llm_worked = False
        if self._llm_available:
            max_batches = 3
            batches_done = 0
            for i in range(0, min(len(unseen), 90), batch_size):
                if batches_done >= max_batches: break
                batch = unseen[i:i+batch_size]
                titles_text = "\n".join(f"- {h.get('title','')}" for h in batch)
                raw = self.gemini.call(EXTRACT_PROMPT, f"Headlines:\n{titles_text}")
                batches_done += 1
                if not raw:
                    self._llm_available = False
                    log.info("LLM unavailable — switching to keyword fallback")
                    break
                llm_worked = True
                try:
                    items = json.loads(raw)
                    if not isinstance(items, list): continue
                    for idx, item in enumerate(items):
                        t = item.get("t","").upper().strip()
                        if not t or not t.isalpha() or len(t) > 5: continue
                        hl_idx = min(idx, len(batch)-1)
                        sig = ExtractedSignal(
                            ticker=t, sentiment=max(-1,min(1,float(item.get("s",0)))),
                            urgency=max(1,min(5,int(item.get("u",1)))),
                            headline=batch[hl_idx].get("title","")[:120],
                            source=batch[hl_idx].get("source",""),
                            ts=datetime.now(timezone.utc).isoformat())
                        signals.append(sig)
                except (json.JSONDecodeError, ValueError):
                    continue

        # Fallback: keyword-based extraction
        if not llm_worked:
            log.info(f"Using keyword fallback on {len(unseen)} headlines")
            for h in unseen[:200]:
                signals.extend(self._extract_fallback(h))
            # Retry LLM next cycle
            self._llm_available = True

        # Save signals to DB
        self._save_signals(signals, batch_id)

        # Build candidate map from ALL recent signals (not just this batch)
        return self._rank_candidates(lookback_min)

    def _save_signals(self, signals: list[ExtractedSignal], batch_id: str):
        if not signals: return
        try:
            with sqlite3.connect(self.db_path) as conn:
                conn.executemany(
                    "INSERT INTO news_signals (ticker,sentiment,urgency,headline,source,extracted_at,batch_id) VALUES (?,?,?,?,?,?,?)",
                    [(s.ticker, s.sentiment, s.urgency, s.headline, s.source, s.ts, batch_id) for s in signals]
                )
                conn.commit()
            log.info(f"Saved {len(signals)} signals (batch {batch_id})")
        except Exception as e:
            log.error(f"Signal save error: {e}")

    def _rank_candidates(self, lookback_min: int) -> list[CandidateTicker]:
        cutoff = (datetime.now(timezone.utc) - timedelta(minutes=lookback_min)).isoformat()
        try:
            with sqlite3.connect(self.db_path) as conn:
                rows = conn.execute(
                    "SELECT ticker, sentiment, urgency, headline, source, extracted_at "
                    "FROM news_signals WHERE extracted_at >= ? ORDER BY extracted_at DESC",
                    (cutoff,)
                ).fetchall()
        except: return []

        ticker_data = defaultdict(lambda: {"sents":[], "urgencies":[], "headlines":[], "sources":set(), "first":""})
        for ticker, sent, urg, hl, src, ts in rows:
            d = ticker_data[ticker]
            d["sents"].append(sent)
            d["urgencies"].append(urg)
            d["headlines"].append(hl)
            d["sources"].add(src)
            if not d["first"] or ts < d["first"]: d["first"] = ts

        # Also count from raw CSV for tickers we know about
        candidates = []
        for ticker, d in ticker_data.items():
            count = len(d["sents"])
            # Velocity: weighted by urgency, normalized by time window
            raw_vel = sum(u * 0.5 for u in d["urgencies"])
            velocity = min(1.0, raw_vel / max(1, lookback_min / 30))
            avg_sent = sum(d["sents"]) / max(1, len(d["sents"]))

            # Also check raw CSV mentions
            csv_mentions = self.cache.get_for_ticker(ticker, lookback_min)
            total_mentions = count + len(csv_mentions)

            candidates.append(CandidateTicker(
                ticker=ticker, mention_count=total_mentions,
                velocity_score=round(velocity, 4), avg_sentiment=round(avg_sent, 4),
                headlines=d["headlines"][:10], first_seen=d["first"],
                sources=d["sources"]
            ))

        # Sort by velocity * abs(sentiment) — strongest signals first
        candidates.sort(key=lambda c: c.velocity_score * abs(c.avg_sentiment) * c.mention_count, reverse=True)
        return candidates[:25]  # Cap at 25 to respect yFinance limits

    def get_conviction(self, ticker: str, action: str, headlines: list,
                       sentiment: float, velocity: float, rsi: float,
                       macd_signal: str, price_vs_ema: str, price: float) -> tuple[float, str]:
        raw = self.gemini.call(
            "You are a quantitative trading analyst. Return ONLY valid JSON.",
            CONVICTION_PROMPT.format(
                action=action, ticker=ticker, headline_count=len(headlines),
                headlines=" | ".join(headlines[:5]), sentiment=sentiment,
                velocity=velocity, rsi=rsi, macd_signal=macd_signal,
                price_vs_ema=price_vs_ema, price=price
            )
        )
        if not raw: return 0.5, "LLM unavailable"
        try:
            data = json.loads(raw)
            return max(0, min(1, float(data.get("conviction", 0.5)))), data.get("reasoning", "")
        except:
            return 0.5, "Parse error"
