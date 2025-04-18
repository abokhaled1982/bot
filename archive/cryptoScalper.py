## === run() und Einstiegspunkt in scalper.py === ##

from datetime import datetime
from utils.strategy import TradeStrategy
from utils.orders import OrderManager
from utils.account import AccountManager
from utils.telegram import TelegramNotifier
from utils.log import Logger, Stats
from utils.helper import calculate_ema, calculate_rsi, fetch_candles, load_config
from coinbase.rest import RESTClient

import argparse
import time

class CryptoScalper:
    def __init__(self, config, dry_run=False):
        client = RESTClient(
            api_key=config["cdp"]["api_key"],
            api_secret=config["cdp"]["api_secret"]
        )

        self.logger = Logger()
        self.account = AccountManager(client, self.logger)
        self.orders = OrderManager(client, self.logger, dry_run=dry_run)
        self.strategy = TradeStrategy()

        self.in_position = False
        self.entry_price = None
        self.trades = 0
        self.wins = 0
        self.losses = 0
        self.latest_ema=0
        self.tp_pct =self.strategy.tp_pct
        self.sl_pct = self.strategy.sl_pct

        self.telegram = TelegramNotifier(
            token=config["telegramm"]["token"],
            chat_id=config["telegramm"]["chat_id"],
            bot=self
        )
        self.logger.telegram = self.telegram  # optional für Push

        self.stats = Stats(self.logger, self.account.get_balance, bot=self)
        self.client = client
        self.product_id = "BTC-EUR"

    def count_trade_result(self, exit_type):
        self.trades += 1
        if exit_type == "TP":
            self.wins += 1
        elif exit_type == "SL":
            self.losses += 1

    def run(self, interval=60):
        self.logger.log("🚀 Starte RSI+EMA Scalping Bot...")

        while True:
            try:
                self.telegram.check_for_commands()
                eur, btc = self.account.get_balances()
                candles = fetch_candles(self.client, self.product_id)
                if not candles:
                    time.sleep(interval)
                    continue

                closes = [c["close"] for c in candles]
                current_price = closes[-1]
                rsi = calculate_rsi(closes, window=2)
                ema = calculate_ema(closes, window=20)

                if rsi is None or ema is None:
                    time.sleep(interval)
                    continue

                if not self.in_position: 
                    #not in postion and hold btc
                    if not self.in_position and btc:
                       self.in_position=True 
                       self.entry_price= current_price                  
                    should_enter, reason = self.strategy.should_enter(
                        self.in_position, rsi, current_price, ema, eur
                    )
                    self.logger.log(reason)
                    if should_enter and self.orders.place_market_buy(eur):
                        self.in_position = True
                        self.entry_price = current_price
                        self.logger.log(f"💸 Gekauft zu: {current_price:.2f} EUR")
                else:
                    should_exit, exit_type, reason = self.strategy.should_exit(
                        self.in_position, current_price, self.entry_price
                    )
                    self.logger.log(reason)
                    if should_exit and self.orders.place_market_sell(btc):
                        self.in_position = False
                        self.entry_price = None
                        self.count_trade_result(exit_type)
                        self.logger.log(f"💼 Position geschlossen per {exit_type}")

                self.stats.print_stats(current_price)

            except Exception as e:
                self.logger.log(f"Fehler im Hauptloop: {e}")

            time.sleep(interval)

if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--dry-run", action="store_true", help="Simuliere Trades ohne echte Orders")
    args = parser.parse_args()

    config = load_config()
    bot = CryptoScalper(config=config, dry_run=args.dry_run)
    bot.run()
