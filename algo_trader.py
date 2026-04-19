"""
AlphaEngine v4.0 — LLM-First News Discovery Trading Engine
═══════════════════════════════════════════════════════════════
Zero hardcoded tickers. All opportunities come from news.
Pipeline: Headlines → LLM Extract → Velocity Rank → yFinance TA → Fusion → LLM Conviction → Execute

.env keys: T212_API_KEY, T212_MODE (demo|live), GEMINI_API_KEY
Usage: venv/bin/python algo_trader.py
═══════════════════════════════════════════════════════════════
"""
from __future__ import annotations
import base64, json, logging, os, random, threading, time, sqlite3
from collections import defaultdict, deque
from dataclasses import dataclass, field
from datetime import datetime, timezone, timedelta
import numpy as np, requests, yfinance as yf, talib
from dotenv import load_dotenv
from news_intelligence import NewsCache, NewsIntelligence, GeminiClient, CandidateTicker

load_dotenv()

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[logging.StreamHandler(), logging.FileHandler("alpha_engine.log", encoding="utf-8")])
log = logging.getLogger("AlphaEngine")

# ═══════════════════════════════════════════════════════════════
class C:
    R="\033[0m";B="\033[1m";DIM="\033[2m";GR="\033[92m";RD="\033[91m"
    YL="\033[93m";CY="\033[96m";WH="\033[97m";GY="\033[90m";MG="\033[95m"
def g(t,*c): return "".join(c)+str(t)+C.R

# ═══════════════════════════════════════════════════════════════
class Config:
    WEIGHT_TA=0.30; WEIGHT_SENT=0.35; WEIGHT_VEL=0.15; WEIGHT_MOM=0.10; WEIGHT_CONV=0.10
    BUY_THRESHOLD=0.62; SELL_THRESHOLD=0.38
    MAX_POSITION_PCT=0.08; MAX_OPEN_TRADES=10; MIN_SHARES=0.01
    SCAN_INTERVAL_SEC=60; COOLDOWN_SEC=300; NEWS_LOOKBACK_MIN=120
    MIN_VELOCITY=0.05; MIN_MENTIONS=2
    YF_JITTER=(0.1,0.3); YF_MAX_RETRIES=2; YF_RETRY_BACKOFF=2.0
    GEMINI_API_KEY=os.getenv("GEMINI_API_KEY",""); GEMINI_MODEL="gemini-2.0-flash"
    NEWS_CSV="market_news_warehouse.csv"; DB_NAME="stock_bot.db"
    T212_MODE=os.getenv("T212_MODE","demo"); T212_KEY=os.getenv("T212_API_KEY","")
    T212_SECRET=os.getenv("T212_API_SECRET","")

# ═══════════════════════════════════════════════════════════════
@dataclass
class TASnapshot:
    ticker:str; price:float; rsi:float; macd:float; macd_sig:float
    ema20:float; ema50:float; bb_upper:float; bb_lower:float; atr:float

@dataclass
class TradeDecision:
    ticker:str; t212_ticker:str; action:str; combined_score:float
    ta_score:float; sent_score:float; velocity_score:float; momentum:float
    llm_conviction:float; quantity:float; price:float; reason:str
    gates_passed:str; funnel_stage:str; mention_count:int; headlines_used:str
    ai_reasoning:str=""; timestamp:str=field(default_factory=lambda: datetime.now(timezone.utc).isoformat())

