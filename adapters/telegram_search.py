from telethon.sync import TelegramClient
from telethon.tl.functions.contacts import SearchRequest
from telethon.tl.functions.channels import JoinChannelRequest
import os
from dotenv import load_dotenv
from loguru import logger

load_dotenv()

async def find_and_join_alpha_channels():
    api_id = int(os.getenv('TG_API_ID'))
    api_hash = os.getenv('TG_API_HASH')
    
    # Session name must match the one used in setup_telegram.py
    client = TelegramClient('anon_mirror_session', api_id, api_hash)
    await client.start()
    
    # Liste von Keywords für die Suche
    keywords = ["Solana Gems", "Memecoin Alpha", "Crypto Calls", "Gem Finder", "Moonshot Calls"]
    
    for kw in keywords:
        logger.info(f"Suche nach Kanal: {kw}")
        results = await client(SearchRequest(q=kw, limit=5))
        
        for chat in results.chats:
            if hasattr(chat, 'username') and chat.username:
                try:
                    logger.info(f"Trete Kanal bei: {chat.title} (@{chat.username})")
                    await client(JoinChannelRequest(chat.username))
                except Exception as e:
                    logger.error(f"Konnte {chat.title} nicht beitreten: {e}")
                    
    await client.disconnect()

if __name__ == "__main__":
    import asyncio
    asyncio.run(find_and_join_alpha_channels())
