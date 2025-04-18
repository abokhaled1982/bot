# utils/telegram.py

import requests
import time

class TelegramController:
    def __init__(self, token, chat_id, bot=None):
        self.token = token
        self.chat_id = chat_id
        self.bot = bot  # Zugriff auf zentrale Botklasse (z. B. BotRunner)
        self.api_url = f"https://api.telegram.org/bot{token}"
        self.last_update_id = None

    def send(self, message: str):
        """Sendet eine Nachricht an Telegram"""
        try:
            requests.post(
                f"{self.api_url}/sendMessage",
                data={"chat_id": self.chat_id, "text": message, "parse_mode": "Markdown"}
            )
        except Exception as e:
            print(f"[Telegram] Fehler beim Senden: {e}")

    def check_for_commands(self):
        """Empfängt neue Befehle und führt aus."""
        try:
            params = {'timeout': 5}
            if self.last_update_id:
                params['offset'] = self.last_update_id + 1

            res = requests.get(f"{self.api_url}/getUpdates", params=params)
            data = res.json()
            if not data.get("ok"):
                return

            for update in data["result"]:
                self.last_update_id = update["update_id"]
                message = update.get("message", {})
                text = message.get("text", "").strip().lower()

                self._handle_command(text)

        except Exception as e:
            print(f"[Telegram] Fehler beim Verarbeiten von Kommandos: {e}")

    def _handle_command(self, text):
        """Mapped Kommandos auf Aktionen im Bot"""
        if not self.bot:
            return

        if text == "/status":
            price = self.bot.fetch_price()
            self.bot.stats.print_stats(price)

        elif text == "/buy":
            eur = self.bot.account.get_balance("EUR")
            self.bot.orders.place_market_buy(eur)

        elif text == "/sell":
            btc = self.bot.account.get_balance("BTC")
            self.bot.orders.place_market_sell(btc)

        elif text == "/cancel":
            if self.bot.in_position:
                btc = self.bot.account.get_balance("BTC")
                self.bot.orders.place_market_sell(btc)
                self.bot.in_position = False
                self.send("❌ Position manuell geschlossen.")

        elif text == "/help":
            self.send(
                "📋 *Verfügbare Befehle:*\n"
                "/status – aktuelle Lage\n"
                "/buy – sofort kaufen\n"
                "/sell – sofort verkaufen\n"
                "/cancel – Position schließen\n"
                "/help – Hilfe anzeigen"
            )
