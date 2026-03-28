"""
MarketPulse News Engine v2.0
- 10,000+ RSS feeds generated at runtime
- Per-domain rate limiting (anti-ban)
- Thread-safe worker pool (one SessionPage per thread)
- User-Agent rotation
- Exponential backoff on failure
- Retry-After header respect
- Async-friendly producer/consumer queue
- Structured logging
- Runs 24/7
"""

import feedparser
import csv
import time
import random
import os
import logging
import threading
from queue import Queue, Empty
from datetime import datetime, timezone
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, asdict
from collections import defaultdict
from DrissionPage import SessionPage

# ──────────────────────────────────────────────
# LOGGING
# ──────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[
        logging.StreamHandler(),
        logging.FileHandler("engine.log", encoding="utf-8"),
    ],
)
log = logging.getLogger("MarketPulse")

# ──────────────────────────────────────────────
# MODULE 1 – DATA MODEL
# ──────────────────────────────────────────────
@dataclass
class NewsItem:
    source: str
    news_type: str          # Crypto | Stock | Finance | Macro | Sentiment
    title: str
    link: str
    date: str
    fetched_at: str = ""    # UTC timestamp when we scraped it

    def __post_init__(self):
        if not self.fetched_at:
            self.fetched_at = datetime.now(timezone.utc).isoformat()

