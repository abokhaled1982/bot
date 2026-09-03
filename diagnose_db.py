#!/usr/bin/env python3
"""Diagnose der Trader-DB. Zeigt Pfad, Tabellen, Anzahl und erste Zeilen."""
from __future__ import annotations

import os
import sqlite3
import sys
from pathlib import Path

DEFAULT_DB = "binance_orderflow.db"


def find_dbs() -> list[Path]:
    here = Path.cwd()
    return sorted(here.rglob("*.db"))


def inspect(db_path: Path) -> None:
    print(f"\n=== DB: {db_path.resolve()} ===")
    if not db_path.exists():
        print("  ⚠  Datei existiert NICHT.")
        return
    size = db_path.stat().st_size
    print(f"  Größe: {size} bytes")
    try:
        conn = sqlite3.connect(str(db_path))
    except sqlite3.Error as exc:
        print(f"  Fehler beim Öffnen: {exc}")
        return

    tables = [row[0] for row in conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' ORDER BY name"
    ).fetchall()]
    print(f"  Tabellen: {tables or '— keine —'}")

    if "copy_traders" not in tables:
        print("  ⚠  Tabelle 'copy_traders' fehlt.")
        conn.close()
        return

    total = conn.execute("SELECT COUNT(*) FROM copy_traders").fetchone()[0]
    copied = conn.execute(
        "SELECT COUNT(*) FROM copy_traders WHERE is_copied=1"
    ).fetchone()[0]
    focus = conn.execute(
        "SELECT COUNT(*) FROM copy_traders WHERE is_focus=1"
    ).fetchone()[0]
    print(f"  copy_traders: total={total}, is_copied=1: {copied}, is_focus=1: {focus}")

    if total > 0:
        rows = conn.execute(
            "SELECT wallet, is_copied, is_focus, source "
            "FROM copy_traders ORDER BY updated_at DESC LIMIT 5"
        ).fetchall()
        print("  Erste 5 Trader:")
        for wallet, is_copied, is_focus, source in rows:
            print(
                f"    - {wallet}  copied={is_copied}  focus={is_focus}  source={source}"
            )
    conn.close()


def main() -> None:
    print(f"CWD: {Path.cwd()}")
    print(f"Python: {sys.executable}")
    print(f"BOT_DB_PATH env: {os.getenv('BOT_DB_PATH', '(nicht gesetzt)')}")

    env_path = os.getenv("BOT_DB_PATH", DEFAULT_DB)
    default_path = Path(env_path)
    inspect(default_path)

    print("\n--- Suche nach weiteren .db-Dateien unter dem CWD ---")
    found = [p for p in find_dbs() if p.name.endswith(".db")]
    if not found:
        print("  Keine .db-Dateien im aktuellen Ordner gefunden.")
        return
    for path in found:
        if path.resolve() == default_path.resolve():
            continue
        inspect(path)


if __name__ == "__main__":
    main()
