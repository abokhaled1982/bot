import os
from telethon import TelegramClient

async def list_dialogs():
    api_id = int(os.getenv("TG_API_ID"))
    api_hash = os.getenv("TG_API_HASH")
    session_name = '.sessions/anon_mirror_session'
    
    client = TelegramClient(session_name, api_id, api_hash)
    await client.start()
    
    print("Deine Telegram Kanäle:")
    async for dialog in client.iter_dialogs():
        if dialog.is_channel:
            print(f"Name: {dialog.name} | ID: {dialog.id}")
            
    await client.disconnect()

if __name__ == '__main__':
    from dotenv import load_dotenv
    import asyncio
    load_dotenv()
    asyncio.run(list_dialogs())
