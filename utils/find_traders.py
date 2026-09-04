#!/usr/bin/env python3
"""
find_traders.py — Finde profitable Intraday-Trader auf Binance (Futures-Leaderboard)

Nutzung:
    python3 find_traders.py                       # Top-Intraday-Trader (Standard-Filter)
    python3 find_traders.py --limit 10             # Nur Top 10
    python3 find_traders.py --all                  # Auch nicht-verifizierte Kandidaten zeigen

    # Alle Trader mit Winrate >= 80% suchen und in traders_export.json ergaenzen
    # (existierende Wallets werden NICHT ueberschrieben und NICHT dupliziert):
    python3 find_traders.py --min-win-rate 80 --min-day-roi 0 --min-day-pnl 0 \\
        --pool-size 2000 --limit 500 --append-to traders_export.json

Keine API-Keys noetig. Nutzt Binances oeffentliches (inoffizielles) Leaderboard.
"""
from __future__ import annotations

import argparse
import json
import time
from datetime import datetime
from pathlib import Path
from typing import Any

from src.adapters.binance_leaderboard import (
    binance_leaderboard_url, find_intraday_traders,
)

_LEADERBOARD_BASE = "https://www.binance.com/en/copy-trading/lead-details"


def _candidate_to_export_row(candidate: Any, activate: bool) -> dict[str, Any]:
    """Kandidat in dieselbe Struktur wie export_traders_json bringen."""
    uid = str(candidate.uid)
    m = candidate.metrics
    now = time.time()
    return {
        "wallet":       uid,
        "size_usdt":    None,
        "is_copied":    1 if activate else 0,
        "is_focus":     0,
        "note":         "",
        "source":       "scanner",
        "account_usd":  0.0,
        "win_rate":     float(m.win_rate or 0.0),
        "trades":       0,
        "added_at":     now,
        "updated_at":   now,
        "trader_id":    uid,
        "trader_profile_url":   f"{_LEADERBOARD_BASE}/{uid}?timeRange=30D",
        "trader_positions_url": f"{_LEADERBOARD_BASE}/{uid}?tab=Positions",
        "trader_history_url":   f"{_LEADERBOARD_BASE}/{uid}?tab=TradeHistory",
        "verification": {
            "wallet":       uid,
            "verified_at":  now,
            "quality_score": float(candidate.quality_score),
            "metrics": {
                "quality_score":   float(candidate.quality_score),
                "day_roi":         m.day_roi,
                "day_pnl":         m.day_pnl,
                "week_roi":        m.week_roi,
                "week_pnl":        m.week_pnl,
                "month_roi":       m.month_roi,
                "month_pnl":       m.month_pnl,
                "win_rate":        m.win_rate,
                "follower_count":  m.follower_count,
                "position_shared": m.position_shared,
                "last_active_age": m.last_update_age,
                "metrics_source":  "find_traders --append-to",
            },
        },
    }


