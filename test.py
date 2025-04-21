from core.Trader import Trader

from utils.TelegramControler import TelegramController
from utils.log import Logger
from utils.helper import load_config, fetch_candles, calculate_rsi, calculate_ema
from utils.account import AccountManager
from utils.orders import OrderManager
from core.StrategyEngine import StrategyEngine
import argparse
import time


from binance.spot import Spot

client = Spot()


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    config = load_config()
 
    client=Spot(api_key=config["binance"]["api_key"], api_secret=config["binance"]["api_secret"])

    telegram = TelegramController(
        token=config["telegramm"]["token"],
        chat_id=config["telegramm"]["chat_id"]
    )

    main_logger = Logger(telegram=telegram)
    account = AccountManager(client, main_logger)
    orders = OrderManager(client, main_logger, dry_run=args.dry_run)

    candles = fetch_candles(client, "BTC-EUR")
    closes = [c["close"] for c in candles]
    price = closes[-1]
    rsi = calculate_rsi(closes, 2)
    ema = calculate_ema(closes, 20)

    market_data = {
        "price": price,
        "rsi": rsi,
        "ema": ema,
        "timestamp": time.time()
    }

    amount_btc = account.get_balance("BTC")

    orders.place_market_sell(amount_btc)