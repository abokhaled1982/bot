import requests


class TelegramNotifier:
    def __init__(self, token, chat_id, bot=None):
        self.token = token
        self.chat_id = chat_id
        self.bot = bot  # Zugriff auf CryptoScalper-Instanz
        self.api_url = f"https://api.telegram.org/bot{token}"
        self.last_update_id = None

    def send(self, message):
        try:
            requests.post(f"{self.api_url}/sendMessage", data={
                "chat_id": self.chat_id,
                "text":message,
                "parse_mode": "Markdown"
            })
        except Exception as e:
            print(f"[Telegram] Fehler beim Senden: {e}")

    def check_for_commands(self):
        """Holt neue Telegram-Befehle und führt direkt aus."""
        try:
            params = {'timeout': 5}
            if self.last_update_id:
                params['offset'] = self.last_update_id + 1

            response = requests.get(f"{self.api_url}/getUpdates", params=params)
            data = response.json()

            if not data.get("ok"):
                return

            for update in data["result"]:
                self.last_update_id = update["update_id"]
                message = update.get("message", {})
                text = message.get("text", "").strip().lower()

                if text == "/status":
                    if self.bot:                       
                        self.bot.print_stats(self.bot.fetch_price())
                elif text == "/buy":
                    if self.bot:
                        eur = self.bot.get_eur_balance()
                        self.bot.place_market_buy(eur)
                elif text == "/sell":
                    if self.bot:
                        btc = self.bot.get_btc_balance()
                        self.bot.place_market_sell(btc)
                elif text == "/cancel":
                    if self.bot and self.bot.in_position:
                        btc = self.bot.get_btc_balance()
                        self.bot.place_market_sell(btc)
                        self.bot.in_position = False
                        self.send("❌ Position manuell geschlossen.")
                elif text == "/help":
                    self.send("📋 *Verfügbare Befehle:*\n/buy\n/sell\n/status\n/cancel\n/help")

        except Exception as e:
            print(f"[Telegram] Fehler beim Verarbeiten von Kommandos: {e}")
