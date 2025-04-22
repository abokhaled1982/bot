# starter.py

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

    # Deine Strategien
    strategies = [
        StrategyEngine(indicators=["rsi"], config={"rsi": 10, "pct": 0.3, "amount": 19}),
        StrategyEngine(indicators=["bollinger"], config={"bollinger_margin": -0.005, "pct": 0.3, "amount": 15})
        #StrategyEngine(indicators=["rsi"], config={"rsi": 10, "pct": 1, "amount": 4})
    ]

    # Trader mit eigenem Logger & Stats
    traders = []
    for strategy in strategies:
        logger = Logger(telegram=telegram, prefix=strategy.name)
        trader = Trader(strategy_engine=strategy, account=account, orders=orders, logger=logger)
        traders.append(trader)
        logger.log("🚀 Trader gestartet")

    while True:
        try:
            telegram.check_for_commands()
            market_data = fetch_candles(client, symbol="BTCEUR", interval="5m")

            for trader in traders:
                trader.logger.log(trader.summary_line(market_data))
                trader.update(market_data)

        except Exception as e:
            main_logger.log(f"❌ Fehler: {e}")

        time.sleep(60)
