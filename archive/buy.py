from coinbase.rest import RESTClient
import uuid
import time

PRODUCT_ID = "BTC-EUR"
TRADE_AMOUNT_EUR = 9.0
client = RESTClient(key_file="./cdp_api_key.json")

# Preis abfragen
def get_current_price():
    try:
        ticker = client.get_product(product_id=PRODUCT_ID)
        return float(ticker['price'])
    except Exception as e:
        print("Fehler beim Preisabruf:", e)
        return None

# Kaufversuch mit Retry

def buy_btc_with_retry(amount_eur, max_retries=3):
    for attempt in range(max_retries):
        try:
            order_config = {"market_market_ioc": {"quote_size": str(amount_eur)}}
            response = client.create_order(
                client_order_id=str(uuid.uuid4()),
                product_id=PRODUCT_ID,
                side="BUY",
                order_configuration=order_config
            )
            print("\u2705 Kauf erfolgreich:", response)
            return
        except Exception as e:
            print(f"Fehler beim Kaufversuch {attempt + 1}: {e}")
            time.sleep(5)
    print("\u274c Kauf nach mehreren Versuchen fehlgeschlagen.")

# Beispielaufruf
price = get_current_price()
if price:
    print(f"Aktueller BTC-Preis: {price} EUR")
    buy_btc_with_retry(TRADE_AMOUNT_EUR)
