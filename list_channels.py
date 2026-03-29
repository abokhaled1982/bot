from telethon import TelegramClient
import os
from dotenv import load_dotenv

load_dotenv()
api_id = int(os.getenv('TG_API_ID'))
api_hash = os.getenv('TG_API_HASH')

client = TelegramClient('anon_mirror_session', api_id, api_hash)

async def main():
    await client.start()
    async for dialog in client.iter_dialogs():
        if dialog.is_channel:
            print(f"Name: {dialog.name}, ID: {dialog.id}")

import asyncio
asyncio.run(main())