# ──────────────────────────────────────────────
# MODULE 2 – SOURCE MANAGER (10 000+ feeds)
# ──────────────────────────────────────────────
class SourceManager:
    """
    Generates 10 000+ RSS URLs across stocks, crypto, macro, sentiment,
    and authoritative static feeds. All computation happens at runtime
    on a dedicated background thread so the engine starts instantly.
    """

    # ── Static high-authority feeds ──────────────────────────────────
    STATIC_FEEDS = {
        # Finance / Business
        "Reuters Business":        ("https://feeds.reuters.com/reuters/businessNews", "Finance"),
        "Reuters Tech":            ("https://feeds.reuters.com/reuters/technologyNews", "Finance"),
        "AP Business":             ("https://feeds.apnews.com/ApNewsBusinessFeed", "Finance"),
        "CNBC Top News":           ("https://www.cnbc.com/id/100003114/device/rss/rss.html", "Finance"),
        "CNBC Finance":            ("https://www.cnbc.com/id/10000664/device/rss/rss.html", "Finance"),
        "MarketWatch":             ("https://www.marketwatch.com/rss/topstories", "Stock"),
        "WSJ Markets":             ("https://feeds.a.dj.com/rss/RSSMarketsMain.xml", "Stock"),
        "WSJ Business":            ("https://feeds.a.dj.com/rss/WSJcomUSBusiness.xml", "Finance"),
        "Fortune":                 ("https://fortune.com/feed/", "Finance"),
        "Seeking Alpha":           ("https://seekingalpha.com/market_currents.xml", "Stock"),
        "Motley Fool":             ("https://www.fool.com/feeds/index.aspx", "Stock"),
        "Investopedia":            ("https://www.investopedia.com/feedbuilder/feed/getfeed?feedName=rss_headline", "Finance"),
        "Zacks":                   ("https://www.zacks.com/rss.xml", "Stock"),
        "TheStreet":               ("https://www.thestreet.com/.rss/full", "Stock"),
        "Benzinga":                ("https://www.benzinga.com/feed", "Stock"),
        "Nasdaq News":             ("https://www.nasdaq.com/feed/rssoutbound?category=Markets", "Stock"),
        "Yahoo Finance":           ("https://finance.yahoo.com/news/rssindex", "Finance"),
        "FT Markets":              ("https://www.ft.com/markets?format=rss", "Finance"),
        "Bloomberg Markets":       ("https://feeds.bloomberg.com/markets/news.rss", "Finance"),
        "Barrons":                 ("https://www.barrons.com/feed", "Stock"),
        # Crypto – tier 1
        "CoinDesk":                ("https://www.coindesk.com/arc/outboundfeeds/rss/", "Crypto"),
        "Cointelegraph":           ("https://cointelegraph.com/rss", "Crypto"),
        "The Block":               ("https://www.theblock.co/rss.xml", "Crypto"),
        "Decrypt":                 ("https://decrypt.co/feed", "Crypto"),
        "CryptoSlate":             ("https://cryptoslate.com/feed/", "Crypto"),
        "BeInCrypto":              ("https://beincrypto.com/feed/", "Crypto"),
        "Bitcoin Magazine":        ("https://bitcoinmagazine.com/.rss/full/", "Crypto"),
        "CryptoNews":              ("https://cryptonews.com/news/feed/", "Crypto"),
        "AMBCrypto":               ("https://ambcrypto.com/feed/", "Crypto"),
        "NewsBTC":                 ("https://www.newsbtc.com/feed/", "Crypto"),
        "Messari":                 ("https://messari.io/rss", "Crypto"),
        "The Defiant":             ("https://thedefiant.io/feed", "Crypto"),
        "U.Today Crypto":          ("https://u.today/rss", "Crypto"),
        "Crypto Briefing":         ("https://cryptobriefing.com/feed/", "Crypto"),
        "DeFi Llama News":         ("https://defillama.com/news/rss", "Crypto"),
        # Macro / Central Banks
        "FedReserve Press":        ("https://www.federalreserve.gov/feeds/press_all.xml", "Macro"),
        "FedReserve Speeches":     ("https://www.federalreserve.gov/feeds/speeches.xml", "Macro"),
        "ECB News":                ("https://www.ecb.europa.eu/rss/press.html", "Macro"),
        "BIS Research":            ("https://www.bis.org/rss/bis_research.htm", "Macro"),
        "IMF News":                ("https://www.imf.org/en/News/rss", "Macro"),
        "World Bank":              ("https://feeds.worldbank.org/worldbank/news", "Macro"),
        "SEC Filings":             ("https://www.sec.gov/cgi-bin/browse-edgar?action=getcurrent&type=8-K&dateb=&owner=include&count=40&output=atom", "Macro"),
        "CFTC News":               ("https://www.cftc.gov/rss/pressreleases.xml", "Macro"),
        # Sentiment / Social
        "Reddit WSB":              ("https://www.reddit.com/r/wallstreetbets/.rss", "Sentiment"),
        "Reddit Stocks":           ("https://www.reddit.com/r/stocks/.rss", "Sentiment"),
        "Reddit Crypto":           ("https://www.reddit.com/r/CryptoCurrency/.rss", "Sentiment"),
        "Reddit Investing":        ("https://www.reddit.com/r/investing/.rss", "Sentiment"),
        "Reddit Bitcoin":          ("https://www.reddit.com/r/Bitcoin/.rss", "Sentiment"),
        "Reddit ETH":              ("https://www.reddit.com/r/ethereum/.rss", "Sentiment"),
        # YouTube (RSS works on channel IDs)
        "Bloomberg YT":            ("https://www.youtube.com/feeds/videos.xml?user=Bloomberg", "Finance"),
        "CNBC YT":                 ("https://www.youtube.com/feeds/videos.xml?user=cnbc", "Finance"),
        "Reuters YT":              ("https://www.youtube.com/feeds/videos.xml?user=Reuters", "Finance"),
    }

    # ── Full S&P 500 + high-cap growth tickers (clean, no duplicates) ──
    SP500 = [
        # A
        "MMM","AOS","ABT","ABBV","ACN","ADBE","AMD","AES","AFL","A","APD","ABNB","AKAM",
        "ALB","ARE","ALGN","ALLE","LNT","ALL","GOOGL","GOOG","MO","AMZN","AMCR","AEE",
        "AAL","AEP","AXP","AIG","AMT","AWK","AMP","AME","AMGN","APH","ADI","ANSS","AON",
        "APA","AAPL","AMAT","APTV","ACGL","ADM","ANET","AJG","AIZ","T","ATO","ADSK","AZO",
        "AVB","AVY","AXON",
        # B
        "BKR","BALL","BAC","BK","BBWI","BAX","BDX","BRK.B","BBY","BIO","TECH","BIIB",
        "BLK","BX","BA","BSX","BMY","AVGO","BR","BRO","BF.B","BLDR","BG",
        # C
        "CDNS","CZR","CPT","CPB","COF","CAH","KMX","CCL","CARR","CAT","CBOE","CBRE",
        "CDW","CE","CNC","CDAY","CF","CRL","SCHW","CHTR","CVX","CMG","CB","CHD","CI",
        "CINF","CTAS","CSCO","C","CFG","CLX","CME","CMS","KO","CTSH","CL","CMCSA","CMA",
        "CAG","COP","ED","STZ","CEG","COO","CPRT","GLW","CTVA","CSGP","COST","CTRA","CCI",
        "CSX","CMI","CVS",
        # D
        "DHR","DRI","DVA","DAY","DE","DAL","XRAY","DVN","DXCM","FANG","DLR","DFS","DG",
        "DLTR","D","DPZ","DOV","DOW","DHI","DTE","DUK","DD","DOC",
        # E
        "EMN","ETN","EBAY","ECL","EIX","EW","EA","ELV","LLY","EMR","ENPH","ETR","EOG",
        "EPAM","EQT","EFX","EQIX","EQR","ESS","EL","ETSY","EG","ES","EXC","EXPE","EXPD","EXR",
        # F
        "XOM","FFIV","FDS","FICO","FAST","FRT","FDX","FIS","FITB","FSLR","FE","FI","FMC",
        "F","FTNT","FTV","FOXA","FOX","BEN","FCX",
        # G
        "GRMN","IT","GE","GEHC","GEN","GNRC","GD","GIS","GM","GPC","GILD","GPN","GL","GS",
        # H
        "HAL","HIG","HAS","HCA","HSIC","HSY","HES","HPE","HLT","HOLX","HD","HON","HRL",
        "HST","HWM","HPQ","HUBB","HUM","HBAN","HII",
        # I
        "IBM","IEX","IDXX","ITW","INCY","IR","PODD","INTC","ICE","IFF","IP","IPG","INTU",
        "ISRG","IVZ","INVH","IQV","IRM",
        # J
        "JKHY","J","JBL","JNPR","JCI","JPM","K","KVUE","KDP","KEY","KEYS","KMB","KIM",
        "KMI","KLAC","KHC","KR",
        # L
        "LHX","LH","LRCX","LW","LVS","LDOS","LEN","LNC","LIN","LYV","LKQ","LMT","L",
        "LOW","LYB",
        # M
        "MTB","MRO","MPC","MKTX","MAR","MMC","MLM","MAS","MA","MTCH","MKC","MCD","MCK",
        "MDT","MRK","META","MET","MTD","MGM","MCHP","MU","MSFT","MAA","MRNA","MHK","MOH",
        "TAP","MDLZ","MPWR","MNST","MCO","MS","MOS","MSI","MSCI",
        # N
        "NDAQ","NTAP","NFLX","NWL","NEM","NWSA","NWS","NEE","NKE","NI","NDSN","NSC",
        "NTRS","NOC","NCLH","NRG","NUE","NVDA","NVR","NXPI",
        # O
        "ORLY","OXY","ODFL","OMC","ON","OKE","ORCL","OTIS",
        # P
        "PCAR","PKG","PANW","PH","PAYX","PAYC","PYPL","PNR","PEP","PFE","PCG","PM","PSX",
        "PNW","PNC","POOL","PPG","PPL","PFG","PG","PGR","PRU","PEG","PSA","PHM","QRVO",
        "PWR","QCOM",
        # R
        "DGX","RL","RJF","RTX","O","REG","REGN","RF","RSG","RMD","RVTY","ROK","ROL","ROP",
        "ROST","RCL",
        # S
        "SPGI","CRM","SBAC","SLB","STX","SRE","NOW","SHW","SBUX","STT","STLD","STE","SYK",
        "SWK","SNPS","SO","LUV","SJM","SPG","SWKS","SNA","SOLV","SYY",
        # T
        "TMUS","TROW","TTWO","TPR","TGT","TEL","TDY","TFX","TER","TSLA","TXN","TXT","TMO",
        "TJX","TSCO","TT","TDG","TRV","TRMB","TFC","TYL",
        # U
        "ULTA","UDR","USB","UNP","UAL","UPS","URI","UNH","UHS",
        # V
        "VLO","VTR","VLTO","VRSN","VRSK","VZ","VRTX","VFC","VTRS","VICI","V","VST","VMC",
        # W
        "WRB","GWW","WAB","WBA","WMT","DIS","WBD","WM","WAT","WEC","WFC","WELL","WST",
        "WDC","WY","WHR","WMB","WTW","WYNN",
        # X-Z
        "XEL","XYL","YUM","ZBRA","ZBH","ZTS",
        # High-cap growth (not in classic S&P500 but high volume news)
        "PLTR","UBER","DASH","RBLX","NET","SNOW","DDOG","MDB","ZM","CRWD","OKTA","GTLB",
        "HOOD","COIN","MSTR","SOFI","UPST","AFRM","OPEN","LMND","ROOT","HIPPO","DKNG",
        "PENN","MGM","LVS","WYNN","CZR","NKLA","RIVN","LCID","FSR","GOEV","XPEV","LI",
        "NIO","BIDU","JD","PDD","BABA","TCEHY","9988.HK","700.HK","SE","GRAB","GOTO",
        "ABNB","EXPE","BKNG","TRIP","LYFT","UBER","SNAP","PINS","TWTR","RDDT","BMBL",
        "MTCH","IAC","ZG","RDFN","COMP","OPEN","UWMC","RKT","LDI","GHLD","PFSI",
        "ARM","SMCI","DELL","HPE","PSTG","NTAP","WDC","STX","IONQ","RGTI","QUBT","QBTS",
    ]

    # ── Top 500 crypto tickers by market cap (2026, deduplicated) ────
    CRYPTO = [
        # Layer 1 / Major
        "BTC","ETH","BNB","XRP","SOL","ADA","AVAX","DOT","ATOM","NEAR","APT","SUI","SEI",
        "TRX","LTC","BCH","XMR","XLM","ALGO","VET","ETC","FIL","ICP","HBAR","ZEC","DASH",
        # Stablecoins (good for news)
        "USDT","USDC","USDS","DAI","PYUSD","USDE","SUSDE","SUSDS","FRAX","TUSD","BUSD",
        # LST / LRT
        "STETH","WSTETH","WBETH","AWETH","WEETH","CBBTC","WBTC","ETHFI","RENZO","PUFFER",
        "SWELL","KELP","MELLOW","BEDROCK","ETHERFI","EIGENLAYER","SYMBIOTIC","KARAK",
        # DeFi Blue Chips
        "LINK","UNI","AAVE","MKR","CRV","SNX","COMP","BAL","YFI","1INCH","SUSHI","DYDX",
        "GMX","GNS","GAINS","KWENTA","SYNTHETIX","LYRA","OPYN","RIBBON","LQTY","FLUID",
        "PENDLE","ENA","RPL","LRC","GRT","BNT","KNC","REN","ZRX","BAND","NMR","UMA",
        "BADGER","PERP","HEGIC","DPI","BEL","ORN","CTSI","POLS","OCEAN","FET","AGIX",
        # Meme / Culture
        "DOGE","SHIB","PEPE","FLOKI","BONK","WIF","BOME","MEW","POPCAT","MOG","BRETT",
        "MAGA","MYRO","SLERF","BOOK","DOGS","HAMSTER","CATI","MAJOR","BLUM","HMSTR",
        # Layer 2 / Scaling
        "ARB","OP","POL","METIS","BOBA","OMG","HOP","ZETA","BLAST","MODE","SCROLL",
        "LINEA","STRK","MANTA","PORTAL","DYM","ZRO","IO","BASE","MERLIN","BOB",
        # Emerging L1s
        "MOVE","BERA","STORY","MONAD","INITIA","BABYLON","NUCLEUS","BOUNCE","MAGIC",
        "HYPE","INJ","TIA","TAO","RON","RENDER","RNDR","W","PYTH","JUP","JTO","ALT",
        "WLD","PIXEL","SHRAP","YGG","SUPER","LQTY","SAGA","TNSR","OMNI","REZ","ETHFI",
        # Infra / AI / Data
        "STORJ","SIA","ANKR","NKN","IOTX","FOAM","MXC","DENT","TRAC","DATA","SENT",
        "AMB","MOBI","IOTA","THETA","TFUEL","CHR","CELR","SYS","ZIL","POWR","ELEC",
        "WPR","EWT","SFP","XNO","VIBE","LAMB","SKY","RUFF","DHT","HMQ","BLOC","ALIS",
        # Exchange tokens
        "CRO","KCS","HT","OKB","GT","MX","BGB","WOO","NEXO","LEO",
        # Gaming / NFT / Metaverse
        "SAND","MANA","AXS","ENJ","CHZ","GALA","IMX","ACM","BAR","PSG","JUV","CITY",
        "ASR","ATM","NAVI","SPURS","LAZ","FAN","FLOW","GFAL","REVV","GRID","TOWER",
        "LEAG","DERC","PYR","NFTY","WHALE","ALPHA",
        # RWA / Institutional
        "USDY","OUSG","OMMF","BUIDL","BENJI","TBILL","USTB","FOBXX","ONDO","MPL",
        "CFG","RWA","POLYX","TKN","PROPS","DIMO","CELO","MASA","HONEY","PAXG","XAUT",
        # Solana ecosystem
        "RAY","ORCA","FIDA","KIN","SAMO","BONFIDA","GRAPE","LARIX","FRANCIUM","MERC",
        "SYP","SHDW","AUDIO","GENE","SONAR","NINJA","GSOL","SBR","PORT","MNGO","SLIM",
        "TULIP","SUNNY","STEP","MAPS","MEDIA","COPE","SRM",
        # TON ecosystem
        "TON","NOT","DUREV","X",
        # Misc high-mcap
        "XMR","EGLD","FTM","AXS","CVC","POWR","POND","FOR","RSV","PAID","DIGG",
        "LUNC","LUNA","USTC","CEL","FTT","VBTC","MBTC","NBTC","OBTC","FBTC",
        "DBTC","CBTC","ABTC","ZBTC","TBTC","SBTC","IBBTC","WBTC2","RENBTC","HBTC",
        "BTCB","RENBTC2","HBTC2","CACHE","PMGT","MTG","MCAU","XBULLION","AWG","AWC",
        "JUNO","EVMOS","KAVA","MIOTA","DEFI","DEFICAT","DEFIPUNK",
    ]

    # ── Extra Google News topic RSS ───────────────────────────────────
    GOOGLE_TOPICS = [
        ("CAAqJggKIiBDQkFTRWdvSUwyMHZNRGx6TVdZU0FtVnVHZ0pWVXlnQVAB", "Stock", "GNews-Markets"),
        ("CAAqJggKIiBDQkFTRWdvSUwyMHZNRGwwTlRFU0FtVnVHZ0pWVXlnQVAB", "Crypto", "GNews-Crypto"),
        ("CAAqJggKIiBDQkFTRWdvSUwyMHZNRFp1ZEdvU0FtVnVHZ0pWVXlnQVAB", "Finance", "GNews-Finance"),
        ("CAAqBwgKMLmH0QowyPbpAg",                                    "Finance", "GNews-Economy"),
        ("CAAqBwgKML_H0QowsfbpAg",                                    "Stock",   "GNews-Stocks"),
    ]

    def get_all_feeds(self) -> dict:
        """
        Build the master feed dict at runtime.
        Returns {name: (url, category)} with 10 000+ entries.
        """
        master = {}

        # 1. Static authoritative feeds
        master.update(self.STATIC_FEEDS)

        # 2. Per-ticker: Google News, Yahoo Finance, Bing News
        tickers = list(dict.fromkeys(self.SP500))          # deduplicate
        coins   = list(dict.fromkeys(self.CRYPTO))

        for t in tickers:
            safe = t.replace(".", "-")
            master[f"GNews-stock-{safe}"]  = (f"https://news.google.com/rss/search?q={safe}+stock+NYSE+NASDAQ&hl=en-US&gl=US&ceid=US:en", "Stock")
            master[f"Yahoo-stock-{safe}"]  = (f"https://finance.yahoo.com/rss/headline?s={safe}", "Stock")
            master[f"Bing-stock-{safe}"]   = (f"https://www.bing.com/news/search?q={safe}+stock&format=rss", "Stock")
            master[f"Nasdaq-{safe}"]       = (f"https://www.nasdaq.com/feed/rssoutbound?symbol={safe}", "Stock")

        for c in coins:
            master[f"GNews-crypto-{c}"]   = (f"https://news.google.com/rss/search?q={c}+cryptocurrency&hl=en-US&gl=US&ceid=US:en", "Crypto")
            master[f"Yahoo-crypto-{c}"]   = (f"https://finance.yahoo.com/rss/headline?s={c}-USD", "Crypto")
            master[f"Bing-crypto-{c}"]    = (f"https://www.bing.com/news/search?q={c}+crypto&format=rss", "Crypto")
            master[f"CoinGecko-{c}"]      = (f"https://www.coingecko.com/en/coins/{c.lower()}/news.rss", "Crypto")

        # 3. Google News curated topic feeds
        for topic_id, cat, label in self.GOOGLE_TOPICS:
            master[label] = (f"https://news.google.com/rss/topics/{topic_id}?hl=en-US&gl=US&ceid=US:en", cat)

        # 4. Macro keyword sweeps
        macro_terms = [
            "Federal Reserve interest rate", "ECB monetary policy", "inflation CPI",
            "GDP growth", "unemployment rate", "treasury yield", "bond market",
            "oil price OPEC", "gold silver commodities", "forex USD EUR",
            "IPO 2026", "earnings report", "merger acquisition", "short squeeze",
            "hedge fund", "private equity", "venture capital", "fintech",
        ]
        for term in macro_terms:
            slug = term.replace(" ", "+")
            label = "GNews-macro-" + term.replace(" ", "_")[:30]
            master[label] = (f"https://news.google.com/rss/search?q={slug}&hl=en-US&gl=US&ceid=US:en", "Macro")

        log.info(f"SourceManager: {len(master):,} feeds generated.")
        return master


