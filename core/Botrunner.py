# core/BotRunner.py

import time
from core.Trader import Trader  # Deine Trader-Klasse
from core.StrategyEngine import StrategyEngine
from utils.helper import calculate_ema, calculate_rsi, fetch_candles  # Die flexible Engine

class BotRunner:
    def __init__(self, client, account, orders, logger):
        self.client = client
        self.account = account
        self.orders = orders
        self.logger = logger
        self.traders = []  # Aktive Trader (jeweils 1 Position)

        # Du definierst hier, mit welchen Strategien du arbeiten willst
        self.available_strategies = [
            StrategyEngine(
                indicators=["rsi"],
                config={"rsi": 10,"pct":0.5, "amount":4}
            ),
              StrategyEngine(
                indicators=["rsi"],
                config={"rsi": 10,"pct":1,"amount":4}
            )
           
        ]

    def run(self, interval=60):
        self.logger.log("🚀 BotRunner gestartet")
        while True:
            try:

                if self.logger.telegram:
                    self.logger.telegram.check_for_commands()

                candles = fetch_candles(self.client, "BTC-EUR")
                closes = [c["close"] for c in candles]
                close = closes[-1]
                rsi = calculate_rsi(closes, 2)
                ema = calculate_ema(closes, 20)
                eur = self.account.get_balance("EUR")

                market_data = {
                    "close": close,
                    "rsi": rsi,
                    "ema": ema,
                    "eur_balance": eur,
                    "timestamp": time.time()
                }

                # Bestehende Trader updaten
                for trader in self.traders:
                    trader.update(market_data)

                # Neue Trader erzeugen, wenn Signale passen
                for strategy in self.available_strategies:
                    if strategy.should_enter(market_data):
                        trader = Trader(strategy, self.account, self.orders, self.logger)
                        self.traders.append(trader)
                        self.logger.log(f"➕ Neuer Trader gestartet mit Strategie: {strategy.name}")

               
                if self.stats:
                    self.stats.print_stats(price)

            except Exception as e:
                self.logger.log(f"❌ Fehler im BotLoop: {e}")

            time.sleep(interval)


