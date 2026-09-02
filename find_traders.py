#!/usr/bin/env python3
"""
find_traders.py — Finde profitable Intraday-Trader auf Binance (Futures-Leaderboard)

Nutzung:
    python3 find_traders.py                  # Top-Intraday-Trader (Standard-Filter)
    python3 find_traders.py --limit 10        # Nur Top 10
    python3 find_traders.py --all             # Auch nicht-verifizierte Kandidaten zeigen

Keine API-Keys noetig. Nutzt Binances oeffentliches (inoffizielles) Leaderboard.
"""
import argparse

from src.adapters.binance_leaderboard import (
    binance_leaderboard_url, find_intraday_traders,
)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--limit", type=int, default=20, help="Max. Anzahl Trader")
    parser.add_argument("--all", action="store_true",
                         help="Auch nicht-verifizierte Kandidaten anzeigen")
    parser.add_argument("--min-day-roi", type=float, default=None,
                         help="Mindest-Tages-ROI in %%")
    parser.add_argument("--min-day-pnl", type=float, default=None,
                         help="Mindest-Tages-PnL in USD")
    args = parser.parse_args()

    kwargs = {"limit": args.limit, "verified_only": not args.all}
    if args.min_day_roi is not None:
        kwargs["min_day_roi_pct"] = args.min_day_roi
    if args.min_day_pnl is not None:
        kwargs["min_day_pnl_usd"] = args.min_day_pnl

    print("\n🏆 Binance Futures-Leaderboard — Top Intraday-Trader")
    print("=" * 70)
    print("\nLade Daten von binance.com ...")

    candidates = find_intraday_traders(**kwargs)

    if not candidates:
        print("Keine Trader gefunden, die die Filterkriterien erfuellen.")
        return

    print(f"\n{'Rang':<6} {'Score':<7} {'Tages-ROI':<11} {'Tages-PnL':<12} "
          f"{'7T-ROI':<9} {'30T-ROI':<9} {'Follower':<9} {'Name'}")
    print(f"{'-'*6} {'-'*7} {'-'*11} {'-'*12} {'-'*9} {'-'*9} {'-'*9} {'-'*20}")

    for i, c in enumerate(candidates, 1):
        m = c.metrics
        week = f"{m.week_roi:+.1f}%" if m.week_roi is not None else "—"
        month = f"{m.month_roi:+.1f}%" if m.month_roi is not None else "—"
        print(f"#{i:<5} {c.quality_score:<7.1f} {m.day_roi:>+9.1f}% "
              f"${m.day_pnl:>+9,.0f} {week:<9} {month:<9} {m.follower_count:<9} "
              f"{m.nick_name}")

    print("\n💡 Tipp: Profil pruefen:")
    print(f"   {binance_leaderboard_url(candidates[0].uid)}")
    print("\n💡 Tipp: UID in .env eintragen, um dauerhaft zu tracken:")
    print("   BNLB_TRADER_UIDS=uid1,uid2")


if __name__ == "__main__":
    main()