def _append_to_export(path: Path, candidates: list[Any], activate: bool) -> tuple[int, int]:
    """Neue Kandidaten in traders_export.json ergaenzen. Kein Ueberschreiben.

    Return: (added, skipped_duplicates)
    """
    if path.exists():
        try:
            existing = json.loads(path.read_text(encoding="utf-8"))
        except Exception as e:
            raise SystemExit(f"⚠  {path} unlesbar ({e}) — Abbruch, keine Aenderungen.")
    else:
        existing = {"exported_at": "", "count": 0, "traders": []}

    traders: list[dict[str, Any]] = list(existing.get("traders") or [])
    known_wallets = {
        str(t.get("wallet") or t.get("trader_id") or "").strip()
        for t in traders
    }
    known_wallets.discard("")

    added = 0
    skipped = 0
    for candidate in candidates:
        uid = str(candidate.uid)
        if uid in known_wallets:
            skipped += 1
            continue
        traders.append(_candidate_to_export_row(candidate, activate))
        known_wallets.add(uid)
        added += 1

    existing["traders"] = traders
    existing["count"] = len(traders)
    existing["exported_at"] = datetime.now().astimezone().isoformat()

    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(
        json.dumps(existing, indent=2, ensure_ascii=True) + "\n",
        encoding="utf-8",
    )
    tmp.replace(path)
    return added, skipped


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--limit", type=int, default=20, help="Max. Anzahl Trader")
    parser.add_argument("--all", action="store_true",
                         help="Auch nicht-verifizierte Kandidaten anzeigen")
    parser.add_argument("--pool-size", type=int, default=None,
                         help="Kandidaten-Pool beim Scan (Default 2000)")
    parser.add_argument("--min-day-roi", type=float, default=None,
                         help="Mindest-Tages-ROI in %%")
    parser.add_argument("--min-day-pnl", type=float, default=None,
                         help="Mindest-Tages-PnL in USD")
    parser.add_argument("--min-win-rate", type=float, default=None,
                         help="Mindest-Win-Rate in Prozent")
    parser.add_argument("--append-to", metavar="PATH",
                         help="Neue Trader in diese traders_export.json-Datei "
                              "ergaenzen (Duplikate anhand wallet werden "
                              "uebersprungen).")
    parser.add_argument("--activate", action="store_true",
                         help="Neu ergaenzte Trader mit is_copied=1 speichern "
                              "(nur mit --append-to).")
    args = parser.parse_args()

    kwargs: dict[str, Any] = {"limit": args.limit, "verified_only": not args.all}
    if args.pool_size is not None:
        kwargs["pool_size"] = args.pool_size
    if args.min_day_roi is not None:
        kwargs["min_day_roi_pct"] = args.min_day_roi
    if args.min_day_pnl is not None:
        kwargs["min_day_pnl_usd"] = args.min_day_pnl
    if args.min_win_rate is not None:
        kwargs["min_win_rate_pct"] = args.min_win_rate

    print("\n🏆 Binance Futures-Leaderboard — Top Intraday-Trader")
    print("=" * 70)
    print("\nLade Daten von binance.com ...")

    candidates = find_intraday_traders(**kwargs)

    if not candidates:
        print("Keine Trader gefunden, die die Filterkriterien erfuellen.")
        return

    print(f"\n{'Rang':<6} {'Score':<7} {'Tages-ROI':<11} {'Tages-PnL':<12} "
          f"{'Win-Rate':<10} {'7T-ROI':<9} {'30T-ROI':<9} {'Follower':<9} {'Name'}")
    print(f"{'-'*6} {'-'*7} {'-'*11} {'-'*12} {'-'*10} {'-'*9} "
          f"{'-'*9} {'-'*9} {'-'*20}")

    for i, c in enumerate(candidates, 1):
        m = c.metrics
        week = f"{m.week_roi:+.1f}%" if m.week_roi is not None else "—"
        month = f"{m.month_roi:+.1f}%" if m.month_roi is not None else "—"
        win_rate = f"{m.win_rate:.1f}%" if m.win_rate is not None else "—"
        print(f"#{i:<5} {c.quality_score:<7.1f} {m.day_roi:>+9.1f}% "
              f"${m.day_pnl:>+9,.0f} {win_rate:<10} {week:<9} {month:<9} "
              f"{m.follower_count:<9} "
              f"{m.nick_name}")

    print("\n💡 Tipp: Profil pruefen:")
    print(f"   {binance_leaderboard_url(candidates[0].uid)}")
    print("\n💡 Tipp: UID in .env eintragen, um dauerhaft zu tracken:")
    print("   BNLB_TRADER_UIDS=uid1,uid2")

    if args.append_to:
        path = Path(args.append_to)
        added, skipped = _append_to_export(path, candidates, args.activate)
        print(
            f"\n📝 {path}: +{added} neue Trader ergaenzt, "
            f"{skipped} bereits vorhanden (uebersprungen). "
            f"is_copied={'1' if args.activate else '0'} bei neuen Eintraegen."
        )


if __name__ == "__main__":
    main()
