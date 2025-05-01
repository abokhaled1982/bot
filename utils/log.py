from datetime import datetime
from zoneinfo import ZoneInfo

class Logger:
    def __init__(self, telegram=None, prefix=""):
        self.telegram = telegram
        self.prefix = prefix

    def log(self, message):
        now = datetime.now(ZoneInfo("Europe/Berlin")).strftime('%d.%m.%Y %H:%M')
        full = f"[{now}]"

        # Wenn message mehrzeilig ist → jede Zeile einrücken
        if "\n" in message:
            indented = "\n".join("  " + line for line in message.splitlines())
            full += f"\n{indented}"
        else:
            full += f" {message}"

        print(full)

        if self.telegram:
            self.telegram.send(full)
