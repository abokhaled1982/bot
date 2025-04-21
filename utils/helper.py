import json
from datetime import datetime, timedelta
import pandas as pd
from ta.momentum import RSIIndicator
from ta.trend import EMAIndicator

def load_config(path="./utils/config.json"):
    with open(path, "r") as f:
        return json.load(f)


def fetch_price(client, product_id):
    try:
        ticker = client.get_product(product_id=product_id)
        return float(ticker["price"])
    except Exception as e:
        return None

def fetch_candles2(client, product_id, granularity="ONE_MINUTE", limit=100):
    try:
        now = datetime.now()
        end = now.replace(second=0, microsecond=0)
        start = end - timedelta(minutes=limit)



        response = client.get_candles(
            product_id=product_id,
            start=int(start.timestamp()),
            end=int(end.timestamp()),
            granularity=granularity,
            limit=limit
        )

        candles = [
            {
                "start": datetime.utcfromtimestamp(int(c["start"])),
                "open": float(c["open"]),
                "close": float(c["close"]),
                "high": float(c["high"]),
                "low": float(c["low"]),
                "volume": float(c["volume"])
            }
            for c in response.candles
        ]

        candles.sort(key=lambda x: x["start"])

        print("Letzte Candle:")
        print(f"  Zeit (UTC): {candles[-1]['start']}")
        print(f"  Close: {candles[-1]['close']}")
        print(f"  Alter (Sekunden): {(datetime.utcnow() - candles[-1]['start']).total_seconds()}")

        return candles
    except Exception as e:
        print(f"Fehler: {e}")
        return None


def fetch_candles(client, product_id, granularity="ONE_MINUTE", limit=100):
    try:
       # Example settings
        symbol = "BTCEUR"
        interval = "1m"  # Options: "1m", "5m", "15m", "1h", "1d", etc.
        limit = 100  # Max: 1000
       
        raw_candles = client.klines(symbol=symbol, interval=interval, limit=limit)
        candles = []
        for c in raw_candles:
            candle = {
                "open_time": datetime.fromtimestamp(c[0] / 1000),
                "open": float(c[1]),
                "high": float(c[2]),
                "low": float(c[3]),
                "close": float(c[4]),
                "volume": float(c[5]),
                "close_time": datetime.fromtimestamp(c[6] / 1000),
                "quote_asset_volume": float(c[7]),
                "trades": int(c[8]),
                "taker_buy_base_volume": float(c[9]),
                "taker_buy_quote_volume": float(c[10]),
            }
            candles.append(candle)
        #close candle    
        candles.sort(key=lambda x: x["open_time"])

        return candles
    except Exception as e:
        print(f"Fehler: {e}")
        return None

def calculate_rsi(closes, window):
    try:
        return RSIIndicator(pd.Series(closes), window=window).rsi().iloc[-1]
    except:
        return None

def calculate_ema(closes, window):
    try:
        return EMAIndicator(pd.Series(closes), window=window).ema_indicator().iloc[-1]
    except:
        return None