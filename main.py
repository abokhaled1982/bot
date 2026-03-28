"""
MarketPulse News Engine v3.0
━━━━━━━━━━━━━━━━━━━━━━━━━━━
Architecture  : Producer → work_queue → Workers → result_queue → Drain
Anti-block    : Circuit breaker (persisted), ETag/Last-Modified cache (persisted),
                per-domain concurrency cap (semaphore), full UA+header profiles,
                request jitter, Retry-After respect, instant-trip on 403/401/451
Pipeline      : Workers start fetching the MOMENT first URL is queued
Storage       : Incremental CSV writes, dedup by link
Runs 24/7     : Auto-restart cycle with random wait between runs
"""

import feedparser
import csv
import json
import time
import random
import os
import logging
import threading
from queue import Queue
from datetime import datetime, timezone
from dataclasses import dataclass, asdict
from collections import defaultdict
from urllib.parse import urlparse
from DrissionPage import SessionPage


# ══════════════════════════════════════════════════════════════════════
# LOGGING
# ══════════════════════════════════════════════════════════════════════
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[
        logging.StreamHandler(),
        logging.FileHandler("engine.log", encoding="utf-8"),
    ],
)
log = logging.getLogger("MarketPulse")


# ══════════════════════════════════════════════════════════════════════
# MODULE 1 – DATA MODEL
# ══════════════════════════════════════════════════════════════════════
@dataclass
class NewsItem:
    source:     str
    news_type:  str   # Crypto | Stock | Finance | Macro | Sentiment
    title:      str
    link:       str
    date:       str
    fetched_at: str = ""

    def __post_init__(self):
        if not self.fetched_at:
            self.fetched_at = datetime.now(timezone.utc).isoformat()