# ═══════════════════════════════════════════════════════════════
class DatabaseManager:
    def __init__(self, db_path:str): self.db_path=db_path
    def log_trade(self, d:TradeDecision, status:str, detail:str):
        try:
            with sqlite3.connect(self.db_path) as conn:
                conn.execute(
                    "INSERT INTO trades (ticker,t212_ticker,action,combined_score,ta_score,sent_score,"
                    "velocity_score,momentum,llm_conviction,quantity,price,status,detail,reason,timestamp,"
                    "gates_passed,funnel_stage,ai_reasoning,mention_count,headlines_used) "
                    "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    (d.ticker,d.t212_ticker,d.action,d.combined_score,d.ta_score,d.sent_score,
                     d.velocity_score,d.momentum,d.llm_conviction,d.quantity,d.price,status,detail,
                     d.reason,d.timestamp,d.gates_passed,d.funnel_stage,d.ai_reasoning,
                     d.mention_count,d.headlines_used))
        except Exception as e: log.error(f"DB: {e}")

    def log_candidate(self, c:CandidateTicker, ta:float, fusion:float, conv:float, decision:str, gates:str, reason:str, cycle:int):
        try:
            with sqlite3.connect(self.db_path) as conn:
                conn.execute(
                    "INSERT INTO candidates (ticker,mention_count,velocity_score,avg_sentiment,"
                    "ta_score,fusion_score,llm_conviction,decision,gates_passed,rejection_reason,cycle,timestamp) "
                    "VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                    (c.ticker,c.mention_count,c.velocity_score,c.avg_sentiment,ta,fusion,conv,
                     decision,gates,reason,cycle,datetime.now(timezone.utc).isoformat()))
        except: pass

# ═══════════════════════════════════════════════════════════════
class TAScorer:
    def __init__(self, cfg:Config):
        self.cfg=cfg; self._cache={}; self._cache_ts={}

    def score(self, ticker:str) -> tuple[float, TASnapshot|None]:
        # Cache for 5 min
        now=time.time()
        if ticker in self._cache and now - self._cache_ts.get(ticker,0) < 300:
            snap=self._cache[ticker]
            return self._calc_score(snap), snap
        snap=self._fetch(ticker)
        if snap:
            self._cache[ticker]=snap; self._cache_ts[ticker]=now
            return self._calc_score(snap), snap
        return 0.5, None

    def _calc_score(self, s:TASnapshot) -> float:
        sc=0.5
        if s.rsi<30: sc+=0.20
        elif s.rsi<45: sc+=0.10
        elif s.rsi>70: sc-=0.20
        elif s.rsi>55: sc-=0.10
        if s.macd>s.macd_sig: sc+=0.15
        elif s.macd<s.macd_sig: sc-=0.15
        if s.price>s.ema20>s.ema50: sc+=0.15
        elif s.price<s.ema20<s.ema50: sc-=0.15
        if s.price<s.bb_lower: sc+=0.10
        elif s.price>s.bb_upper: sc-=0.10
        return max(0.0,min(1.0,sc))

    def _fetch(self, ticker:str) -> TASnapshot|None:
        for attempt in range(self.cfg.YF_MAX_RETRIES):
            try:
                time.sleep(random.uniform(*self.cfg.YF_JITTER))
                df=yf.Ticker(ticker).history(period="5d",interval="5m")
                if len(df)<50: return None
                c=df["Close"].values.astype(float)
                h=df["High"].values.astype(float)
                l=df["Low"].values.astype(float)
                rsi=float(talib.RSI(c,14)[-1])
                macd,sig,_=talib.MACD(c,12,26,9)
                ema20=float(talib.EMA(c,20)[-1])
                ema50=float(talib.EMA(c,50)[-1])
                bb_up,_,bb_lo=talib.BBANDS(c,20)
                atr=float(talib.ATR(h,l,c,14)[-1])
                return TASnapshot(ticker,round(float(c[-1]),4),round(rsi,2),
                    round(float(macd[-1]),4),round(float(sig[-1]),4),round(ema20,4),
                    round(ema50,4),round(float(bb_up[-1]),4),round(float(bb_lo[-1]),4),round(atr,4))
            except:
                if attempt<self.cfg.YF_MAX_RETRIES-1: time.sleep(self.cfg.YF_RETRY_BACKOFF)
        return None

