import os
from telethon import TelegramClient
from dotenv import load_dotenv

# Lade Umgebungsvariablen
load_dotenv()

api_id = int(os.getenv("TG_API_ID"))
api_hash = os.getenv("TG_API_HASH")
phone = os.getenv("TG_PHONE_NUMBER")
session_name = '.sessions/anon_mirror_session'

async def main():
    # Erstelle den Session-Ordner, falls er nicht existiert
    if not os.path.exists('.sessions'):
        os.makedirs('.sessions')

    client = TelegramClient(session_name, api_id, api_hash)
    
    print("Starte Telegram-Authentifizierung...")
    await client.start(phone=phone)
    
    print("Erfolgreich eingeloggt!")
    print(f"Session-Datei wurde unter {session_name}.session gespeichert.")
    
    await client.disconnect()

if __name__ == '__main__':
    import asyncio
    asyncio.run(main())