# ══════════════════════════════════════════════════════════════════════
# MODULE 2 – SOURCE MANAGER
# ══════════════════════════════════════════════════════════════════════
class SourceManager:
    """Generates RSS URLs at runtime across stocks, crypto, macro and sentiment."""

    STATIC_FEEDS = {
        # Finance / Business
        "Reuters Business":    ("https://feeds.reuters.com/reuters/businessNews",                                                               "Finance"),
        "Reuters Tech":        ("https://feeds.reuters.com/reuters/technologyNews",                                                             "Finance"),
        "AP Business":         ("https://feeds.apnews.com/ApNewsBusinessFeed",                                                                  "Finance"),
        "CNBC Top News":       ("https://www.cnbc.com/id/100003114/device/rss/rss.html",                                                        "Finance"),
        "CNBC Finance":        ("https://www.cnbc.com/id/10000664/device/rss/rss.html",                                                         "Finance"),
        "MarketWatch":         ("https://www.marketwatch.com/rss/topstories",                                                                   "Stock"),
        "WSJ Markets":         ("https://feeds.a.dj.com/rss/RSSMarketsMain.xml",                                                               "Stock"),
        "WSJ Business":        ("https://feeds.a.dj.com/rss/WSJcomUSBusiness.xml",                                                             "Finance"),
        "Fortune":             ("https://fortune.com/feed/",                                                                                    "Finance"),
        "Seeking Alpha":       ("https://seekingalpha.com/market_currents.xml",                                                                 "Stock"),
        "Motley Fool":         ("https://www.fool.com/feeds/index.aspx",                                                                        "Stock"),
        "Investopedia":        ("https://www.investopedia.com/feedbuilder/feed/getfeed?feedName=rss_headline",                                  "Finance"),
        "Zacks":               ("https://www.zacks.com/rss.xml",                                                                                "Stock"),
        "TheStreet":           ("https://www.thestreet.com/.rss/full",                                                                          "Stock"),
        "Benzinga":            ("https://www.benzinga.com/feed",                                                                                "Stock"),
        "Nasdaq News":         ("https://www.nasdaq.com/feed/rssoutbound?category=Markets",                                                     "Stock"),
        "Yahoo Finance":       ("https://finance.yahoo.com/news/rssindex",                                                                      "Finance"),
        "FT Markets":          ("https://www.ft.com/markets?format=rss",                                                                        "Finance"),
        "Bloomberg Markets":   ("https://feeds.bloomberg.com/markets/news.rss",                                                                 "Finance"),
        "Barrons":             ("https://www.barrons.com/feed",                                                                                 "Stock"),
        # Crypto tier-1
        "CoinDesk":            ("https://www.coindesk.com/arc/outboundfeeds/rss/",                                                              "Crypto"),
        "Cointelegraph":       ("https://cointelegraph.com/rss",                                                                                "Crypto"),
        "The Block":           ("https://www.theblock.co/rss.xml",                                                                              "Crypto"),
        "Decrypt":             ("https://decrypt.co/feed",                                                                                      "Crypto"),
        "CryptoSlate":         ("https://cryptoslate.com/feed/",                                                                                "Crypto"),
        "BeInCrypto":          ("https://beincrypto.com/feed/",                                                                                 "Crypto"),
        "Bitcoin Magazine":    ("https://bitcoinmagazine.com/.rss/full/",                                                                       "Crypto"),
        "CryptoNews":          ("https://cryptonews.com/news/feed/",                                                                            "Crypto"),
        "AMBCrypto":           ("https://ambcrypto.com/feed/",                                                                                  "Crypto"),
        "NewsBTC":             ("https://www.newsbtc.com/feed/",                                                                                "Crypto"),
        "Messari":             ("https://messari.io/rss",                                                                                       "Crypto"),
        "The Defiant":         ("https://thedefiant.io/feed",                                                                                   "Crypto"),
        "U.Today Crypto":      ("https://u.today/rss",                                                                                          "Crypto"),
        "Crypto Briefing":     ("https://cryptobriefing.com/feed/",                                                                             "Crypto"),
        "DeFi Llama News":     ("https://defillama.com/news/rss",                                                                               "Crypto"),
        # Macro / Central Banks
        "FedReserve Press":    ("https://www.federalreserve.gov/feeds/press_all.xml",                                                           "Macro"),
        "FedReserve Speeches": ("https://www.federalreserve.gov/feeds/speeches.xml",                                                            "Macro"),
        "ECB News":            ("https://www.ecb.europa.eu/rss/press.html",                                                                     "Macro"),
        "BIS Research":        ("https://www.bis.org/rss/bis_research.htm",                                                                     "Macro"),
        "IMF News":            ("https://www.imf.org/en/News/rss",                                                                              "Macro"),
        "World Bank":          ("https://feeds.worldbank.org/worldbank/news",                                                                   "Macro"),
        "SEC Filings 8-K":     ("https://www.sec.gov/cgi-bin/browse-edgar?action=getcurrent&type=8-K&dateb=&owner=include&count=40&output=atom","Macro"),
        "CFTC News":           ("https://www.cftc.gov/rss/pressreleases.xml",                                                                   "Macro"),
        # Sentiment / Social
        "Reddit WSB":          ("https://www.reddit.com/r/wallstreetbets/.rss",                                                                 "Sentiment"),
        "Reddit Stocks":       ("https://www.reddit.com/r/stocks/.rss",                                                                         "Sentiment"),
        "Reddit Crypto":       ("https://www.reddit.com/r/CryptoCurrency/.rss",                                                                 "Sentiment"),
        "Reddit Investing":    ("https://www.reddit.com/r/investing/.rss",                                                                      "Sentiment"),
        "Reddit Bitcoin":      ("https://www.reddit.com/r/Bitcoin/.rss",                                                                        "Sentiment"),
        "Reddit ETH":          ("https://www.reddit.com/r/ethereum/.rss",                                                                       "Sentiment"),
        # YouTube
        "Bloomberg YT":        ("https://www.youtube.com/feeds/videos.xml?user=Bloomberg",                                                      "Finance"),
        "CNBC YT":             ("https://www.youtube.com/feeds/videos.xml?user=cnbc",                                                           "Finance"),
        "Reuters YT":          ("https://www.youtube.com/feeds/videos.xml?user=Reuters",                                                        "Finance"),
    }

    SP500 = [
        "MMM","AOS","ABT","ABBV","ACN","ADBE","AMD","AES","AFL","A","APD","ABNB","AKAM",
        "ALB","ARE","ALGN","ALLE","LNT","ALL","GOOGL","GOOG","MO","AMZN","AMCR","AEE",
        "AAL","AEP","AXP","AIG","AMT","AWK","AMP","AME","AMGN","APH","ADI","ANSS","AON",
        "APA","AAPL","AMAT","APTV","ACGL","ADM","ANET","AJG","AIZ","T","ATO","ADSK","AZO",
        "AVB","AVY","AXON","BKR","BALL","BAC","BK","BBWI","BAX","BDX","BRK.B","BBY","BIO",
        "TECH","BIIB","BLK","BX","BA","BSX","BMY","AVGO","BR","BRO","BF.B","BLDR","BG",
        "CDNS","CZR","CPT","CPB","COF","CAH","KMX","CCL","CARR","CAT","CBOE","CBRE","CDW",
        "CE","CNC","CDAY","CF","CRL","SCHW","CHTR","CVX","CMG","CB","CHD","CI","CINF","CTAS",
        "CSCO","C","CFG","CLX","CME","CMS","KO","CTSH","CL","CMCSA","CMA","CAG","COP","ED",
        "STZ","CEG","COO","CPRT","GLW","CTVA","CSGP","COST","CTRA","CCI","CSX","CMI","CVS",
        "DHR","DRI","DVA","DAY","DE","DAL","XRAY","DVN","DXCM","FANG","DLR","DFS","DG",
        "DLTR","D","DPZ","DOV","DOW","DHI","DTE","DUK","DD","DOC","EMN","ETN","EBAY","ECL",
        "EIX","EW","EA","ELV","LLY","EMR","ENPH","ETR","EOG","EPAM","EQT","EFX","EQIX",
        "EQR","ESS","EL","ETSY","EG","ES","EXC","EXPE","EXPD","EXR","XOM","FFIV","FDS",
        "FICO","FAST","FRT","FDX","FIS","FITB","FSLR","FE","FI","FMC","F","FTNT","FTV",
        "FOXA","FOX","BEN","FCX","GRMN","IT","GE","GEHC","GEN","GNRC","GD","GIS","GM","GPC",
        "GILD","GPN","GL","GS","HAL","HIG","HAS","HCA","HSIC","HSY","HES","HPE","HLT","HOLX",
        "HD","HON","HRL","HST","HWM","HPQ","HUBB","HUM","HBAN","HII","IBM","IEX","IDXX","ITW",
        "INCY","IR","PODD","INTC","ICE","IFF","IP","IPG","INTU","ISRG","IVZ","INVH","IQV",
        "IRM","JKHY","J","JBL","JNPR","JCI","JPM","K","KVUE","KDP","KEY","KEYS","KMB","KIM",
        "KMI","KLAC","KHC","KR","LHX","LH","LRCX","LW","LVS","LDOS","LEN","LNC","LIN","LYV",
        "LKQ","LMT","L","LOW","LYB","MTB","MRO","MPC","MKTX","MAR","MMC","MLM","MAS","MA",
        "MTCH","MKC","MCD","MCK","MDT","MRK","META","MET","MTD","MGM","MCHP","MU","MSFT",
        "MAA","MRNA","MHK","MOH","TAP","MDLZ","MPWR","MNST","MCO","MS","MOS","MSI","MSCI",
        "NDAQ","NTAP","NFLX","NWL","NEM","NWSA","NWS","NEE","NKE","NI","NDSN","NSC","NTRS",
        "NOC","NCLH","NRG","NUE","NVDA","NVR","NXPI","ORLY","OXY","ODFL","OMC","ON","OKE",
        "ORCL","OTIS","PCAR","PKG","PANW","PH","PAYX","PAYC","PYPL","PNR","PEP","PFE","PCG",
        "PM","PSX","PNW","PNC","POOL","PPG","PPL","PFG","PG","PGR","PRU","PEG","PSA","PHM",
        "QRVO","PWR","QCOM","DGX","RL","RJF","RTX","O","REG","REGN","RF","RSG","RMD","RVTY",
        "ROK","ROL","ROP","ROST","RCL","SPGI","CRM","SBAC","SLB","STX","SRE","NOW","SHW",
        "SBUX","STT","STLD","STE","SYK","SWK","SNPS","SO","LUV","SJM","SPG","SWKS","SNA",
        "SOLV","SYY","TMUS","TROW","TTWO","TPR","TGT","TEL","TDY","TFX","TER","TSLA","TXN",
        "TXT","TMO","TJX","TSCO","TT","TDG","TRV","TRMB","TFC","TYL","ULTA","UDR","USB",
        "UNP","UAL","UPS","URI","UNH","UHS","VLO","VTR","VLTO","VRSN","VRSK","VZ","VRTX",
        "VFC","VTRS","VICI","V","VST","VMC","WRB","GWW","WAB","WBA","WMT","DIS","WBD","WM",
        "WAT","WEC","WFC","WELL","WST","WDC","WY","WHR","WMB","WTW","WYNN","XEL","XYL",
        "YUM","ZBRA","ZBH","ZTS",
        # High-cap growth / extended universe
        "PLTR","UBER","DASH","RBLX","NET","SNOW","DDOG","MDB","ZM","CRWD","OKTA","GTLB",
        "HOOD","COIN","MSTR","SOFI","UPST","AFRM","DKNG","RIVN","LCID","XPEV","LI","NIO",
        "BIDU","JD","PDD","BABA","SE","GRAB","LYFT","SNAP","PINS","RDDT","BMBL","ARM",
        "SMCI","DELL","PSTG","IONQ","RGTI","QUBT","QBTS",
    ]

    CRYPTO = [
        "BTC","ETH","BNB","XRP","SOL","ADA","AVAX","DOT","ATOM","NEAR","APT","SUI","SEI",
        "TRX","LTC","BCH","XMR","XLM","ALGO","VET","ETC","FIL","ICP","HBAR","ZEC","DASH",
        "USDT","USDC","USDS","DAI","PYUSD","USDE","SUSDE","SUSDS","FRAX","TUSD",
        "STETH","WSTETH","WBETH","AWETH","WEETH","CBBTC","WBTC","ETHFI","RENZO","PUFFER",
        "SWELL","KELP","MELLOW","BEDROCK","EIGENLAYER","SYMBIOTIC","KARAK",
        "LINK","UNI","AAVE","MKR","CRV","SNX","COMP","BAL","YFI","1INCH","SUSHI","DYDX",
        "GMX","GNS","KWENTA","SYNTHETIX","LYRA","OPYN","RIBBON","LQTY","FLUID","PENDLE",
        "ENA","RPL","LRC","GRT","BNT","KNC","ZRX","BAND","NMR","UMA","BADGER","PERP",
        "HEGIC","OCEAN","FET","AGIX","WLD","JUP","PYTH","BOME","MEW","POPCAT",
        "DOGE","SHIB","PEPE","FLOKI","BONK","WIF","MOG","BRETT","MAGA","MYRO","SLERF",
        "ARB","OP","POL","METIS","BOBA","HOP","ZETA","BLAST","MODE","SCROLL","LINEA",
        "STRK","MANTA","PORTAL","DYM","ZRO","IO","MERLIN","BOB",
        "MOVE","BERA","STORY","MONAD","INITIA","BABYLON","NUCLEUS","BOUNCE","MAGIC",
        "HYPE","INJ","TIA","TAO","RON","RENDER","W","JTO","ALT","PIXEL","SHRAP","YGG",
        "SUPER","SAGA","TNSR","OMNI","REZ","TON","NOT","DOGS","CATI","MAJOR",
        "STORJ","ANKR","NKN","IOTX","DENT","TRAC","DATA","SENT","AMB","IOTA","THETA",
        "TFUEL","CHR","CELR","SYS","ZIL","POWR","WPR","EWT","XNO","LAMB","RUFF",
        "CRO","KCS","OKB","GT","MX","BGB","WOO","NEXO","LEO",
        "SAND","MANA","AXS","ENJ","CHZ","GALA","IMX","FAN","FLOW","GFAL","REVV",
        "TOWER","PYR","NFTY","WHALE","ALPHA","PAXG","XAUT",
        "USDY","OUSG","OMMF","BUIDL","BENJI","TBILL","USTB","ONDO","MPL","CFG","RWA",
        "POLYX","DIMO","CELO","MASA",
        "RAY","ORCA","FIDA","KIN","SAMO","BONFIDA","GRAPE","LARIX","SBR","PORT","MNGO",
        "SLIM","TULIP","SUNNY","STEP","MAPS","SRM",
        "LUNC","LUNA","CEL","FTT","VBTC","MBTC","NBTC","FBTC","DBTC","CBTC","ABTC",
        "TBTC","SBTC","RENBTC","HBTC","BTCB","JUNO","EVMOS","KAVA","MIOTA",
    ]

    GOOGLE_TOPICS = [
        ("CAAqJggKIiBDQkFTRWdvSUwyMHZNRGx6TVdZU0FtVnVHZ0pWVXlnQVAB", "Stock",   "GNews-Markets"),
        ("CAAqJggKIiBDQkFTRWdvSUwyMHZNRGwwTlRFU0FtVnVHZ0pWVXlnQVAB", "Crypto",  "GNews-Crypto"),
        ("CAAqJggKIiBDQkFTRWdvSUwyMHZNRFp1ZEdvU0FtVnVHZ0pWVXlnQVAB", "Finance", "GNews-Finance"),
        ("CAAqBwgKMLmH0QowyPbpAg",                                     "Finance", "GNews-Economy"),
        ("CAAqBwgKML_H0QowsfbpAg",                                     "Stock",   "GNews-Stocks"),
    ]

    MACRO_TERMS = [
        "Federal Reserve interest rate", "ECB monetary policy", "inflation CPI",
        "GDP growth", "unemployment rate", "treasury yield", "bond market",
        "oil price OPEC", "gold silver commodities", "forex USD EUR",
        "IPO 2026", "earnings report", "merger acquisition", "short squeeze",
        "hedge fund", "private equity", "venture capital", "fintech",
        "recession outlook", "central bank", "quantitative easing",
    ]

    def get_all_feeds(self) -> dict:
        master = {}
        master.update(self.STATIC_FEEDS)

        tickers = list(dict.fromkeys(self.SP500))
        coins   = list(dict.fromkeys(self.CRYPTO))

        for t in tickers:
            s = t.replace(".", "-")
            master[f"GNews-stock-{s}"]  = (f"https://news.google.com/rss/search?q={s}+stock+NYSE+NASDAQ&hl=en-US&gl=US&ceid=US:en", "Stock")
            master[f"Finviz-{s}"]       = (f"https://finviz.com/rss.ashx?t={s}",                                                    "Stock")
            master[f"Bing-stock-{s}"]   = (f"https://www.bing.com/news/search?q={s}+stock&format=rss",                              "Stock")
            master[f"Nasdaq-{s}"]       = (f"https://www.nasdaq.com/feed/rssoutbound?symbol={s}",                                   "Stock")
            master[f"MarketBeat-{s}"]   = (f"https://www.marketbeat.com/stock-ideas/rss/?symbol={s}",                              "Stock")

        for c in coins:
            cl = c.lower()
            master[f"GNews-crypto-{c}"] = (f"https://news.google.com/rss/search?q={c}+cryptocurrency&hl=en-US&gl=US&ceid=US:en",   "Crypto")
            master[f"Bing-crypto-{c}"]  = (f"https://www.bing.com/news/search?q={c}+crypto&format=rss",                            "Crypto")
            master[f"CTel-{c}"]         = (f"https://cointelegraph.com/rss/tag/{cl}",                                              "Crypto")
            master[f"CSlate-{c}"]       = (f"https://cryptoslate.com/feed/?s={cl}",                                                "Crypto")

        for topic_id, cat, label in self.GOOGLE_TOPICS:
            master[label] = (f"https://news.google.com/rss/topics/{topic_id}?hl=en-US&gl=US&ceid=US:en", cat)

        for term in self.MACRO_TERMS:
            slug  = term.replace(" ", "+")
            label = "Macro-" + term.replace(" ", "_")[:28]
            master[label] = (f"https://news.google.com/rss/search?q={slug}&hl=en-US&gl=US&ceid=US:en", "Macro")

        log.info(f"SourceManager: {len(master):,} feeds generated.")
        return master


