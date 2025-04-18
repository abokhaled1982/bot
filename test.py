from binance.spot import Spot

client = Spot()



price_info = client.ticker_price(symbol="BTCUSDT")
print("Aktueller BTC Preis:", price_info["price"])


candles = client.klines(symbol="BTCUSDT", interval="1m", limit=100)

# Beispiel: erste 5 Kerzen anzeigen
for candle in candles[:5]:
    open_time = candle[0]
    open_price = candle[1]
    high = candle[2]
    low = candle[3]
    close = candle[4]
    volume = candle[5]
    print(f"🕒 {open_time} | 🟢 {open_price} | 🔴 {close} | High: {high} | Low: {low} | Vol: {volume}")
