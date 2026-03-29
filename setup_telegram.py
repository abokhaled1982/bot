from telethon import TelegramClient
import os
import sys
from dotenv import load_dotenv

load_dotenv()
api_id = int(os.getenv('TG_API_ID'))
api_hash = os.getenv('TG_API_HASH')

print('Starte Telegram Authentifizierung...')
client = TelegramClient('anon_mirror_session', api_id, api_hash)
client.start()
print('Erfolgreich eingeloggt! Die Datei anon_mirror_session.session wurde erstellt.')
