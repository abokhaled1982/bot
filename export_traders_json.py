#!/usr/bin/env python3
"""Exportiert gespeicherte Trader aus SQLite in JSON-Dateien."""
from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any

from src.utils import trader_store


BASE_URL = "https://www.binance.com/en/copy-trading/lead-details"


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Gespeicherte Trader aus der DB als JSON exportieren."
    )
    parser.add_argument(
        "--output", default="traders_export.json",
        help="Zentrale JSON-Datei (Standard: traders_export.json).",
    )
    parser.add_argument(
        "--split-dir",
        help="Optional: zusätzlich eine JSON-Datei pro Trader in diesem Ordner.",
    )
    parser.add_argument(
        "--copied-only", action="store_true",
        help="Nur Trader mit is_copied=1 exportieren.",
    )
    return parser.parse_args()


def trader_json(trader: dict[str, Any]) -> dict[str, Any]:
    wallet = str(trader["wallet"])
    verification = trader_store.get_verification(wallet)
    return {
        **trader,
        "trader_id": wallet,
        "trader_profile_url": f"{BASE_URL}/{wallet}?timeRange=30D",
        "trader_positions_url": f"{BASE_URL}/{wallet}?tab=Positions",
        "trader_history_url": f"{BASE_URL}/{wallet}?tab=TradeHistory",
        "verification": verification,
    }


def write_json(path: Path, data: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(data, indent=2, ensure_ascii=True) + "\n",
        encoding="utf-8",
    )


def main() -> None:
    args = parse_args()
    trader_store.init_db()
    traders = trader_store.list_traders()
    if args.copied_only:
        traders = [trader for trader in traders if trader["is_copied"]]

    exported = [trader_json(trader) for trader in traders]
    output = Path(args.output)
    write_json(output, {
        "exported_at": __import__("datetime").datetime.now().astimezone().isoformat(),
        "count": len(exported),
        "traders": exported,
    })
    print(f"Exportiert: {len(exported)} Trader -> {output.resolve()}")

    if args.split_dir:
        split_dir = Path(args.split_dir)
        split_dir.mkdir(parents=True, exist_ok=True)
        for trader in exported:
            write_json(split_dir / f"{trader['trader_id']}.json", trader)
        print(f"Einzeldateien: {len(exported)} -> {split_dir.resolve()}")

    if not exported:
        print("Keine Trader in der DB gefunden.")


if __name__ == "__main__":
    main()
