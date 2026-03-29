import os
import re
import sqlite3
from telethon import TelegramClient, events
from loguru import logger
from dotenv import load_dotenv
from datetime import datetime, timedelta

load_dotenv()

DB_PATH = ".temp/memecoin_bot.db"

class TelegramAlphaMirror:
    def __init__(self):
        self.api_id = int(os.getenv("TG_API_ID"))
        self.api_hash = os.getenv("TG_API_HASH")
        self.client = TelegramClient(os.path.join(os.getcwd(), '.sessions/anon_mirror_session'), self.api_id, self.api_hash)
        self.tier1_channels = [-1003007547715, -1002786398276, -1002930278171]
        self.tier2_channels = [-1002283903153, -1002438747738, -1002174850334, -1001463496932]
        self.ticker_pattern = re.compile(r'\$([A-Za-z0-9]{2,10})')
        self.ca_pattern = re.compile(r'\b([1-9A-HJ-NP-Za-km-z]{32,44})\b')

    async def start_listening(self):
        await self.client.connect()
        if not await self.client.is_user_authorized():
            logger.error("Session not authorized. Please run setup_telegram.py.")
            return

        logger.info("Telegram Alpha Mirror active. Saving mentions to local SQLite...")
        
        @self.client.on(events.NewMessage(chats=self.tier1_channels + self.tier2_channels))
        async def handler(event):
            chat_id = event.chat_id
            text = event.raw_text
            tickers = self.ticker_pattern.findall(text)
            cas = self.ca_pattern.findall(text)
            identifiers = set(tickers + cas)
            
            if not identifiers: return
            
            weight = 3.0 if chat_id in self.tier1_channels else 1.0
            source_name = f"Telegram-Tier1:{chat_id}" if weight == 3.0 else f"Telegram-Tier2:{chat_id}"
            
            conn = sqlite3.connect(DB_PATH)
            c = conn.cursor()
            for ident in identifiers:
                c.execute("INSERT INTO news_items (source, content, token_symbol, sentiment_weight) VALUES (?, ?, ?, ?)",
                          (source_name, text, ident.upper(), weight))
            conn.commit()
            conn.close()
            logger.debug(f"Saved {len(identifiers)} mentions from {chat_id}")
            
        await self.client.run_until_disconnected()

    def get_recent_mentions(self, symbol, address, minutes=30):
        conn = sqlite3.connect(DB_PATH)
        c = conn.cursor()
        threshold = (datetime.utcnow() - timedelta(minutes=minutes)).isoformat()
        c.execute("SELECT source, content, sentiment_weight FROM news_items WHERE (token_symbol = ? OR token_symbol = ?) AND timestamp >= ?",
                  (symbol.upper(), address, threshold))
        rows = c.fetchall()
        conn.close()
        return [{"source": r[0], "content": r[1], "weight": r[2]} for r in rows]

    def get_channel_count_last_5m(self, symbol, address):
        conn = sqlite3.connect(DB_PATH)
        c = conn.cursor()
        threshold = (datetime.utcnow() - timedelta(minutes=5)).isoformat()
        c.execute("SELECT COUNT(DISTINCT source) FROM news_items WHERE (token_symbol = ? OR token_symbol = ?) AND timestamp >= ?",
                  (symbol.upper(), address, threshold))
        count = c.fetchone()[0]
        conn.close()
        return count or 0
