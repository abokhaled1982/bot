# utils/account.py
from datetime import datetime, timedelta

class AccountManager:
    def __init__(self, client, logger, cache_duration_sec=90):
        self.client = client
        self.logger = logger
        self.cache_duration = timedelta(seconds=cache_duration_sec)
        self._eur = 0.0
        self._btc = 0.0
        self._last_update = datetime.min

    def get_balance(self, asset):
        try:
            account_info = self.client.account()
            balances = account_info['balances']
            # Only show non-zero balances
            non_zero = [b for b in balances if float(b['free']) > 0 or float(b['locked']) > 0]
            # Get current prices for estimation
            all_prices = {p['symbol']: float(p['price']) for p in self.client.get_all_tickers()}
            for balance in account_info["balances"]:
                if balance["asset"] == asset:
                    return float(balance["free"])
        except Exception as e:
            print("Fehler beim Abrufen des Kontostands:", e)
            return 0.0
        return 0.0

    def get_balances(self):
        now = datetime.utcnow()
        if now - self._last_update > self.cache_duration:
            try:
                accounts = self.client.get_accounts()
                for acc in accounts["accounts"]:
                    if acc["currency"] == "EUR":
                        self._eur = float(acc["available_balance"]["value"])
                    elif acc["currency"] == "BTC":
                        self._btc = float(acc["available_balance"]["value"])
                self._last_update = now
                self.logger.log("🔄 Balances aktualisiert.")
            except Exception as e:
                self.logger.log(f"⚠️ Fehler beim Balance-Update: {e}")
        return self._eur, self._btc
