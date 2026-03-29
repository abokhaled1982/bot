import os
import asyncio
from dotenv import load_dotenv
from binance import AsyncClient
from loguru import logger

load_dotenv()

async def get_total_balance():
    api_key = os.getenv("BINANCE_API_KEY")
    api_secret = os.getenv("BINANCE_SECRET")
    
    if not api_key or not api_secret:
        logger.error("Binance API keys not found in .env")
        return

    try:
        client = await AsyncClient.create(api_key, api_secret)
        
        # Get prices to convert to USDT
        prices = await client.get_all_tickers()
        price_map = {p['symbol']: float(p['price']) for p in prices}
        
        account = await client.get_account()
        total_usdt = 0.0
        
        print("\n--- Dein geschätzter Gesamtwert in USDT ---")
        for b in account['balances']:
            asset = b['asset']
            free = float(b['free'])
            locked = float(b['locked'])
            amount = free + locked
            
            if amount > 0:
                if asset == 'USDT':
                    total_usdt += amount
                else:
                    pair = f"{asset}USDT"
                    if pair in price_map:
                        value = amount * price_map[pair]
                        total_usdt += value
                    else:
                        # Try reverse pair (USDT/Asset) or skip if not found
                        pair_rev = f"USDT{asset}"
                        if pair_rev in price_map:
                            value = amount / price_map[pair_rev]
                            total_usdt += value
                        else:
                            # Cannot estimate value for very small/unknown assets
                            pass
        
        print(f"Gesamtwert: ca. {total_usdt:.2f} USDT")
        await client.close_connection()
    except Exception as e:
        logger.error(f"Fehler beim Berechnen des Werts: {e}")

if __name__ == "__main__":
    asyncio.run(get_total_balance())