# ══════════════════════════════════════════════════════════════════════
# MODULE 3 – CIRCUIT BREAKER  (persisted to disk)
# ══════════════════════════════════════════════════════════════════════
class CircuitBreaker:
    """
    Per-domain failure tracker. After `threshold` failures the domain
    enters a cooldown for `cooldown` seconds. State is persisted to disk
    so bans survive script restarts. 403/401/451 instantly trip the breaker.
    """
    STATE_FILE = "circuit_state.json"
    PERM_BLOCK = {403, 401, 451}

    def __init__(self, threshold: int = 3, cooldown: int = 1800):
        self._threshold = threshold
        self._cooldown  = cooldown
        self._failures: dict = defaultdict(int)
        self._tripped:  dict = {}   # domain → monotonic timestamp of trip
        self._lock = threading.Lock()
        self._load()

    def _load(self):
        if os.path.exists(self.STATE_FILE):
            try:
                with open(self.STATE_FILE) as f:
                    data = json.load(f)
                self._tripped  = data.get("tripped", {})
                self._failures = defaultdict(int, data.get("failures", {}))
                log.info(f"CircuitBreaker: {len(self._tripped)} domain(s) in cooldown")
            except Exception as e:
                log.warning(f"CircuitBreaker load error: {e}")

    def _save(self):
        try:
            with open(self.STATE_FILE, "w") as f:
                json.dump(
                    {"tripped": self._tripped, "failures": dict(self._failures)},
                    f, indent=2
                )
        except Exception as e:
            log.warning(f"CircuitBreaker save error: {e}")

    def is_open(self, domain: str) -> bool:
        """True = skip this domain entirely."""
        with self._lock:
            if domain in self._tripped:
                if time.monotonic() - float(self._tripped[domain]) < self._cooldown:
                    return True
                # cooldown expired → reset
                del self._tripped[domain]
                self._failures[domain] = 0
                self._save()
            return False

    def record_failure(self, domain: str, code: int):
        with self._lock:
            # Permanent-block codes count as `threshold` failures at once
            inc = self._threshold if code in self.PERM_BLOCK else 1
            self._failures[domain] += inc
            if self._failures[domain] >= self._threshold:
                self._tripped[domain] = time.monotonic()
                label = "PERMANENT BLOCK" if code in self.PERM_BLOCK else "30-min cooldown"
                log.warning(f"  ⚡ Circuit tripped [{domain}] HTTP {code} — {label}")
                self._save()

    def record_success(self, domain: str):
        with self._lock:
            if self._failures.get(domain, 0) > 0:
                self._failures[domain] = 0
                self._save()

    def stats(self) -> str:
        with self._lock:
            return f"tripped={len(self._tripped)}, tracked={len(self._failures)}"


