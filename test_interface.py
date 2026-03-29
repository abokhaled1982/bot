import asyncio
from adapters.dexscreener import DexScreenerAdapter
from adapters.telegram_mirror import TelegramAlphaMirror
import sqlite3
import os

async def test_interfaces():
    print("--- TESTING INTERFACES ---")
    
    # 1. DexScreener Test
    dex = DexScreenerAdapter()
    print("Fetching boosted tokens from DexScreener...")
    boosted = await dex.get_boosted_tokens()
    print(f"Found {len(boosted)} boosted tokens.")
    if boosted:
        print(f"Example: {boosted[0]}")
        
    # 2. Telegram Database Check
    conn = sqlite3.connect("memecoin_bot.db")
    c = conn.cursor()
    c.execute("SELECT count(*) FROM news_items")
    count = c.fetchone()[0]
    print(f"Messages in database: {count}")
    if count > 0:
        c.execute("SELECT source, token_symbol FROM news_items LIMIT 5")
        print("Last 5 mentions:", c.fetchall())
    conn.close()
    
    print("--- INTERFACE TEST COMPLETE ---")

asyncio.run(test_interfaces())
