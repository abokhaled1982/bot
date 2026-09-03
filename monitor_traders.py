#!/usr/bin/env python3
"""Monitor found Binance leaderboard traders and print only trading signals."""
from __future__ import annotations

import argparse
import asyncio
import os

os.environ.setdefault("BNLB_AUTO_DISCOVER", "False")
os.environ.setdefault("BNLB_EMIT_AUTO_SIGNALS", "True")
os.environ["DRY_RUN"] = "True"

from loguru import logger

from src.adapters.binance_leaderboard import (
    BinanceLeaderboardTrader,
    binance_leaderboard_url,
    find_intraday_traders,
)
from src.adapters.binance_orderflow import BinanceOrderFlowAdapter
from src.bot.copytrader_pipeline import handle_copy_signal


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Find and monitor Binance leaderboard traders."
    )
    parser.add_argument("--limit", type=int, default=5)
    parser.add_argument("--pool-size", type=int, default=50)
    parser.add_argument("--all", action="store_true")
    parser.add_argument("--min-day-roi", type=float, default=None)
    parser.add_argument("--min-day-pnl", type=float, default=None)
    parser.add_argument("--min-win-rate", type=float, default=None)
    parser.add_argument("--poll-interval", type=float, default=5.0)
    parser.add_argument("--show-status", action="store_true")
    parser.add_argument("--status-interval", type=float, default=1.0)
    parser.add_argument("--paper-copy", action="store_true")
    return parser.parse_args()


def find_traders(args: argparse.Namespace):
    kwargs = {
        "limit": args.limit,
        "pool_size": args.pool_size,
        "verified_only": not args.all,
    }
    if args.min_day_roi is not None:
        kwargs["min_day_roi_pct"] = args.min_day_roi
    if args.min_day_pnl is not None:
        kwargs["min_day_pnl_usd"] = args.min_day_pnl
    if args.min_win_rate is not None:
        kwargs["min_win_rate_pct"] = args.min_win_rate
    return find_intraday_traders(**kwargs)


async def _status_loop(
    adapter: BinanceLeaderboardTrader, interval: float,
) -> None:
    while True:
        status = adapter.status()
        print(
            f"TRACKING traders={status['tracked_traders']} "
            f"polls={status['poll_count']} "
            f"positions={status['total_positions']} "
            f"api={status['api_health']}",
            flush=True,
        )
        await asyncio.sleep(interval)


async def monitor(
    trader_ids: list[str], poll_interval: float, show_status: bool,
    status_interval: float, paper_copy: bool,
) -> None:
    adapter = BinanceLeaderboardTrader(publish_state=False)
    market_adapter = BinanceOrderFlowAdapter()
    positions: dict = {}
    adapter.set_poll_interval(poll_interval)
    adapter._auto_uids = trader_ids
    adapter._rebuild_uid_list()

    monitor_task = asyncio.create_task(adapter.start())
    market_task = (
        asyncio.create_task(market_adapter.start())
        if paper_copy else None
    )
    status_task = (
        asyncio.create_task(_status_loop(adapter, status_interval))
        if show_status else None
    )
    try:
        while True:
            signal = await adapter.signal_queue.get()
            print(
                f"SIGNAL trader={signal.trader} action={signal.signal} "
                f"coin={signal.coin} symbol={signal.symbol} "
                f"size_usd={signal.size_usd:.2f} "
                f"entry={signal.entry_price:.8g} "
                f"leverage={signal.leverage:.2f}x "
                f"pnl_pct={signal.pnl_pct:.2f}",
                flush=True,
            )
            if paper_copy:
                paper_action = (
                    "BUY" if signal.signal in ("COPY_OPEN_LONG", "COPY_INCREASE")
                    else "CLOSE"
                )
                success = await handle_copy_signal(
                    signal, market_adapter, positions, adapter,
                )
                print(
                    f"PAPER action={paper_action} symbol={signal.symbol} "
                    f"result={'executed' if success else 'skipped'} "
                    f"open_positions={len(positions)}",
                    flush=True,
                )
    finally:
        monitor_task.cancel()
        await asyncio.gather(monitor_task, return_exceptions=True)
        if market_task:
            market_task.cancel()
            await asyncio.gather(market_task, return_exceptions=True)
        if status_task:
            status_task.cancel()
            await asyncio.gather(status_task, return_exceptions=True)


async def main() -> None:
    logger.remove()
    args = parse_args()
    candidates = await asyncio.to_thread(find_traders, args)
    if candidates:
        print(f"SCAN found={len(candidates)} source=Binance leaderboard", flush=True)
        for index, candidate in enumerate(candidates, 1):
            metrics = candidate.metrics
            win_rate = (
                f"{metrics.win_rate:.1f}%"
                if metrics.win_rate is not None else "n/a"
            )
            print(
                f"TRADER {index} name={metrics.nick_name or 'n/a'} "
                f"uid={candidate.uid} day_roi={metrics.day_roi:.1f}% "
                f"win_rate={win_rate} "
                f"profile={binance_leaderboard_url(candidate.uid)}",
                flush=True,
            )
        print(
            f"TRACKING started={len(candidates)} poll_interval={args.poll_interval}s "
            f"paper_copy={args.paper_copy}",
            flush=True,
        )
        await monitor(
            [candidate.uid for candidate in candidates],
            args.poll_interval, args.show_status,
            args.status_interval, args.paper_copy,
        )
    elif args.show_status:
        print("TRACKING traders=0 polls=0 positions=0 api=unknown", flush=True)


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        pass
