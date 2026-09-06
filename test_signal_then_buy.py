#!/usr/bin/env python3
"""
test_signal_then_buy.py — Einmaliger Live-Test: wartet auf ein ECHTES
COPY_OPEN_LONG-Signal eines abonnierten Traders (siehe data/copy_traders.json)
und kauft danach genau einmal einen FESTEN Betrag (Default 20 USDT) des Coins,
den der Trader gerade eroeffnet hat.

Nutzt ausschliesslich Produktionscode, kein Mock:
  * Ueberwachung:      src.monitoring.CopyTraderMonitor (echtes Polling)
  * Order-Ausfuehrung: src.execution.real_executor.buy_and_protect (Market-Buy + OCO)

Der Kaufbetrag ist bewusst fest ueber --amount gesetzt — unabhaengig vom in
copy_traders.json hinterlegten `size_usdt` (das steuert live_copytrader.py,
nicht dieses Skript). Anders als live_copytrader.py hat dieses Skript KEINE
Risiko-Gates (STOP_BOT/max-positions/daily-loss), keine Positions-Persistenz
und keine Benachrichtigungen — es kauft exakt einmal und beendet sich danach.

ACHTUNG: platziert eine ECHTE Order, sobald `DRY_RUN=False` in `.env` steht.

Start:
  python3 test_signal_then_buy.py                                     # 20 USDT, jeder abonnierte Trader
  python3 test_signal_then_buy.py --amount 20 --trader 5041579634804156928
"""
from __future__ import annotations

import argparse
import asyncio
import os
import sys

from dotenv import load_dotenv
from loguru import logger

_REPO_ROOT = os.path.dirname(os.path.abspath(__file__))
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)

# Muss vor dem Import der src-Module laufen: die lesen ihre Config beim Import.
load_dotenv(os.path.join(_REPO_ROOT, ".env"))

os.environ.setdefault("BNLB_AUTO_DISCOVER", "False")
os.environ.setdefault("BNLB_EMIT_AUTO_SIGNALS", "True")

from src.execution import real_executor as ex  # noqa: E402
from src.monitoring import CopyTraderMonitor  # noqa: E402


async def _wait_for_signal_and_buy(
    monitor: CopyTraderMonitor, amount_usdt: float, only_trader: str | None,
) -> None:
    async for sig in monitor.signals():
        if sig.signal != "COPY_OPEN_LONG" or not monitor.is_following(sig.trader):
            continue
        if only_trader and sig.trader != only_trader:
            continue

        logger.success(
            f"[TEST] 📡 Signal empfangen: {sig.trader} eroeffnet {sig.coin} "
            f"@ ${sig.entry_price:.6f} — kaufe jetzt ${amount_usdt:.2f} USDT"
        )
        buy, oco = ex.buy_and_protect(
            sig.symbol, amount_usdt,
            trader=sig.trader, coin=sig.coin,
            price_hint=float(sig.entry_price or 0.0) or None,
        )
        logger.info(buy.summary())
        if oco is not None:
            logger.info(oco.summary())
        return  # genau EIN Kauf, danach fertig


async def _run(args: argparse.Namespace) -> None:
    mode = "LIVE" if not ex.DRY_RUN else "DRY-RUN"
    logger.info(
        f"[TEST] Modus: {mode} | Kaufbetrag: ${args.amount:.2f} | "
        f"Trader-Filter: {args.trader or 'alle abonnierten'}"
    )

    monitor = CopyTraderMonitor(
        poll_interval=args.poll_interval, min_copy_size_usd=args.min_copy_size_usd,
    )
    followed = monitor.followed()
    if not followed:
        logger.error(
            "[TEST] Kein Trader in data/copy_traders.json abonniert (is_copied=1) — Abbruch."
        )
        return
    logger.info(f"[TEST] Beobachte: {', '.join(followed)} — warte auf frischen Long-Open …")

    monitor_task = asyncio.create_task(monitor.start())
    try:
        await _wait_for_signal_and_buy(monitor, args.amount, args.trader)
    finally:
        monitor_task.cancel()
        try:
            await monitor_task
        except asyncio.CancelledError:
            pass
    logger.info("[TEST] Fertig — beendet.")


def _parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(
        description="Wartet auf ein echtes Copy-Signal und kauft danach einmalig",
    )
    p.add_argument("--amount", type=float, default=20.0,
                    help="Fester Kaufbetrag in USDT (Default 20)")
    p.add_argument("--trader", default="",
                    help="Nur auf diesen Trader warten (leer = alle abonnierten)")
    p.add_argument("--poll-interval", type=float, default=3.0)
    p.add_argument("--min-copy-size-usd", type=float, default=50.0,
                    help="Positionen des Traders unter diesem Wert erzeugen kein Signal")
    return p.parse_args()


def main() -> int:
    args = _parse_args()
    args.trader = args.trader.strip() or None
    logger.remove()
    logger.add(sys.stderr, level="INFO", format=(
        "<green>{time:HH:mm:ss}</green> | <level>{level: <7}</level> | {message}"
    ))
    try:
        asyncio.run(_run(args))
    except KeyboardInterrupt:
        logger.info("[TEST] abgebrochen")
    return 0


if __name__ == "__main__":
    sys.exit(main())