# ══════════════════════════════════════════════════════════════════════
# MODULE 4 – CONDITIONAL-GET CACHE  (persisted to disk)
# ══════════════════════════════════════════════════════════════════════
class ConditionalGetCache:
    """
    Stores ETag + Last-Modified per URL. On repeat visits we send these
    headers back — server returns 304 Not Modified if nothing changed.
    Result: ~60-80% less bandwidth, and we look like a real RSS reader.
    Flushed to disk every 50 updates and at end of each cycle.
    """
    CACHE_FILE = "etag_cache.json"

    def __init__(self):
        self._cache: dict = {}
        self._lock  = threading.Lock()
        self._dirty = 0
        self._load()

    def _load(self):
        if os.path.exists(self.CACHE_FILE):
            try:
                with open(self.CACHE_FILE) as f:
                    self._cache = json.load(f)
                log.info(f"ETagCache: {len(self._cache):,} entries loaded")
            except Exception as e:
                log.warning(f"ETagCache load error: {e}")

    def _save(self):
        try:
            with open(self.CACHE_FILE, "w") as f:
                json.dump(self._cache, f)
        except Exception as e:
            log.warning(f"ETagCache save error: {e}")

    def get_headers(self, url: str) -> dict:
        entry   = self._cache.get(url, {})
        headers = {}
        if "etag" in entry:
            headers["If-None-Match"]     = entry["etag"]
        if "last_modified" in entry:
            headers["If-Modified-Since"] = entry["last_modified"]
        return headers

    def update(self, url: str, resp_headers: dict):
        entry = {}
        if "ETag" in resp_headers:
            entry["etag"]          = resp_headers["ETag"]
        if "Last-Modified" in resp_headers:
            entry["last_modified"] = resp_headers["Last-Modified"]
        if not entry:
            return
        with self._lock:
            self._cache[url] = entry
            self._dirty     += 1
            if self._dirty >= 50:
                self._save()
                self._dirty = 0

    def flush(self):
        with self._lock:
            if self._dirty:
                self._save()
                self._dirty = 0