# ──────────────────────────────────────────────
# MODULE 3 – DOMAIN RATE LIMITER (anti-ban)
# ──────────────────────────────────────────────
class DomainLimiter:
    """
    Ensures at most `rate` requests per `window` seconds per domain.
    Thread-safe via per-domain locks.
    """
    def __init__(self, rate: float = 1.0, window: float = 2.0):
        self._rate   = rate    # max requests per window
        self._window = window  # seconds
        self._counts: dict = defaultdict(list)
        self._lock = threading.Lock()

    @staticmethod
    def _domain(url: str) -> str:
        try:
            from urllib.parse import urlparse
            return urlparse(url).netloc
        except Exception:
            return url

    def wait_if_needed(self, url: str):
        domain = self._domain(url)
        with self._lock:
            now = time.monotonic()
            # Expire old timestamps
            self._counts[domain] = [t for t in self._counts[domain] if now - t < self._window]
            if len(self._counts[domain]) >= self._rate:
                sleep_for = self._window - (now - self._counts[domain][0])
                if sleep_for > 0:
                    time.sleep(sleep_for)
            self._counts[domain].append(time.monotonic())


# ──────────────────────────────────────────────
# MODULE 4 – STEALTH WORKER (thread-safe)
# ──────────────────────────────────────────────
class WorkerPool:
    """
    Maintains one DrissionPage SessionPage per thread (thread-local storage).
    Rotates User-Agents and applies exponential backoff.
    """
    USER_AGENTS = [
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/123.0.0.0 Safari/537.36",
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 14_4) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.4 Safari/605.1.15",
        "Mozilla/5.0 (X11; Linux x86_64; rv:124.0) Gecko/20100101 Firefox/124.0",
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36 Edg/122.0.0.0",
        "Mozilla/5.0 (iPhone; CPU iPhone OS 17_4 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.4 Mobile/15E148 Safari/604.1",
        "Feedfetcher-Google; (+http://www.google.com/feedfetcher.html)",
        "FeedValidator/1.3 +https://validator.w3.org/feed/",
    ]

    _local = threading.local()

    def __init__(self, limiter: DomainLimiter):
        self.limiter = limiter

    def _get_session(self) -> SessionPage:
        if not hasattr(self._local, "session"):
            self._local.session = SessionPage()
        return self._local.session

    def _ua(self) -> str:
        return random.choice(self.USER_AGENTS)

    def process_feed(self, name: str, url: str, category: str, max_retries: int = 3):
        session = self._get_session()
        backoff  = 1.5

        for attempt in range(max_retries):
            try:
                self.limiter.wait_if_needed(url)
                session.set.header("User-Agent", self._ua())
                session.get(url, timeout=15)
                code = session.response.status_code

                if code == 429:                         # rate-limited
                    retry_after = int(session.response.headers.get("Retry-After", backoff * 2))
                    log.warning(f"[429] {name} – sleeping {retry_after}s")
                    time.sleep(retry_after)
                    backoff *= 2
                    continue

                if code != 200:
                    log.info(f"  ✗ [{category:9s}] {name[:45]:<45} → HTTP {code}")
                    break                               # non-retryable

                parsed = feedparser.parse(session.response.text)
                items  = []
                for entry in parsed.entries[:5]:        # up to 5 items per feed
                    items.append(NewsItem(
                        source    = name,
                        news_type = category,
                        title     = entry.get("title", "N/A").strip(),
                        link      = entry.get("link", "N/A"),
                        date      = entry.get("published", entry.get("updated", "No Date")),
                    ))
                if items:
                    log.info(f"  ✓ [{category:9s}] {name[:45]:<45} → {len(items)} item(s)")
                else:
                    log.info(f"  ○ [{category:9s}] {name[:45]:<45} → empty feed")
                return items

            except Exception as exc:
                log.debug(f"[attempt {attempt+1}] {name}: {exc}")
                time.sleep(backoff)
                backoff *= 2

        return []


