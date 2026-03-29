from telethon import TelegramClient
import os
from dotenv import load_dotenv

load_dotenv()
client = TelegramClient('anon_mirror_session', int(os.getenv('TG_API_ID')), os.getenv('TG_API_HASH'))

async def main():
    await client.connect()
    if await client.is_user_authorized():
        print("SESSION_VALID")
    else:
        print("SESSION_INVALID")
    await client.disconnect()

import asyncio
asyncio.run(main())
