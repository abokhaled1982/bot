import discum
import os
from dotenv import load_dotenv

load_dotenv()
TOKEN = os.getenv("DISCORD_TOKEN")

bot = discum.Client(token=TOKEN)

guilds = bot.getGuilds().json()
for guild in guilds:
    print(f"\nServer: {guild['name']}")
    channels = bot.getGuildChannels(guild['id']).json()
    for ch in channels:
        if ch.get('type') == 0:
            print(f"  #{ch['name']} — ID: {ch['id']}")