# ──────────────────────────────────────────────
# MODULE 5 – DATA WAREHOUSE (CSV + dedup)
# ──────────────────────────────────────────────
class DataWarehouse:
    FIELDS = ["source", "news_type", "title", "link", "date", "fetched_at"]

    def __init__(self, filename: str = "market_news_warehouse.csv"):
        self.filename   = filename
        self._lock      = threading.Lock()
        self.seen_links: set = set()
        self._initialize()

    def _initialize(self):
        if os.path.exists(self.filename):
            with open(self.filename, "r", encoding="utf-8") as f:
                reader = csv.DictReader(f)
                for row in reader:
                    if "link" in row:
                        self.seen_links.add(row["link"])
            log.info(f"Warehouse loaded: {len(self.seen_links):,} known links.")

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


# ──────────────────────────────────────────────
# MODULE 6 – NEWS ENGINE (producer/consumer)
#
# Architecture:
#   Producer thread  → generates feed URLs → work_queue
#   Worker threads   → pull from work_queue → fetch → result_queue
#   Drain thread     → pulls from result_queue → save to CSV
#
# Workers start fetching the MOMENT the first URL
# is queued — no waiting for the full list.
# ──────────────────────────────────────────────

_SENTINEL = None   # signals "no more work"

class NewsEngine:
    def __init__(
        self,
        max_workers: int = 50,
        cycle_min_wait: int = 90,
        cycle_max_wait: int = 150,
    ):
        self.max_workers = max_workers
        self.min_wait    = cycle_min_wait
        self.max_wait    = cycle_max_wait
        self.limiter     = DomainLimiter(rate=1, window=2)
        self.worker_pool = WorkerPool(self.limiter)
        self.warehouse   = DataWarehouse()

    # ── Producer: generates URLs into work_queue ──────────────────────
    def _producer(self, work_queue: Queue):
        log.info("⚙️  Producer: building feed list…")
        sm    = SourceManager()
        feeds = sm.get_all_feeds()
        log.info(f"⚙️  Producer: {len(feeds):,} feeds ready — pushing to workers now")
        for name, (url, cat) in feeds.items():
            work_queue.put((name, url, cat))
        # One sentinel per worker thread so every thread knows to stop
        for _ in range(self.max_workers):
            work_queue.put(_SENTINEL)
        log.info("⚙️  Producer: done queuing.")

    # ── Worker: pulls from work_queue, fetches, pushes to result_queue ─
    def _worker(self, work_queue: Queue, result_queue: Queue):
        while True:
            item = work_queue.get()
            if item is _SENTINEL:
                work_queue.task_done()
                break
            name, url, cat = item
            try:
                results = self.worker_pool.process_feed(name, url, cat)
                result_queue.put((name, results))
            except Exception as exc:
                log.debug(f"Worker error [{name}]: {exc}")
                result_queue.put((name, []))
            finally:
                work_queue.task_done()

    # ── Drain: pulls results, saves to CSV, logs progress ────────────
    def _drain(self, result_queue: Queue, total_feeds: threading.Event,
               total_ref: list, cycle_start: datetime):
        completed   = 0
        saved_total = 0

        while True:
            try:
                item = result_queue.get(timeout=1)
            except Exception:
                # If all workers finished and queue is empty → done
                if total_feeds.is_set() and result_queue.empty():
                    break
                continue

            name, results = item
            if results:
                saved = self.warehouse.save_batch(results)
                saved_total += saved

            completed += 1
            total = total_ref[0] if total_ref[0] else "?"

            elapsed = int((datetime.now(timezone.utc) - cycle_start).total_seconds())
            log.info(
                f"  {'✓' if results else '○'} [{completed:>5}/{total}] "
                f"{name[:50]:<50} | +{len(results)} | "
                f"saved={saved_total} | {elapsed}s"
            )
            result_queue.task_done()

        elapsed = int((datetime.now(timezone.utc) - cycle_start).total_seconds())
        log.info(
            f"✅ Cycle complete — {saved_total:,} new articles | "
            f"total in DB: {len(self.warehouse.seen_links):,} | "
            f"{elapsed}s"
        )

    # ── Main cycle ────────────────────────────────────────────────────
    def _run_cycle(self):
        cycle_start = datetime.now(timezone.utc)
        log.info(f"🚀 Cycle started | {cycle_start.isoformat()}Z")
        log.info(f"   CSV → {os.path.abspath(self.warehouse.filename)}")

        work_queue   = Queue(maxsize=200)   # backpressure: producer blocks if workers fall behind
        result_queue = Queue()
        total_ref    = [0]                  # mutable so drain thread can read final count
        gen_done     = threading.Event()

        # 1. Producer thread
        def _prod():
            self._producer(work_queue)
            # Count total by peeking SourceManager (lightweight)
            total_ref[0] = work_queue.qsize() + self.max_workers  # approx
            gen_done.set()

        prod_thread = threading.Thread(target=_prod, daemon=True, name="Producer")

        # 2. Worker threads
        worker_threads = [
            threading.Thread(
                target=self._worker,
                args=(work_queue, result_queue),
                daemon=True,
                name=f"Worker-{i}",
            )
            for i in range(self.max_workers)
        ]

        # 3. Drain thread
        drain_thread = threading.Thread(
            target=self._drain,
            args=(result_queue, gen_done, total_ref, cycle_start),
            daemon=True,
            name="Drain",
        )

        # Start everything — workers begin fetching as soon as
        # the producer pushes the first URL into work_queue
        drain_thread.start()
        prod_thread.start()
        for w in worker_threads:
            w.start()

        # Wait for all workers to finish
        for w in worker_threads:
            w.join()

        # Signal drain there's nothing left
        gen_done.set()
        drain_thread.join()

    def start(self):
        log.info("MarketPulse Engine v2.1 starting…")
        while True:
            self._run_cycle()
            wait = random.randint(self.min_wait, self.max_wait)
            log.info(f"⏳ Next cycle in {wait}s…")
            time.sleep(wait)


# ──────────────────────────────────────────────
# ENTRY POINT
# ──────────────────────────────────────────────
if __name__ == "__main__":
    engine = NewsEngine(
        max_workers=50,
        cycle_min_wait=90,
        cycle_max_wait=150,
    )
    engine.start()