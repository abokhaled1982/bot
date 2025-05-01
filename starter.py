from core.Trader import Trader
from utils.TelegramControler import TelegramController
from utils.log import Logger
from utils.helper import load_config, fetch_candles
from utils.account import AccountManager
from utils.orders import OrderManager
from core.StrategyEngine import StrategyEngine
import argparse
import time

from binance.spot import Spot

if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    config = load_config()
    client = Spot(api_key=config["binance"]["api_key"], api_secret=config["binance"]["api_secret"])

    telegram = TelegramController(
        token=config["telegramm"]["token"],
        chat_id=config["telegramm"]["chat_id"]
    )

    main_logger = Logger(telegram=telegram)
    account = AccountManager(client, main_logger)
   

 
    # Trader erstellen
    traders = []
    for strat_cfg in config.get("strategies", []):
        strategy = StrategyEngine(strat_cfg)
        logger = Logger(telegram=telegram, prefix=f"{strategy.name}")
        orders = OrderManager(client, main_logger, symbole=strat_cfg["symbol"],dry_run=args.dry_run)
        trader = Trader(
            strategy_engine=strategy,
            account=account,
            orders=orders,
            logger=logger,
            symbol=strat_cfg["symbol"],
            interval=strat_cfg["interval"],
            coin=strat_cfg["coin"]
        )
        traders.append(trader)
        logger.log(f"🚀 Trader für {strat_cfg['symbol']} gestartet")

    while True:
        try:
            telegram.check_for_commands()
            for trader in traders:
                # Nur Marktdaten für den aktuellen Trader (Symbol!)
                market_data = fetch_candles(client, symbol=trader.symbol, interval=trader.interval)               
                trader.update(market_data)

        except Exception as e:
            main_logger.log(f"❌ Fehler: {e}")

        time.sleep(60)
