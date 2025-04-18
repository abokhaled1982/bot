# utils/log.py

from datetime import datetime
from zoneinfo import ZoneInfo

class Logger:
    def __init__(self, telegram=None, logfile="scalper.log", prefix=None):
        self.telegram = telegram
        self.logfile = logfile
        self.prefix = prefix or ""

    def log(self, message: str):
        now = datetime.now(ZoneInfo("Europe/Berlin")).strftime('%d.%m.%Y %H:%M')
        full = f"[{now}]{message}"
        print(full)
        with open(self.logfile, "a", encoding="utf-8") as f:
            f.write(full + "\n")
        if self.telegram:
            try:
                self.telegram.send(full)
            except Exception:
                pass