# ══════════════════════════════════════════════════════════════════════
# MODULE 5 – DOMAIN RATE LIMITER
# ══════════════════════════════════════════════════════════════════════
class DomainLimiter:
    """Max `rate` requests per `window` seconds per domain. Thread-safe."""

    def __init__(self, rate: float = 1.0, window: float = 2.0):
        self._rate   = rate
        self._window = window
        self._counts: dict = defaultdict(list)
        self._lock   = threading.Lock()

    @staticmethod
    def _domain(url: str) -> str:
        try:
            return urlparse(url).netloc
        except Exception:
            return url

    def wait_if_needed(self, url: str):
        domain = self._domain(url)
        with self._lock:
            now = time.monotonic()
            self._counts[domain] = [t for t in self._counts[domain] if now - t < self._window]
            if len(self._counts[domain]) >= self._rate:
                sleep_for = self._window - (now - self._counts[domain][0])
                if sleep_for > 0:
                    time.sleep(sleep_for)
            self._counts[domain].append(time.monotonic())


# ══════════════════════════════════════════════════════════════════════
# MODULE 6 – WORKER POOL  (thread-safe, block-resistant)
# ══════════════════════════════════════════════════════════════════════
class WorkerPool:
    """
    One DrissionPage SessionPage per thread (thread-local storage).

    Anti-block stack applied on every request:
      1. Circuit breaker check  — skip domain if it's in cooldown
      2. Per-domain semaphore   — max 3 concurrent connections per host
      3. Domain rate limiter    — max 1 req / 2s per host
      4. Full UA+header profile — rotate realistic browser fingerprints
      5. Conditional GET        — ETag / If-Modified-Since headers
      6. 304 handling           — return early, no parsing needed
      7. 429 handling           — respect Retry-After, exponential backoff
      8. 403/401/451 handling   — instant circuit trip, no retries wasted
      9. Jitter on backoff      — unpredictable timing, not bot-regular
    """

    UA_PROFILES = [
        {
            "User-Agent":      "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/123.0.0.0 Safari/537.36",
            "Accept-Language": "en-US,en;q=0.9",
            "Accept-Encoding": "gzip, deflate, br",
            "Accept":          "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            "Cache-Control":   "no-cache",
        },
        {
            "User-Agent":      "Mozilla/5.0 (Macintosh; Intel Mac OS X 14_4) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.4 Safari/605.1.15",
            "Accept-Language": "en-GB,en;q=0.8",
            "Accept-Encoding": "gzip, deflate, br",
            "Accept":          "application/rss+xml,application/xml,text/xml,*/*;q=0.8",
        },
        {
            "User-Agent":      "Mozilla/5.0 (X11; Linux x86_64; rv:124.0) Gecko/20100101 Firefox/124.0",
            "Accept-Language": "en-US,en;q=0.5",
            "Accept-Encoding": "gzip, deflate, br",
            "Accept":          "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,*/*;q=0.8",
        },
        {
            # Google Feedfetcher — widely whitelisted by RSS providers
            "User-Agent":      "Feedfetcher-Google; (+http://www.google.com/feedfetcher.html)",
            "Accept":          "application/rss+xml, application/xml, text/xml",
            "Accept-Encoding": "gzip",
        },
        {
            # RSS validator UA — also commonly whitelisted
            "User-Agent":      "Mozilla/5.0 (compatible; FeedValidator/1.3; +https://validator.w3.org/feed/)",
            "Accept":          "application/atom+xml, application/rss+xml, application/xml",
        },
    ]

    _local = threading.local()

    def __init__(
        self,
        limiter:    DomainLimiter,
        circuit:    CircuitBreaker,
        etag_cache: ConditionalGetCache,
    ):
        self.limiter    = limiter
        self.circuit    = circuit
        self.etag_cache = etag_cache
        # Max 3 concurrent connections to any single domain
        self._domain_sems: dict = defaultdict(lambda: threading.Semaphore(3))

    def _get_session(self) -> SessionPage:
        if not hasattr(self._local, "session"):
            self._local.session = SessionPage()
        return self._local.session

    @staticmethod
    def _domain(url: str) -> str:
        try:
            return urlparse(url).netloc
        except Exception:
            return url

    def _profile(self) -> dict:
        return random.choice(self.UA_PROFILES)

    def process_feed(self, name: str, url: str, category: str, max_retries: int = 3) -> list:
        domain  = self._domain(url)
        session = self._get_session()
        backoff = 1.5

        # 1. Circuit breaker — skip immediately if domain is cooling down
        if self.circuit.is_open(domain):
            log.debug(f"  ⚡ Skipped (circuit open): {name}")
            return []

        for attempt in range(max_retries):
            try:
                # 2. Per-domain concurrency cap
                with self._domain_sems[domain]:
                    # 3. Rate limiter
                    self.limiter.wait_if_needed(url)

                    # 4+5. Full header profile + conditional GET headers
                    headers = {**self._profile(), **self.etag_cache.get_headers(url)}
                    session.set.headers(headers)
                    session.get(url, timeout=15)
                    code = session.response.status_code

                # 6. Not Modified — cheapest possible outcome
                if code == 304:
                    log.debug(f"  ↩ 304 Not Modified: {name}")
                    self.circuit.record_success(domain)
                    return []

                # 7. Rate limited — respect Retry-After
                if code == 429:
                    retry_after = int(session.response.headers.get("Retry-After", backoff * 2))
                    log.warning(f"  [429] {name} — backing off {retry_after}s")
                    self.circuit.record_failure(domain, code)
                    time.sleep(retry_after)
                    backoff *= 2
                    continue

                # 8. Permanent blocks — instant circuit trip, no retries
                if code in CircuitBreaker.PERM_BLOCK:
                    log.warning(f"  [HTTP {code}] {name} — permanent block, circuit tripped")
                    self.circuit.record_failure(domain, code)
                    return []

                # Other non-200
                if code != 200:
                    log.info(f"  ✗ [{category:9s}] {name[:45]:<45} → HTTP {code}")
                    self.circuit.record_failure(domain, code)
                    break

                # ── Success ───────────────────────────────────────────
                self.etag_cache.update(url, dict(session.response.headers))
                self.circuit.record_success(domain)

                parsed = feedparser.parse(session.response.text)
                items  = []
                for entry in parsed.entries[:5]:
                    items.append(NewsItem(
                        source    = name,
                        news_type = category,
                        title     = entry.get("title", "N/A").strip(),
                        link      = entry.get("link",  "N/A"),
                        date      = entry.get("published", entry.get("updated", "No Date")),
                    ))

                if items:
                    log.info(f"  ✓ [{category:9s}] {name[:45]:<45} → {len(items)} item(s)")
                else:
                    log.info(f"  ○ [{category:9s}] {name[:45]:<45} → empty feed")
                return items

            except Exception as exc:
                # 9. Jitter on backoff — unpredictable timing
                jitter = random.uniform(0.2, 1.2)
                log.debug(f"  [attempt {attempt+1}] {name}: {exc}")
                time.sleep(backoff + jitter)
                backoff *= 2

        return []