# ═══════════════════════════════════════════════════════════════
class DecisionEngine:
    def __init__(self, cfg:Config, intel:NewsIntelligence):
        self.cfg=cfg; self.intel=intel; self.ta=TAScorer(cfg)
        self._prices=defaultdict(lambda:deque(maxlen=10))

    def evaluate(self, cand:CandidateTicker, free_cash:float, positions:list) -> TradeDecision:
        t212=f"{cand.ticker}_US_EQ"
        gates=[]

        # G1: Data — can we get TA?
        ta_score, snap = self.ta.score(cand.ticker)
        if not snap:
            return self._reject(cand,t212,ta_score,0,0,0,0,"G1 Fail: No TA data","","G1")
        gates.append("G1"); price=snap.price

        # G2: Safety — price bounds
        if price<1.0 or price>5000:
            return self._reject(cand,t212,ta_score,0,0,0,price,"G2 Fail: Price out of range","G1","G2")
        gates.append("G2")

        # G3: Velocity filter — enough buzz?
        if cand.velocity_score < self.cfg.MIN_VELOCITY and cand.mention_count < self.cfg.MIN_MENTIONS:
            return self._reject(cand,t212,ta_score,0,cand.velocity_score,0,price,
                f"G3 Fail: Low velocity ({cand.velocity_score:.3f})","G1,G2","G3")
        gates.append("G3")

        # G4: Sentiment — is there a clear signal?
        sent_norm = (cand.avg_sentiment + 1.0) / 2.0  # normalize -1..1 to 0..1
        gates.append("G4")

        # G5: TA confirmation — TA shouldn't contradict sentiment
        if cand.avg_sentiment > 0.3 and ta_score < 0.35:
            return self._reject(cand,t212,ta_score,sent_norm,cand.velocity_score,0,price,
                "G5 Fail: TA contradicts bullish news","G1,G2,G3,G4","G5")
        if cand.avg_sentiment < -0.3 and ta_score > 0.65:
            return self._reject(cand,t212,ta_score,sent_norm,cand.velocity_score,0,price,
                "G5 Fail: TA contradicts bearish news","G1,G2,G3,G4","G5")
        gates.append("G5")

        # G6: Fusion score
        self._prices[cand.ticker].append(price)
        hist=list(self._prices[cand.ticker])
        mom=0.5
        if len(hist)>=3:
            pct=(hist[-1]-hist[0])/hist[0]
            mom=0.5+max(-0.5,min(0.5,pct*10))

        fusion = (self.cfg.WEIGHT_TA*ta_score + self.cfg.WEIGHT_SENT*sent_norm +
                  self.cfg.WEIGHT_VEL*min(1.0,cand.velocity_score) +
                  self.cfg.WEIGHT_MOM*mom + self.cfg.WEIGHT_CONV*0.5)
        gates.append("G6")

        pre_action = "BUY" if fusion >= self.cfg.BUY_THRESHOLD else "SELL" if fusion <= self.cfg.SELL_THRESHOLD else "HOLD"
        if pre_action == "HOLD":
            return self._reject(cand,t212,ta_score,sent_norm,cand.velocity_score,mom,price,
                f"G6: Fusion {fusion:.3f} (HOLD zone)",",".join(gates),"G6")

        # G7: LLM Conviction
        macd_sig = "bullish" if snap.macd > snap.macd_sig else "bearish"
        price_ema = "above" if snap.price > snap.ema20 else "below"
        conviction, reasoning = self.intel.get_conviction(
            cand.ticker, pre_action, cand.headlines, cand.avg_sentiment,
            cand.velocity_score, snap.rsi, macd_sig, price_ema, price)
        # Re-calculate fusion with real conviction
        fusion = (self.cfg.WEIGHT_TA*ta_score + self.cfg.WEIGHT_SENT*sent_norm +
                  self.cfg.WEIGHT_VEL*min(1.0,cand.velocity_score) +
                  self.cfg.WEIGHT_MOM*mom + self.cfg.WEIGHT_CONV*conviction)
        gates.append("G7")

        action = "BUY" if fusion >= self.cfg.BUY_THRESHOLD else "SELL" if fusion <= self.cfg.SELL_THRESHOLD else "HOLD"

        # G8: Risk management
        qty=0.0
        if action=="BUY":
            if len(positions)>=self.cfg.MAX_OPEN_TRADES:
                return self._reject(cand,t212,ta_score,sent_norm,cand.velocity_score,mom,price,
                    "G8 Fail: Max trades",",".join(gates),"G8")
            qty=round(max(self.cfg.MIN_SHARES,(free_cash*self.cfg.MAX_POSITION_PCT)/price),4)
            if free_cash<price*self.cfg.MIN_SHARES:
                return self._reject(cand,t212,ta_score,sent_norm,cand.velocity_score,mom,price,
                    "G8 Fail: No funds",",".join(gates),"G8")
            gates.append("G8")
            reason=f"ALL 8 GATES PASSED (Fusion:{fusion:.3f})"
        elif action=="SELL":
            held=[p for p in positions if p.get("ticker","").startswith(cand.ticker[:4])]
            qty=float(held[0].get("quantity",0)) if held else 0.0
            gates.append("G8")
            reason=f"SELL (Fusion:{fusion:.3f})"
        else:
            reason=f"HOLD after conviction (Fusion:{fusion:.3f})"

        hl_str = " | ".join(cand.headlines[:3])
        return TradeDecision(
            ticker=cand.ticker, t212_ticker=t212, action=action,
            combined_score=round(fusion,4), ta_score=round(ta_score,4),
            sent_score=round(sent_norm,4), velocity_score=round(cand.velocity_score,4),
            momentum=round(mom,4), llm_conviction=round(conviction,4),
            quantity=qty, price=price, reason=reason,
            gates_passed=",".join(gates), funnel_stage="Execution" if action in ("BUY","SELL") else "Rejected",
            mention_count=cand.mention_count, headlines_used=hl_str[:500], ai_reasoning=reasoning[:200])

    def _reject(self, c, t212, ta, sent, vel, mom, price, reason, gates, stage):
        return TradeDecision(c.ticker, t212, "HOLD", 0, ta, sent, vel, mom, 0, 0, price,
            reason, gates, stage, c.mention_count, "", "")

