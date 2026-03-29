from telethon.sync import TelegramClient
import os
from dotenv import load_dotenv

load_dotenv()
api_id = int(os.getenv('TG_API_ID'))
api_hash = os.getenv('TG_API_HASH')
phone = os.getenv('TG_PHONE_NUMBER')
password = os.getenv('TG_PASSWORD')

# Speichere die Session exakt unter diesem Namen
client = TelegramClient('anon_mirror_session', api_id, api_hash)
client.connect()

if not client.is_user_authorized():
    print("Authorization required...")
    client.start(phone=phone, password=password)
    print("Logged in successfully!")
else:
    print("Already authorized, session file is valid.")
