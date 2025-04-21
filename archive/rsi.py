# scalper.py

import argparse
import decimal
import time
import uuid
import traceback
from datetime import datetime, timedelta
from utils.helper import load_config
from utils.log import Logger, Stats
import pandas as pd
from ta.momentum import RSIIndicator
from ta.trend import EMAIndicator


from utils.telegram import TelegramNotifier


class CryptoScalper:

    def __init__(self, config, product_id="BTC-EUR", rsi_period=2, entry_rsi=10, tp_pct=0.01, sl_pct=-0.005, dry_run=False):
        # === Coinbase DCP Config ===
              
        api_key = config["cdp"]["api_key"]
        api_secret = config["cdp"]["api_secret"]
        self.client = RESTClient(api_key=api_key, api_secret=api_secret)      

        self.product_id = product_id
        self.rsi_period = rsi_period
        self.entry_rsi = entry_rsi
        self.tp_pct = tp_pct
        self.sl_pct = sl_pct

        self.in_position = False
        self.entry_price = None
        self.entry_time = None
        self._eur_balance = 0.0
        self._btc_balance = 0.0
        self._last_balance_update = datetime.min
        self._balance_cache_duration = timedelta(seconds=300)  # z. B. 1,5 Minuten


        self.trades = 0
        self.wins = 0
        self.losses = 0

        self.latest_ema = None  # Wird im Hauptloop aktualisiert
        self.dry_run = dry_run  # <== NEU

              # === Telegram Setup ===
        telegram_config = config["telegramm"]
        self.telegram = TelegramNotifier(
            token=telegram_config["token"],
            chat_id=telegram_config["chat_id"],
            bot=self  # gibt dem Telegram-Handler Zugriff auf diesen Bot
        )

        self.logger = Logger(self.telegram)
        self.stats = Stats(self.logger, self.get_eur_balance, bot=self)
   
    # ==== Datenabruf ====
    def fetch_price(self):
        """Aktuellen Preis vom Coinbase-Ticker holen."""
        try:
            ticker = self.client.get_product(product_id=self.product_id)
            return float(ticker['price'])
        except Exception as e:
            self.logger.log(f"Fehler beim Abrufen des Preises: {e}")
            
            return None

    def fetch_candles(self, granularity="ONE_MINUTE", limit=100):
        """Lädt historische Kerzen vom Coinbase-Server."""
        try:
            end_dt = datetime.utcnow()
            start_dt = end_dt - timedelta(minutes=limit)

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
            self.logger.log(f"Fehler beim Laden der Kerzen: {e}")
            return None

    # ==== Indikatoren ====
    def calculate_rsi(self, df):
        """Berechnet den RSI basierend auf Schlusskursen."""
        try:
            closes = [c["close"] for c in df]
            rsi_series = RSIIndicator(close=pd.Series(closes), window=self.rsi_period).rsi()
            return rsi_series.iloc[-1]
        except Exception as e:
            self.logger.log(f"Fehler beim RSI-Berechnen: {e}")
            return None

    def calculate_ema(self, df, window=20):
        """Berechnet die EMA basierend auf Schlusskursen."""
        try:
            closes = [c["close"] for c in df]
            ema_series = EMAIndicator(close=pd.Series(closes), window=window).ema_indicator()
            return ema_series.iloc[-1]
        except Exception as e:
            self.logger.log(f"Fehler beim EMA-Berechnen: {e}")
            return None

    # ==== Kontoverwaltung ====
    def get_eur_balance(self):
        """Ruft das verfügbare EUR-Guthaben ab."""
        try:
            accounts = self.client.get_accounts()
            for account in accounts['accounts']:
                if account['currency'] == 'EUR':
                    return float(account['available_balance']['value'])
        except:
            return 0.0
        return 0.0

    def get_btc_balance(self):
        """Ruft das verfügbare BTC-Guthaben ab."""
        try:
            accounts = self.client.get_accounts()
            for account in accounts['accounts']:
                if account['currency'] == 'BTC':
                    return float(account['available_balance']['value'])
        except:
            return 0.0
        return 0.0

    def get_cached_balances(self):
        """Gibt gecachte Balances zurück, wenn gültig. Holt neue falls nötig."""
        now = datetime.utcnow()
        if now - self._last_balance_update > self._balance_cache_duration:
            try:
                accounts = self.client.get_accounts()
                for acc in accounts['accounts']:
                    if acc['currency'] == 'EUR':
                        self._eur_balance = float(acc['available_balance']['value'])
                    elif acc['currency'] == 'BTC':
                        self._btc_balance = float(acc['available_balance']['value'])
                self._last_balance_update = now
                self.logger.log("🔄 Balances aktualisiert.")
            except Exception as e:
                self.logger.log(f"⚠️ Fehler beim Laden der Kontostände: {e}")
        return self._eur_balance, self._btc_balance

    # ==== Orders ====
    def place_market_buy(self, amount_eur, retries=3, delay=1):
        if amount_eur <= 0:
            self.logger.log("❌ Kein EUR-Guthaben für Kauf.")
            return False

        if self.dry_run:
            self.logger.log(f"[DRY-RUN] ❕ Kauf simuliert für {amount_eur:.2f} EUR")
            return True

        for attempt in range(1, retries + 1):
            try:
                amount_eur = decimal.Decimal(amount_eur).quantize(decimal.Decimal('0'), rounding=decimal.ROUND_DOWN)
                order_config = {"market_market_ioc": {"quote_size": str(amount_eur)}}
                response = self.client.create_order(
                    client_order_id=str(uuid.uuid4()),
                    product_id=self.product_id,
                    side="BUY",
                    order_configuration=order_config
                )
                self.logger.log(f"✅ Kauf erfolgreich (Versuch {attempt}): {response['order_id']}")
                return True
            except Exception as e:
                self.logger.log(f"⚠️ Fehler bei Kauf-Order (Versuch {attempt}): {e}")
                time.sleep(delay)

        self.logger.log("❌ Kauf fehlgeschlagen nach mehreren Versuchen.")
        return False

    def place_market_sell(self, size_btc, retries=3, delay=5):
        if size_btc <= 0:
            self.logger.log("❌ Kein BTC-Guthaben für Verkauf.")
            return False

        if self.dry_run:
            self.logger.log(f"[DRY-RUN] ❕ Verkauf simuliert für {size_btc:.8f} BTC")
            return True

        for attempt in range(1, retries + 1):
            try:
                order = self.client.create_order(
                    client_order_id=str(uuid.uuid4()),
                    product_id=self.product_id,
                    side="SELL",
                    order_configuration={"market_market_ioc": {"base_size": str(size_btc)}}
                )
                self.logger.log(f"✅ Verkauf erfolgreich (Versuch {attempt}): {order['order_id']}")
                return True
            except Exception as e:
                self.logger.log(f"⚠️ Fehler bei Verkauf-Order (Versuch {attempt}): {e}")
                time.sleep(delay)

        self.logger.log("❌ Verkauf fehlgeschlagen nach mehreren Versuchen.")
        return False

    # ==== Entry & Exit ====
    def check_entry(self, rsi, current_price, ema, eur_balance):
        """Überprüft Einstiegskriterien und kauft ggf."""
        if not self.in_position and rsi < self.entry_rsi and eur_balance > 0:
            self.logger.log(f"✅ ENTRY – RSI: {rsi:.2f}, Kurs: {current_price:.2f} > EMA({ema:.2f})")
            if self.place_market_buy(eur_balance):
                self.entry_price = current_price
                self.entry_time = datetime.utcnow()
                self.in_position = True
                self.logger.log(f"💸 Gekauft zu: {self.entry_price:.2f} EUR")
        elif rsi >10:
            self.logger.log(f"❌ Kein Einstieg – Kurs über RSI ({rsi:.2f})")
        elif current_price <= ema:
            self.logger.log(f"❌ Kein Einstieg – Kurs unter EMA ({ema:.2f})")
        elif eur_balance <= 0:
            self.logger.log("⚠️ Kein EUR-Guthaben für Einstieg.")

    def check_exit(self, current_price, ema, btc_balance):
        """Verkauft bei TP, SL oder Trendbruch."""
        if self.in_position and btc_balance > 0:
            change_pct = (current_price - self.entry_price) / self.entry_price

            # Take Profit
            if change_pct >= self.tp_pct:
                self.logger.log(f"[TP] Ziel erreicht ({change_pct*100:.2f}%) – verkaufe...")
                if self.place_market_sell(btc_balance):
                    self.trades += 1
                    self.wins += 1
                    self.in_position = False
                    return

            # EMA-Sell
            # if ema and current_price < ema:
            #     self.logger.log(f"[EMA-SELL] Preis unter EMA(20) ({current_price:.2f} < {ema:.2f}) – verkaufe...")
            #     if self.place_market_sell(btc_balance):
            #         self.trades += 1
            #         self.losses += 1
            #         self.in_position = False
            #         return

            # Stop Loss
            if change_pct <= self.sl_pct:
                self.logger.log(f"[SL] Verlustgrenze erreicht ({change_pct*100:.2f}%) – verkaufe...")
                if self.place_market_sell(btc_balance):
                    self.trades += 1
                    self.losses += 1
                    self.in_position = False

    def run(self, interval=60):   
        """Hauptloop des Bots."""
        self.logger.log("🚀 Starte RSI+EMA Scalping Bot...")

        while True:
            try:
                self.telegram.check_for_commands()
                eur_balance, btc_balance = self.get_cached_balances()
                self.balance = eur_balance  # falls du self.balance noch brauchst

                candles = self.fetch_candles(limit=100)
                if not candles:
                    time.sleep(interval)
                    continue

                current_price = self.fetch_price()
                if current_price is None:
                    time.sleep(interval)
                    continue

                rsi = self.calculate_rsi(candles)
                ema = self.calculate_ema(candles, window=20)
                self.latest_ema = ema  # für Stat-Anzeige

                if rsi is None or ema is None:
                    time.sleep(interval)
                    continue

                # Optional: Nur alle x Minuten Kontostände aktualisieren?
                self.balance = self.get_eur_balance()
                btc_balance = self.get_btc_balance()

                if not self.in_position and btc_balance > 0:
                    self.entry_price = current_price
                    self.entry_time = datetime.utcnow()
                    self.in_position = True
                    self.logger.log("⚠️ BTC erkannt – Verkaufskontrolle aktiv.")

                if not self.in_position:
                    self.check_entry(rsi, current_price, ema, eur_balance)
                else:
                    self.check_exit(current_price, ema, btc_balance)


                self.stats.print_stats(current_price)

            except Exception as e:
                self.logger.log(f"Fehler im Hauptloop: {e}")
                traceback.print_exc()

            time.sleep(interval)

if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--dry-run", action="store_true", help="Simuliere Trades ohne echte Orders")
    args = parser.parse_args()
    print(args)
    config = load_config()
    bot = CryptoScalper(config=config,dry_run=args.dry_run)
    bot.run()