# ══════════════════════════════════════════════════════════════════════
# MODULE 7 – DATA WAREHOUSE  (CSV, incremental, thread-safe dedup)
# ══════════════════════════════════════════════════════════════════════
class DataWarehouse:
    FIELDS = ["source", "news_type", "title", "link", "date", "fetched_at"]

    def __init__(self, filename: str = "market_news_warehouse.csv"):
        self.filename    = filename
        self._lock       = threading.Lock()
        self.seen_links: set = set()
        self._initialize()

    def _initialize(self):
        if os.path.exists(self.filename):
            with open(self.filename, "r", encoding="utf-8") as f:
                for row in csv.DictReader(f):
                    if "link" in row:
                        self.seen_links.add(row["link"])
            log.info(f"Warehouse: {len(self.seen_links):,} known links — {os.path.abspath(self.filename)}")

    def save_batch(self, items: list) -> int:
        with self._lock:
            new_items = [i for i in items if i.link not in self.seen_links]
            if not new_items:
                return 0
            file_exists = os.path.isfile(self.filename)
            with open(self.filename, "a", newline="", encoding="utf-8") as f:
                writer = csv.DictWriter(f, fieldnames=self.FIELDS)
                if not file_exists:
                    writer.writeheader()
                for item in new_items:
                    writer.writerow(asdict(item))
                    self.seen_links.add(item.link)
            return len(new_items)