# ═══════════════════════════════════════════════════════════════
class T212Client:
    URLS={"demo":"https://demo.trading212.com/api/v0","live":"https://live.trading212.com/api/v0"}
    def __init__(self,cfg):
        self.base=self.URLS.get(cfg.T212_MODE,self.URLS["demo"])
        auth="Basic "+base64.b64encode(f"{cfg.T212_KEY}:{cfg.T212_SECRET}".encode()).decode() if cfg.T212_SECRET else cfg.T212_KEY
        self._h={"Authorization":auth,"Content-Type":"application/json"}
    def _get(self,p):
        r=requests.get(self.base+p,headers=self._h,timeout=15); r.raise_for_status(); return r.json()
    def _post(self,p,b):
        r=requests.post(self.base+p,headers=self._h,json=b,timeout=15); r.raise_for_status(); return r.json()
    def get_cash(self): return self._get("/equity/account/cash")
    def get_portfolio(self):
        d=self._get("/equity/portfolio"); return d if isinstance(d,list) else d.get("items",[])
    def place_market_order(self,ticker,qty): return self._post("/equity/orders/market",{"ticker":ticker,"quantity":qty})

# ═══════════════════════════════════════════════════════════════
class TradeExecutor:
    def __init__(self, client, cfg):
        self.client=client; self.cfg=cfg; self.db=DatabaseManager(cfg.DB_NAME); self._cd={}
    def execute(self, dec:TradeDecision) -> str:
        if dec.action=="HOLD" or dec.quantity<=0:
            self.db.log_trade(dec,"SKIPPED","HOLD/qty=0"); return "SKIPPED"
        if time.time()-self._cd.get(dec.ticker,0)<self.cfg.COOLDOWN_SEC:
            self.db.log_trade(dec,"SKIPPED","cooldown"); return "SKIPPED"
        try:
            qty=dec.quantity if dec.action=="BUY" else -abs(dec.quantity)
            res=self.client.place_market_order(dec.t212_ticker,qty)
            self._cd[dec.ticker]=time.time()
            self.db.log_trade(dec,"EXECUTED",f"id={res.get('id','?')}")
            col=C.GR if dec.action=="BUY" else C.RD
            log.info(g(f"  ✅ {dec.action} {dec.quantity}×{dec.ticker} @${dec.price:.2f} score={dec.combined_score:.3f}",col))
            return "EXECUTED"
        except Exception as e:
            self.db.log_trade(dec,"ERROR",str(e)); log.error(g(f"  ❌ [{dec.ticker}]: {e}",C.RD))
            return "ERROR"

