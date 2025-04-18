import time
import pandas as pd
import ta
from coinbase.rest import RESTClient
from datetime import datetime, timedelta
import traceback
from ta.momentum import RSIIndicator
import uuid

class CryptoScalper:
    def __init__(self, product_id="BTC-EUR", rsi_period=2, entry_rsi=10, tp_pct=0.01, sl_pct=-0.005):
        self.client = RESTClient(key_file="./cdp_api_key.json")
        self.product_id = product_id
        self.rsi_period = rsi_period
        self.entry_rsi = entry_rsi
        self.tp_pct = tp_pct
        self.sl_pct = sl_pct
        self.in_position = False
        self.entry_price = None
        self.entry_time = None
        self.balance = self.get_eur_balance()

        self.trades = 0
        self.wins = 0
        self.losses = 0

    def log(self, message: str):
        """Zentrale Logging-Funktion mit MEZ-Zeit"""
        now_local = datetime.utcnow()
        print(f"[{now_local.strftime('%d.%m.%Y %H:%M:%S')}] {message}")

    def print_stats(self, current_price):
        self.balance = self.get_eur_balance()
        self.log("📊 Trade-Statistiken:")
        self.log(f"   ➤ Anzahl Trades: {self.trades}")
        self.log(f"   ➤ Gewinne      : {self.wins}")
        self.log(f"   ➤ Verluste     : {self.losses}")
        self.log(f"   💰 Kontostand    : {self.balance:.2f} EUR")
        self.log(f"   📈 Aktueller Preis: {current_price:.2f} EUR")

        if self.in_position and self.entry_price:
            tp_target = self.entry_price * (1 + self.tp_pct)
            self.log(f"   💸 Einstiegspreis: {self.entry_price:.2f} EUR")
            self.log(f"   🎯 Zielpreis     : {tp_target:.2f} EUR")

    def fetch_price(self):
        try:
            ticker = self.client.get_product(product_id=self.product_id)
            return float(ticker['price'])
        except Exception as e:
            self.log(f"Fehler beim Abrufen des Preises: {e}")
            return None

    def fetch_candles(self, granularity="ONE_MINUTE", limit=100):
        try:
            end_dt = datetime.utcnow()
            start_dt = end_dt - timedelta(minutes=limit)
            self.log(f"Hole Kerzen von {start_dt.astimezone()} bis {end_dt.astimezone()}")

            response = self.client.get_candles(
                product_id=self.product_id,
                start=int(start_dt.timestamp()),
                end=int(end_dt.timestamp()),
                granularity=granularity,
                limit=limit
            )

            candles = [
                {
                    "start": datetime.utcfromtimestamp(int(c["start"])),
                    "low": float(c["low"]),
                    "high": float(c["high"]),
                    "open": float(c["open"]),
                    "close": float(c["close"]),
                    "volume": float(c["volume"])
                }
                for c in response.candles
            ]
            candles.sort(key=lambda x: x["start"])
            return candles
        except Exception as e:
            self.log(f"Fehler beim Laden der Kerzen: {e}")
            return None

    def calculate_rsi(self, df):
        try:
            closes = [candle["close"] for candle in df]
            rsi_series = RSIIndicator(close=pd.Series(closes), window=self.rsi_period).rsi()
            rsi_value = rsi_series.iloc[-1]
            self.log(f"📈 RSI berechnet: {rsi_value:.2f}")
            return rsi_value
        except Exception as e:
            self.log(f"Fehler beim RSI-Berechnen: {e}")
            return None

    def get_eur_balance(self):
        try:
            accounts = self.client.get_accounts()
            for account in accounts['accounts']:
                if account['currency'] == 'EUR':
                    return float(account['available_balance']['value'])
        except:
            return 0.0
        return 0.0

    def get_btc_balance(self):
        try:
            accounts = self.client.get_accounts()
            for account in accounts['accounts']:
                if account['currency'] == 'BTC':
                    return float(account['available_balance']['value'])
        except:
            return 0.0
        return 0.0

    def place_market_buy(self, amount_eur):
        if amount_eur <= 0:
            self.log("Kein EUR-Guthaben vorhanden für Kauf.")
            return False
        try:
            order_config = {
                "market_market_ioc": {
                    "quote_size": str(amount_eur)
                }
            }
            order = self.client.create_order(
                client_order_id=str(uuid.uuid4()),
                product_id=self.product_id,
                side="BUY",
                order_configuration=order_config
            )
            self.log(f"Kauf erfolgreich: {order['order_id']}")
            return True
        except Exception as e:
            self.log(f"Fehler bei Kauf-Order: {e}")
            return False

    def place_market_sell(self, size_btc):
        if size_btc <= 0:
            self.log("Kein BTC-Guthaben vorhanden für Verkauf.")
            return False
        try:
            order_config = {
                "market_market_ioc": {
                    "base_size": str(size_btc)
                }
            }
            order = self.client.create_order(
                client_order_id=str(uuid.uuid4()),
                product_id=self.product_id,
                side="SELL",
                order_configuration=order_config
            )
            self.log(f"Verkauf erfolgreich: {order['order_id']}")
            return True
        except Exception as e:
            self.log(f"Fehler bei Verkauf-Order: {e}")
            return False

    def check_entry(self, rsi, current_price):
        self.balance = self.get_eur_balance()
        if not self.in_position and rsi < self.entry_rsi and self.balance > 0:
            self.log(f"ENTRY-Signal erkannt – RSI: {rsi:.2f}")
            if self.place_market_buy(self.balance):
                self.entry_price = current_price
                self.entry_time = datetime.utcnow()
                self.in_position = True
                self.log(f"💸 Gekauft zu: {self.entry_price:.2f} EUR")
        elif self.balance <= 0:
            self.log("Nicht genug EUR-Guthaben für Einstieg.")

    def check_exit(self, current_price):
        size_btc = self.get_btc_balance()
        if self.in_position and size_btc > 0:
            change_pct = (current_price - self.entry_price) / self.entry_price

            if change_pct >= self.tp_pct:
                self.log(f"[TP] Gewinnziel erreicht ({change_pct*100:.2f}%) – verkaufe...")
                if self.place_market_sell(size_btc):
                    self.trades += 1
                    self.wins += 1
                    self.in_position = False
                    self.log("✅ Gewinn realisiert")

            elif change_pct <= self.sl_pct:
                self.log(f"[SL] Verlustgrenze erreicht ({change_pct*100:.2f}%) – verkaufe...")
                if self.place_market_sell(size_btc):
                    self.trades += 1
                    self.losses += 1
                    self.in_position = False
                    self.log("❌ Verlust realisiert")
        elif size_btc <= 0:
            self.log("Kein BTC für Verkauf vorhanden – keine Position aktiv.")

    def run(self, interval=60):
        self.log("🚀 Starte RSI Scalping Bot...")
        while True:
            try:
                candles = self.fetch_candles()
                if candles is None:
                    time.sleep(interval)
                    continue

                current_price = self.fetch_price()
                if current_price is None:
                    time.sleep(interval)
                    continue

                rsi = self.calculate_rsi(candles)
                if rsi is None:
                    time.sleep(interval)
                    continue

                if not self.in_position:
                    self.check_entry(rsi, current_price)
                else:
                    self.check_exit(current_price)

                self.print_stats(current_price)

            except Exception as e:
                self.log(f"Fehler im Loop: {e}")
                traceback.print_exc()

            time.sleep(interval)

if __name__ == "__main__":
    bot = CryptoScalper()
    bot.run()
