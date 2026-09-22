python3 find_traders.py --limit 20 --append-to traders_export.json
# in der JSON bei den Interessanten is_copied auf 1 setzen
python3 simulate_copytrader.py --min-win-rate 80 --size-usdt 10
# ein paar Tage laufen lassen, dann im Bot:
/simtop 10
/follow <TRADER_ID> 20