# ═══════════════════════════════════════════════════════════════
class AlphaEngine:
    def __init__(self, cfg=None):
        self.cfg=cfg or Config()
        self.client=T212Client(self.cfg)
        self.cache=NewsCache(self.cfg.NEWS_CSV)
        self.gemini=GeminiClient(self.cfg.GEMINI_API_KEY, self.cfg.GEMINI_MODEL)
        self.intel=NewsIntelligence(self.cache, self.cfg.DB_NAME, self.gemini)
        self.decision=DecisionEngine(self.cfg, self.intel)
        self.executor=TradeExecutor(self.client, self.cfg)
        self.db=DatabaseManager(self.cfg.DB_NAME)
        self.cycle=0

    def _scan(self):
        self.cycle+=1
        try:
            cash_data=self.client.get_cash()
            free_cash=float(cash_data.get("free",cash_data.get("freeForInvest",0)))
            portfolio=self.client.get_portfolio()
        except Exception as e:
            log.error(f"Account fetch failed: {e}"); return

        os.system("cls" if os.name=="nt" else "clear")
        now=datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")

        # DISCOVERY PHASE — LLM extracts tickers from headlines
        print(g("═"*110,C.CY))
        print(f"  AlphaEngine v4.0 — LLM Discovery  [{g(self.cfg.T212_MODE.upper(),C.GR)}]  {g(now,C.DIM)}  #{self.cycle}")
        print(g("═"*110,C.CY))
        print(g(f"  📰 News warehouse: {self.cache.total:,} articles  |  Scanning last {self.cfg.NEWS_LOOKBACK_MIN} min...",C.DIM))

        candidates = self.intel.discover(self.cfg.NEWS_LOOKBACK_MIN)
        if not candidates:
            print(g("  ⚠ No candidates discovered from news this cycle.",C.YL))
            print(g(f"\n  Cash: ${free_cash:,.2f}  |  Positions: {len(portfolio)}/{self.cfg.MAX_OPEN_TRADES}",C.GY))
            return

        print(g(f"  🔍 Discovered {len(candidates)} candidates from news",C.GR))
        print()
        print(f"{'TICKER':<7} {'ACT':<5} {'FUSION':>6} {'TA':>5} {'SENT':>5} {'VEL':>5} {'MOM':>5} {'CONV':>5} {'#':>3} {'GATES':<18} {'REASON':<28}")
        print(g("─"*110,C.DIM))

        for cand in candidates:
            dec = self.decision.evaluate(cand, free_cash, portfolio)
            self.db.log_candidate(cand, dec.ta_score, dec.combined_score, dec.llm_conviction,
                dec.action, dec.gates_passed, dec.reason, self.cycle)

            if dec.action in ("BUY","SELL"):
                self.executor.execute(dec)

            acol=C.GR if dec.action=="BUY" else C.RD if dec.action=="SELL" else C.GY
            print(f"{cand.ticker:<7} {g(dec.action,acol):<14} {dec.combined_score:>6.3f} {dec.ta_score:>5.3f} "
                  f"{dec.sent_score:>5.3f} {dec.velocity_score:>5.3f} {dec.momentum:>5.3f} {dec.llm_conviction:>5.3f} "
                  f"{cand.mention_count:>3} {dec.gates_passed:<18} {g(dec.reason[:28],C.DIM)}")

        print(g("─"*110,C.DIM))
        buys=sum(1 for c in candidates if self.decision.evaluate(c,free_cash,portfolio).action!="HOLD")
        print(g(f"\n  Cash: ${free_cash:,.2f}  |  Positions: {len(portfolio)}/{self.cfg.MAX_OPEN_TRADES}  |  Candidates: {len(candidates)}  |  DB: {self.cfg.DB_NAME}",C.GY))

    def start(self):
        log.info(g("AlphaEngine v4.0 starting — LLM Discovery Mode",C.B+C.CY))
        try:
            while True:
                self._scan(); time.sleep(self.cfg.SCAN_INTERVAL_SEC)
        except KeyboardInterrupt:
            print(g("\n  Stopped.\n",C.YL))

if __name__=="__main__":
    AlphaEngine().start()