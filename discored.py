"""
Discord News Engine - Complete Single File
Runs forever, saves ALL messages to market_news_warehouse.csv
No keyword filter — every message from watched channels is saved
"""

import os
import discum
import csv
import time
import random
import threading
import queue
import logging
import sys
from datetime import datetime, timezone
from collections import deque
from pathlib import Path
from dotenv import load_dotenv

# ─────────────────────────────────────────────
# CONFIG — EDIT THESE
# ─────────────────────────────────────────────

load_dotenv()
TOKEN = os.getenv("DISCORD_TOKEN")

CSV_PATH = "/home/whsg/bot/market_news_warehouse.csv"

CHANNELS = [
    "1359862685826678835",  # Speed Wallet - general-chat
    "1427279097972785162",  # Speed Wallet - trade-ideas
    "1484362244044230676",  # Speed Wallet - GHX4T-IDEAS
    "1393197548017291325",  # Speed Wallet - SAIF-TRADES
    # add more channel IDs here
]

CHANNEL_NAMES = {
    "1359862685826678835": "Discord/SpeedWallet/general-chat",
    "1427279097972785162": "Discord/SpeedWallet/trade-ideas",
    "1484362244044230676": "Discord/SpeedWallet/GHX4T-IDEAS",
    "1393197548017291325": "Discord/SpeedWallet/SAIF-TRADES",
}

KEYWORDS = [
    "BTC", "ETH", "SOL", "XRP", "BNB", "DOGE", "ADA", "AVAX",
    "AAPL", "TSLA", "NVDA", "SPY", "QQQ", "GME",
    "buy", "sell", "pump", "dump", "bullish", "bearish", "alpha",
    "signal", "moon", "short", "long", "entry", "exit", "target",
    "breakout", "support", "resistance", "ATH", "dip",
]

# ─────────────────────────────────────────────
# LOGGING
# ─────────────────────────────────────────────

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[logging.StreamHandler(sys.stdout)]
)
log = logging.getLogger("Engine")
logging.getLogger("discum").setLevel(logging.CRITICAL)
logging.getLogger("websocket").setLevel(logging.CRITICAL)

# ─────────────────────────────────────────────
# CSV — thread-safe append
# ─────────────────────────────────────────────

csv_lock = threading.Lock()

def ensure_csv():
    p = Path(CSV_PATH)
    if not p.exists():
        with open(CSV_PATH, "w", newline="", encoding="utf-8") as f:
            csv.writer(f).writerow(
                ["source", "category", "content", "url", "published_date", "fetched_date"]
            )
        log.info(f"Created new CSV: {CSV_PATH}")
    else:
        kb = p.stat().st_size / 1024
        log.info(f"✅ Found existing CSV: {CSV_PATH} ({kb:.1f} KB) — appending")

def append_row(source, category, content, url, date):
    with csv_lock:
        with open(CSV_PATH, "a", newline="", encoding="utf-8") as f:
            csv.writer(f).writerow([source, category, content, url, date, date])

# ─────────────────────────────────────────────
# SENTIMENT
# ─────────────────────────────────────────────

BULLISH = {"buy","bullish","moon","pump","breakout","long","ath","green","up","accumulate"}
BEARISH = {"sell","bearish","dump","rug","short","crash","red","exit","resistance"}

def sentiment(text):
    words = set(text.lower().split())
    b = len(words & BULLISH)
    s = len(words & BEARISH)
    if b > s: return "Crypto/Bullish"
    if s > b: return "Crypto/Bearish"
    return "Crypto/Neutral"

# ─────────────────────────────────────────────
# DEDUPLICATION
# ─────────────────────────────────────────────

seen = set()
seen_lock = threading.Lock()

def is_dup(mid):
    with seen_lock:
        if mid in seen: return True
        seen.add(mid)
        if len(seen) > 50000:
            [seen.discard(x) for x in list(seen)[:10000]]
        return False

