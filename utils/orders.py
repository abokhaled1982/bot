# utils/orders.py
import decimal
import time
import uuid

class OrderManager:
    def __init__(self, client, logger, product_id="BTC-EUR", dry_run=False):
        self.client = client
        self.logger = logger
        self.product_id = product_id
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
                order_config = {"market_market_ioc": {"quote_size": str(amount)}}
                response = self.client.create_order(
                    client_order_id=str(uuid.uuid4()),
                    product_id=self.product_id,
                    side="BUY",
                    order_configuration=order_config
                )
                self.logger.log(f"✅ Kauf erfolgreich (Versuch {attempt}): {response['order_id']}")
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

        for attempt in range(1, retries + 1):
            try:
                order_config = {"market_market_ioc": {"base_size": str(amount_btc)}}
                response = self.client.create_order(
                    client_order_id=str(uuid.uuid4()),
                    product_id=self.product_id,
                    side="SELL",
                    order_configuration=order_config
                )
                self.logger.log(f"✅ Verkauf erfolgreich (Versuch {attempt}): {response['order_id']}")
                return True
            except Exception as e:
                self.logger.log(f"⚠️ Fehler bei Verkauf (Versuch {attempt}): {e}")
                time.sleep(delay)
        return False
