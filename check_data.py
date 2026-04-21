import asyncio
from src.adapters.binance_stream import BinanceStreamAdapter

async def main():
    adapter = BinanceStreamAdapter()
    t = asyncio.create_task(adapter.start())
    await asyncio.sleep(5)
    print("Tickers:", len(adapter.all_tickers()))
    t.cancel()

asyncio.run(main())
