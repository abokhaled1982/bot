from adapters.dexscreener import DexScreenerAdapter
import asyncio

async def fetch():
    dex = DexScreenerAdapter()
    print("Fetching LIVE Boosted Tokens from DexScreener:")
    boosted = await dex.get_boosted_tokens()
    for b in boosted[:30]:
        data = await dex.get_token_data(b['address'])
        if data:
            print(f"Token: {data['symbol']} | Spike: {data['volume_spike']:.2f}x | Liq: {data['liquidity_usd']:.0f} USD")
        else:
            print(f"Token: {b['symbol']} (No data)")

asyncio.run(fetch())
