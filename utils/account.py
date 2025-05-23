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
        """
        Returns the free balance of a specific asset from Binance account info.

        Parameters:
        - asset (str): Asset symbol, e.g., "BTC" or "EUR"

        Returns:
        - float: The free balance of the given asset, or 0.0 if not found or on error.
        """
        try:
            account_info = self.client.account()
            for balance in account_info["balances"]:
                if balance["asset"] == asset:
                    return float(balance["free"])
        except Exception as e:
            print("Fehler beim Abrufen des Kontostands:", e)
        
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
