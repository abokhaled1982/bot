# core/trader.py

from datetime import datetime
import time
from zoneinfo import ZoneInfo
from utils.stats import TraderStats

class Trader:
    def __init__(self, strategy_engine, account, orders, logger,coin="BTC",symbol="BTCEUR",interval="5m"):
        self.strategy = strategy_engine
        self.account = account
        self.orders = orders
        self.logger = logger
        self.stats = TraderStats()
        self.in_position = False
        self.entry_price = None
        self.amount_btc = 0.0
        self.symbol = symbol  # Neu: Symbol wird gespeichert!,
        self.interval=interval
        self.coin=coin

    def update(self, market_data):
        price = market_data["close"]  
        new_btc_balance = self.account.get_balance(self.coin)        
        if not self.in_position:
            if  self.strategy.should_enter(market_data):
                eur = self.strategy.config.get("amount", 10)
                if self.account.get_balance("EUR") >= eur:
                    if self.orders.place_market_buy(eur):
                        time.sleep(1)
                        new_btc_balance = self.account.get_balance(self.coin)
                        new_amount = new_btc_balance - self.amount_btc
                        self.amount_btc = new_amount  # Nur neu gekaufte Menge merken
                        self.entry_price = market_data["close"]
                        self.in_position = True
        else:
            
            if self.strategy.should_sell(market_data):
                if self.orders.place_market_sell(self.amount_btc):
                    self.logger.log(f"📤 TP erreicht bei {price:.6f} € (+{self.strategy.config['pct']}%)")
                    self.stats.record_trade("TP")
                    self.reset()
        #logging
        self.logger.log(self.summary_line(market_data))


    def summary_line(self, market_data):
        price = market_data.get("close", 0.0)
        rsi = market_data.get("rsi", 0.0)
        ema = market_data.get("ema", 0.0)
        trend_diff = (price - ema) / ema if ema else 0
        trend = "Seitwärts"
        if trend_diff > 0.01:
            trend = "📈 Bullisch"
        elif trend_diff < -0.01:
            trend = "📉 Bärisch"

        entry_price = self.entry_price or 0.0
        tp_price = entry_price * (1 + self.strategy.target_pct) if entry_price else 0.0
       
        lines = []       
        lines.append(f"Strategie: {self.strategy.name}:")       
        lines.append(f"➔ Status: {'✅ IN Position' if self.in_position else '⏳ Wartend'}")
        lines.append(f"➔ Anzahl Trades   : {self.stats.trades}")
        lines.append(f"➔ Gewinne         : {self.stats.wins}")
        lines.append(f"➔ Verluste        : {self.stats.losses}") 
        lines.append(f"➔ Aktueller Preis : {price:.6f} EUR")

        if self.in_position:            
            lines.append(f"➔ Einstiegspreis   : {entry_price:.6f} EUR")
            lines.append(f"➔ Zielpreis        : {tp_price:.6f} EUR")
             
             
        lines.append(f"➔ RSI             : {rsi:.2f}")  
        lines.append(f"➔ Markttrend      : {trend} ({trend_diff * 100:.2f}%)")
        lines.append(f"➔ EMA(20)         : {ema:.2f} EUR")
        return "\n".join(lines)
    
    

    def reset(self):
        self.logger.log("🔀 Warte auf neues Einstiegssignal")
        self.in_position = False
        self.entry_price = None
        self.amount_btc = 0.0
