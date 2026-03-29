from telethon import TelegramClient
import os
from dotenv import load_dotenv
import asyncio

load_dotenv()
api_id = int(os.getenv('TG_API_ID'))
api_hash = os.getenv('TG_API_HASH')
phone = os.getenv('TG_PHONE_NUMBER')
password = os.getenv('TG_PASSWORD')

# Explicitly store session in a known, persistent file
client = TelegramClient('anon_mirror_session', api_id, api_hash)

async def main():
    print("Connecting...")
    await client.connect()
    if not await client.is_user_authorized():
        print("Not authorized, starting login...")
        await client.start(phone=phone, password=password)
        print("Logged in successfully!")
    else:
        print("Already authorized.")
    
    # Keep it open for a second to ensure session is flushed to disk
    await asyncio.sleep(2)
    await client.disconnect()
    print("Session saved.")

asyncio.run(main())
