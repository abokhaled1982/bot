# utils/orders.py
import decimal
import time


class OrderManager:
    def __init__(self, client, logger, symbole="BTCEUR", dry_run=False):
        self.client = client
        self.logger = logger
        self.symbole = symbole
        self.dry_run = dry_run
    
    

    def place_market_buy(self, amount_eur, retries=3, delay=1):
        if amount_eur <= 0:
            self.logger.log("❌ Kein EUR-Guthaben für Kauf.")
            return False

        if self.dry_run:
            self.logger.log(f"[DRY-RUN] ❕ Kauf simuliert für {amount_eur:.2f} EUR")
            return True

        for attempt in range(1, retries + 1):
            try:
                amount = decimal.Decimal(amount_eur).quantize(decimal.Decimal("0"), rounding=decimal.ROUND_DOWN)               
                order = self.client.new_order(
                symbol=self.symbole,
                side='BUY',
                type='MARKET',
                quoteOrderQty=amount
                    )
                self.logger.log(f"✅ Kauf erfolgreich (Versuch {attempt}): {order}")
                return True
            except Exception as e:
                self.logger.log(f"⚠️ Fehler bei Kauf (Versuch {attempt}): {e}")
                time.sleep(delay)
        return False

    def place_market_sell(self, amount_btc, retries=3, delay=1):
        if amount_btc <= 0:
            self.logger.log("❌ Kein BTC-Guthaben für Verkauf.")
            return False

        if self.dry_run:
            self.logger.log(f"[DRY-RUN] ❕ Verkauf simuliert für {amount_btc:.8f} BTC")
            return True

        try:
            # 📘 Symbol info & filters
            exchange_info = self.client.exchange_info()
            symbol_info = next(s for s in exchange_info['symbols'] if s['symbol'] == self.symbole)
            lot_size = next(f for f in symbol_info['filters'] if f['filterType'] == 'LOT_SIZE')
            price_filter = next(f for f in symbol_info['filters'] if f['filterType'] == 'PRICE_FILTER')

            step_size = decimal.Decimal(lot_size['stepSize'])
            min_qty = decimal.Decimal(lot_size['minQty'])
            tick_size = decimal.Decimal(price_filter['tickSize'])

            # 🧮 Round quantity
            qty = decimal.Decimal(str(amount_btc))
            adjusted_qty = (qty // step_size) * step_size

            if adjusted_qty < min_qty:
                self.logger.log(f"❌ Menge {adjusted_qty} BTC ist kleiner als das Mindestmaß {min_qty} BTC.")
                return False

            # 📈 Aktuellen Marktpreis abrufen
            ticker = self.client.ticker_price(symbol=self.symbole)
            market_price = decimal.Decimal(ticker['price'])

            # Runde Preis passend zu tickSize
            adjusted_price = (market_price // tick_size) * tick_size

            # 🧾 Order ausführen
            for attempt in range(1, retries + 1):
                try:
                    order = self.client.new_order(
                        symbol=self.symbole,
                        side='SELL',
                        type='LIMIT',
                        timeInForce='GTC',
                        quantity=str(adjusted_qty),
                        price=str(adjusted_price)
                    )
                    self.logger.log(f"✅ Verkauf erfolgreich (Versuch {attempt}): Order-ID {order['orderId']}")
                    return True
                except Exception as e:
                    self.logger.log(f"⚠️ Fehler bei Verkauf (Versuch {attempt}): {e}")
                    time.sleep(delay)

        except Exception as e:
            self.logger.log(f"❌ Fehler beim Vorbereiten des Verkaufs: {e}")
        return False
