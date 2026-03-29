import os
import asyncio
from dotenv import load_dotenv
from binance import AsyncClient
from loguru import logger

load_dotenv()

async def check_balance():
    api_key = os.getenv("BINANCE_API_KEY")
    api_secret = os.getenv("BINANCE_SECRET")
    
    if not api_key or not api_secret:
        logger.error("Binance API keys not found in .env")
        return

    try:
        client = await AsyncClient.create(api_key, api_secret)
        account = await client.get_account()
        
        # Filter for balances > 0
        balances = [b for b in account['balances'] if float(b['free']) > 0 or float(b['locked']) > 0]
        
        print("\n--- Dein aktueller Binance-Kontostand ---")
        for b in balances:
            print(f"{b['asset']}: {b['free']} (locked: {b['locked']})")
        
        await client.close_connection()
    except Exception as e:
        logger.error(f"Fehler beim Abrufen des Kontostands: {e}")

if __name__ == "__main__":
    asyncio.run(check_balance())