# ══════════════════════════════════════════════════════════════════════
# MODULE 8 – NEWS ENGINE  (producer / worker / drain pipeline)
# ══════════════════════════════════════════════════════════════════════
_SENTINEL = object()   # unique stop signal — not None so it can't be confused with data

class NewsEngine:
    """
    Three-stage concurrent pipeline:

      Producer thread   → SourceManager.get_all_feeds() → work_queue
      Worker threads    → WorkerPool.process_feed()      → result_queue
      Drain thread      → DataWarehouse.save_batch()     → CSV on disk

    Workers begin fetching the MOMENT the producer pushes the first URL.
    No waiting for the full feed list to be built first.
    """

    def __init__(
        self,
        max_workers:    int = 50,
        cycle_min_wait: int = 90,
        cycle_max_wait: int = 150,
    ):
        self.max_workers = max_workers
        self.min_wait    = cycle_min_wait
        self.max_wait    = cycle_max_wait

        # Shared anti-block state — persists across cycles
        self.circuit     = CircuitBreaker()
        self.etag_cache  = ConditionalGetCache()
        self.limiter     = DomainLimiter(rate=1, window=2)
        self.worker_pool = WorkerPool(self.limiter, self.circuit, self.etag_cache)
        self.warehouse   = DataWarehouse()

    # ── Stage 1: Producer ─────────────────────────────────────────────
    def _producer(self, work_queue: Queue, total_ref: list):
        log.info("⚙️  Producer: building feed list…")
        feeds = SourceManager().get_all_feeds()
        total_ref[0] = len(feeds)
        log.info(f"⚙️  Producer: {len(feeds):,} feeds ready — streaming to workers now")
        for name, (url, cat) in feeds.items():
            work_queue.put((name, url, cat))
        # One sentinel per worker so each thread knows when to stop
        for _ in range(self.max_workers):
            work_queue.put(_SENTINEL)
        log.info("⚙️  Producer: done.")

    # ── Stage 2: Worker ───────────────────────────────────────────────
    def _worker(self, work_queue: Queue, result_queue: Queue):
        while True:
            item = work_queue.get()
            if item is _SENTINEL:
                work_queue.task_done()
                result_queue.put(_SENTINEL)   # notify drain this worker finished
                break
            name, url, cat = item
            try:
                results = self.worker_pool.process_feed(name, url, cat)
            except Exception as exc:
                log.debug(f"Worker unhandled error [{name}]: {exc}")
                results = []
            result_queue.put((name, results))
            work_queue.task_done()

    # ── Stage 3: Drain ────────────────────────────────────────────────
    def _drain(self, result_queue: Queue, total_ref: list, cycle_start: datetime):
        completed    = 0
        saved_total  = 0
        workers_done = 0

        while workers_done < self.max_workers:
            item = result_queue.get()

            if item is _SENTINEL:
                workers_done += 1
                result_queue.task_done()
                continue

            name, results = item
            if results:
                saved = self.warehouse.save_batch(results)
                saved_total += saved
            else:
                saved = 0

            completed += 1
            total   = total_ref[0] or "?"
            elapsed = int((datetime.now(timezone.utc) - cycle_start).total_seconds())

            log.info(
                f"  {'✓' if results else '○'} [{completed:>5}/{total}] "
                f"{name[:48]:<48} | +{len(results):>2} | "
                f"saved={saved_total:>5} | {elapsed}s"
            )
            result_queue.task_done()

        # Flush ETag cache to disk at end of cycle
        self.etag_cache.flush()

        elapsed = int((datetime.now(timezone.utc) - cycle_start).total_seconds())
        log.info("─" * 70)
        log.info(
            f"✅ Cycle complete | {saved_total:,} new articles | "
            f"DB: {len(self.warehouse.seen_links):,} total | "
            f"Circuit: {self.circuit.stats()} | "
            f"{elapsed}s"
        )
        log.info("─" * 70)

    # ── Orchestrator ──────────────────────────────────────────────────
    def _run_cycle(self):
        cycle_start = datetime.now(timezone.utc)
        log.info("═" * 70)
        log.info(f"🚀 Cycle started | {cycle_start.strftime('%Y-%m-%d %H:%M:%S')} UTC")
        log.info(f"   CSV    → {os.path.abspath(self.warehouse.filename)}")
        log.info(f"   Circuit: {self.circuit.stats()}")
        log.info("═" * 70)

        work_queue   = Queue(maxsize=200)   # backpressure so producer doesn't flood RAM
        result_queue = Queue()
        total_ref    = [0]                  # mutable container so drain can read final count

        prod_thread = threading.Thread(
            target=self._producer, args=(work_queue, total_ref),
            daemon=True, name="Producer"
        )
        worker_threads = [
            threading.Thread(
                target=self._worker, args=(work_queue, result_queue),
                daemon=True, name=f"Worker-{i}"
            )
            for i in range(self.max_workers)
        ]
        drain_thread = threading.Thread(
            target=self._drain, args=(result_queue, total_ref, cycle_start),
            daemon=True, name="Drain"
        )

        # Start drain first — it must be ready before workers push results
        drain_thread.start()
        prod_thread.start()
        for w in worker_threads:
            w.start()

        # Block until all workers done, then wait for drain to finish writing
        for w in worker_threads:
            w.join()
        drain_thread.join()

    # ── 24/7 loop ────────────────────────────────────────────────────
    def start(self):
        log.info("═" * 70)
        log.info("  MarketPulse News Engine v3.0")
        log.info(f"  Workers : {self.max_workers}")
        log.info(f"  Cycle   : every {self.min_wait}–{self.max_wait}s")
        log.info(f"  Files   : {os.getcwd()}")
        log.info("═" * 70)
        while True:
            self._run_cycle()
            wait = random.randint(self.min_wait, self.max_wait)
            log.info(f"⏳ Next cycle in {wait}s…")
            time.sleep(wait)


# ══════════════════════════════════════════════════════════════════════
# ENTRY POINT
# ══════════════════════════════════════════════════════════════════════
if __name__ == "__main__":
    engine = NewsEngine(
        max_workers    = 50,
        cycle_min_wait = 90,
        cycle_max_wait = 150,
    )
    engine.start()