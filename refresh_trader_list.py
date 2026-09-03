#!/usr/bin/env python3
"""Rangliste der besten Binance-Leaderboard-Trader neu aufbauen.

Nutzt die Scanner-Funktion `find_intraday_traders()` aus dem Bot-Adapter,
optional wird die alte `copy_traders`-Tabelle vorher geleert.
"""
from __future__ import annotations

import argparse
import time

from src.adapters.binance_leaderboard import (
    binance_leaderboard_url,
    find_intraday_traders,
)
from src.utils import trader_store


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Beste Trader vom Binance-Leaderboard finden und in DB speichern."
    )
    parser.add_argument(
        "--limit", type=int, default=100,
        help="Anzahl der zu speichernden Top-Trader (Standard: 100).",
    )
    parser.add_argument(
        "--pool-size", type=int, default=2000,
        help="Größe des Kandidaten-Pools beim Scan (Standard: 2000).",
    )
    parser.add_argument(
        "--min-day-roi", type=float, default=3.0,
        help="Mindest-Tages-ROI in %% (Standard: 3.0).",
    )
    parser.add_argument(
        "--min-day-pnl", type=float, default=50.0,
        help="Mindest-Tages-PnL in USD (Standard: 50).",
    )
    parser.add_argument(
        "--min-week-roi", type=float, default=0.0,
        help="Mindest-7-Tage-ROI in %% (Standard: 0).",
    )
    parser.add_argument(
        "--min-month-roi", type=float, default=0.0,
        help="Mindest-30-Tage-ROI in %% (Standard: 0).",
    )
    parser.add_argument(
        "--activate",
        action="store_true",
        help="Neu gespeicherte Trader direkt auf is_copied=1 setzen.",
    )
    parser.add_argument(
        "--clear-existing",
        action="store_true",
        help="Vorhandene copy_traders vor dem Neubefüllen löschen.",
    )
    parser.add_argument(
        "--confirm-delete",
        action="store_true",
        help="Pflichtflag zusätzlich zu --clear-existing (Sicherheitshinweis).",
    )
    parser.add_argument(
        "--dry-run", action="store_true",
        help="Nur anzeigen, was passieren würde — nichts speichern.",
    )
    return parser.parse_args()


def clear_traders() -> int:
    """Alle Einträge aus copy_traders und tracker_state löschen."""
    existing = trader_store.list_traders()
    for trader in existing:
        trader_store.remove_trader(trader["wallet"])
    return len(existing)


def main() -> None:
    args = parse_args()
    trader_store.init_db()

    if args.clear_existing and not args.confirm_delete and not args.dry_run:
        print(
            "⚠  --clear-existing benötigt zusätzlich --confirm-delete "
            "(sonst wird nichts gelöscht)."
        )
        return

    print(
        f"Scanne Binance-Leaderboard: pool={args.pool_size}, "
        f"limit={args.limit}, min_day_roi={args.min_day_roi}%, "
        f"min_day_pnl=${args.min_day_pnl:.0f}"
    )
    started = time.time()
    candidates = find_intraday_traders(
        limit=args.limit,
        pool_size=args.pool_size,
        verified_only=True,
        min_day_roi_pct=args.min_day_roi,
        min_day_pnl_usd=args.min_day_pnl,
        require_positive_week=args.min_week_roi >= 0.0,
        require_positive_month=args.min_month_roi >= 0.0,
    )
    candidates = [
        candidate for candidate in candidates
        if (candidate.metrics.week_roi or 0.0) >= args.min_week_roi
        and (candidate.metrics.month_roi or 0.0) >= args.min_month_roi
    ]
    duration = time.time() - started
    print(f"Scan fertig in {duration:.1f}s — {len(candidates)} qualifizierte Trader.")

    if not candidates:
        print("Keine Trader entsprechen den Kriterien.")
        return

    if args.dry_run:
        print("\n[DRY RUN] Diese Trader würden gespeichert:")
    else:
        if args.clear_existing:
            removed = clear_traders()
            print(f"Alte Liste geleert — {removed} Trader entfernt.")

    for index, candidate in enumerate(candidates, 1):
        metrics = candidate.metrics
        line = (
            f"{index:>3}. {candidate.uid} | Score {candidate.quality_score:>5.1f} "
            f"| Tag {metrics.day_roi:+6.1f}%/${metrics.day_pnl:>+7,.0f} "
            f"| 7T {(metrics.week_roi or 0):+5.1f}% "
            f"| 30T {(metrics.month_roi or 0):+5.1f}% "
            f"| {metrics.nick_name or '—'}"
        )
        print(line)
        if args.dry_run:
            continue
        trader_store.upsert_trader(
            candidate.uid,
            is_copied=args.activate,
            source="scanner",
        )
        trader_store.update_trader_stats(
            candidate.uid,
            account_usd=0.0,
            win_rate=metrics.win_rate or 0.0,
            trades=0,
        )
        trader_store.save_verification(candidate.uid, {
            "quality_score": candidate.quality_score,
            "day_roi": metrics.day_roi,
            "day_pnl": metrics.day_pnl,
            "week_roi": metrics.week_roi,
            "week_pnl": metrics.week_pnl,
            "month_roi": metrics.month_roi,
            "month_pnl": metrics.month_pnl,
            "follower_count": metrics.follower_count,
            "position_shared": metrics.position_shared,
            "last_active_age": metrics.last_update_age,
            "metrics_source": "refresh_trader_list",
        })

    if not args.dry_run:
        print(
            f"\nGespeichert: {len(candidates)} Trader "
            f"(is_copied={'1' if args.activate else '0'})."
        )
    if candidates:
        print("Profil-Link (bester Kandidat):")
        print(f"  {binance_leaderboard_url(candidates[0].uid)}")


if __name__ == "__main__":
    main()