# ─────────────────────────────────────────────
# ALERT QUEUE — background writer thread
# ─────────────────────────────────────────────

q = queue.Queue()
signals_saved = 0

def worker():
    global signals_saved
    while True:
        try:
            item = q.get(timeout=2)
            if item is None:
                break

            channel_id, author, content, found, mid = item
            now = datetime.now(timezone.utc).isoformat()
            source = CHANNEL_NAMES.get(channel_id, f"Discord/{channel_id}")
            cat = sentiment(content)
            url = f"https://discord.com/channels/@me/{channel_id}/{mid}"

            append_row(source, cat, f"[{author}] {content}", url, now)
            signals_saved += 1

            ts = datetime.now().strftime("%H:%M:%S")
            kw_str = ", ".join(found) if found else "—"
            print(f"\n{'─'*55}")
            print(f"⚡ [{ts}]  #{source.split('/')[-1]}")
            print(f"   👤 {author}: {content[:100]}")
            print(f"   🔑 {kw_str}  |  📊 {cat}")
            print(f"   💾 Saved to CSV  (total: {signals_saved})")

            q.task_done()
        except queue.Empty:
            continue
        except Exception as e:
            log.error(f"Worker error: {e}")

# ─────────────────────────────────────────────
# RATE LIMITER
# ─────────────────────────────────────────────

rl_times = deque()
rl_lock = threading.Lock()

def rate_limit():
    with rl_lock:
        now = time.time()
        while rl_times and rl_times[0] < now - 1:
            rl_times.popleft()
        if len(rl_times) >= 3:
            time.sleep(1 - (now - rl_times[0]) + random.uniform(0.05, 0.15))
        rl_times.append(time.time())

# ─────────────────────────────────────────────
# MESSAGE HANDLER — NO KEYWORD FILTER
# ─────────────────────────────────────────────

CHANNEL_SET = set(CHANNELS)
msgs_seen = 0

def on_message(resp):
    global msgs_seen
    try:
        if not resp.event.message:
            return
        msg = resp.parsed.auto()
        if not isinstance(msg, dict):
            return

        cid = msg.get("channel_id", "")
        if cid not in CHANNEL_SET:
            return

        mid = msg.get("id", "")
        if mid and is_dup(mid):
            return

        content = msg.get("content", "").strip()
        author = msg.get("author", {}).get("username", "unknown") \
            if isinstance(msg.get("author"), dict) else "unknown"

        msgs_seen += 1

        if not content or len(content) < 2:
            return

        # Keyword matching for labeling only — NOT filtering
        cl = content.lower()
        found = [kw for kw in KEYWORDS if kw.lower() in cl]

        # ✅ NO FILTER — every message gets saved
        q.put((cid, author, content, found, mid))

    except Exception:
        pass

# ─────────────────────────────────────────────
# MAIN LOOP — runs forever with auto-reconnect
# ─────────────────────────────────────────────

if __name__ == "__main__":
    print("""
╔══════════════════════════════════════════╗
║   Discord News Engine                    ║
║   ALL messages → market_news_warehouse   ║
║   No keyword filter — saving everything  ║
╚══════════════════════════════════════════╝
    """)

    ensure_csv()

    t = threading.Thread(target=worker, daemon=True)
    t.start()

    failures = 0

    while True:
        try:
            log.info(f"🔌 Connecting... (attempt {failures + 1})")
            bot = discum.Client(token=TOKEN, log=False)
            bot.gateway.command(on_message)
            log.info(f"✅ Live — watching {len(CHANNELS)} channels | saving ALL messages")
            failures = 0
            bot.gateway.run(auto_reconnect=True)

        except KeyboardInterrupt:
            log.info("🛑 Stopped — total saved: " + str(signals_saved))
            q.put(None)
            break

        except Exception as e:
            failures += 1
            delay = min(5 * (2 ** failures) + random.uniform(0, 3), 120)
            log.warning(f"⚠️  Disconnected ({e}) — retry in {delay:.0f}s")
            time.sleep(delay)