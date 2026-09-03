#!/usr/bin/env python3
"""Gespeicherte Trader aus der Bot-Datenbank anzeigen."""
from __future__ import annotations

import argparse
import concurrent.futures
import json
import threading
import time
from datetime import datetime, timezone
try:
    from zoneinfo import ZoneInfo
except ImportError:
    ZoneInfo = None
from pathlib import Path

from src.utils import trader_store


DEFAULT_EVENT_FILE = "trader_events.json"
_BERLIN = ZoneInfo("Europe/Berlin") if ZoneInfo else None


def _fmt_time(unix_ts: float) -> dict:
    """UTC-ISO, deutscher lesbarer Zeitstempel und Unix."""
    if not unix_ts or unix_ts <= 0:
        return {"unix": 0.0, "utc": "", "berlin": ""}
    dt = datetime.fromtimestamp(unix_ts, tz=timezone.utc)
    berlin = dt.astimezone(_BERLIN).strftime("%Y-%m-%d %H:%M:%S %Z") if _BERLIN else ""
    return {"unix": unix_ts, "utc": dt.isoformat(), "berlin": berlin}


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Gespeicherte Trader aus copy_traders anzeigen."
    )
    parser.add_argument(
        "--copied-only",
        action="store_true",
        help="Nur Trader mit aktiviertem Copy-Trading anzeigen.",
    )
    parser.add_argument(
        "--json",
        action="store_true",
        help="Ergebnis als JSON ausgeben.",
    )
    parser.add_argument(
        "--watch",
        action="store_true",
        help="Alle gespeicherten Trader fortlaufend überwachen.",
    )
    parser.add_argument(
        "--interval",
        type=float,
        default=5.0,
        help="Sekunden zwischen den Überwachungszyklen (Standard: 5).",
    )
    parser.add_argument(
        "--workers",
        type=int,
        default=10,
        help="Anzahl paralleler Überwachungs-Threads (Standard: 10).",
    )
    parser.add_argument(
        "--events-file",
        default=DEFAULT_EVENT_FILE,
        help=f"JSON-Datei für Events (Standard: {DEFAULT_EVENT_FILE}).",
    )
    parser.add_argument(
        "--uids",
        default="",
        help="Nur diese Trader-UIDs überwachen (kommagetrennt).",
    )
    parser.add_argument(
        "--limit",
        type=int,
        default=0,
        help="Maximale Anzahl überwachter Trader (0 = alle).",
    )
    parser.add_argument(
        "--hot-secs",
        type=float,
        default=600.0,
        help="Sekunden, die ein aktiver Trader in der Hot-List bleibt (Standard: 600).",
    )
    parser.add_argument(
        "--hot-interval",
        type=float,
        default=2.0,
        help="Abfrageintervall für Hot-List-Trader (Standard: 2).",
    )
    parser.add_argument(
        "--cold-batch",
        type=int,
        default=20,
        help="Wie viele nicht-heisse Trader pro Zyklus rotierend geprüft werden (Standard: 20).",
    )
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    trader_store.init_db()
    traders = trader_store.list_traders()

    if args.copied_only:
        traders = [trader for trader in traders if trader["is_copied"]]

    if args.uids:
        wanted = {uid.strip() for uid in args.uids.split(",") if uid.strip()}
        traders = [trader for trader in traders if trader["wallet"] in wanted]
        for uid in wanted - {trader["wallet"] for trader in traders}:
            traders.append({"wallet": uid, "is_copied": 0, "is_focus": 0, "source": "cli"})

    if args.limit > 0:
        traders = traders[: args.limit]

    if args.watch:
        watch_traders(
            traders, args.interval, args.workers, args.events_file,
            args.hot_secs, args.hot_interval, args.cold_batch,
        )
        return

    if args.json:
        print(json.dumps(traders, indent=2, ensure_ascii=True))
        return

    if not traders:
        print("Keine gespeicherten Trader gefunden.")
        return

    print(f"Gespeicherte Trader: {len(traders)}")
    for index, trader in enumerate(traders, 1):
        status = "COPY AKTIV" if trader["is_copied"] else "nur gespeichert"
        focus = " | FOKUS" if trader["is_focus"] else ""
        print(
            f"{index}. {trader['wallet']} | {status}{focus} | "
            f"Quelle: {trader['source']}"
        )


def _read_positions(uid: str) -> tuple[str, dict[str, dict]]:
    """Liest einen Trader und normalisiert seine öffentlichen Positionen."""
    from src.adapters.binance_leaderboard import (
        BinanceLeaderboardTrader, fetch_other_positions,
    )

    positions = {}
    for row in fetch_other_positions(uid):
        position = BinanceLeaderboardTrader._parse_position_row(row)
        if position and abs(position["size"]) > 1e-12:
            binance_ms = 0
            for key in ("updateTime", "updateTimeStamp", "modifyTime", "time"):
                val = row.get(key)
                if isinstance(val, (int, float)) and val > binance_ms:
                    binance_ms = val
            if binance_ms:
                position["binance_update_ts"] = binance_ms / 1000.0
            positions[position["coin"]] = position
    return uid, positions


def _event(action: str, uid: str, position: dict, previous: dict | None) -> dict:
    """Erzeugt ein vollständiges, JSON-kompatibles Trade-Event."""
    from src.adapters.binance_leaderboard import binance_leaderboard_url

    now = time.time()
    binance_ts = float(position.get("binance_update_ts") or 0)
    pos_updated = binance_ts if binance_ts > 0 else float(position.get("updated_at", now) or now)
    prev_binance_ts = float(previous.get("binance_update_ts") or 0) if previous else 0.0
    prev_updated = prev_binance_ts if prev_binance_ts > 0 else (float(previous["updated_at"]) if previous else 0.0)
    size_change = position["size"] - previous["size"] if previous else position["size"]
    value_change = position["value_usd"] - previous["value_usd"] if previous else position["value_usd"]

    return {
        "event_id": f"{uid}:{position['coin']}:{time.time_ns()}",
        "trader_id": uid,
        "trader_id_short": f"{uid[:6]}...{uid[-4:]}" if len(uid) > 12 else uid,
        "trader_profile_url": binance_leaderboard_url(uid),
        "trader_positions_url": f"https://www.binance.com/en/copy-trading/lead-details/{uid}?tab=Positions",
        "trader_history_url": f"https://www.binance.com/en/copy-trading/lead-details/{uid}?tab=TradeHistory",
        "binance_symbol_url": f"https://www.binance.com/en/futures/{position['symbol']}",
        "binance_price_chart_url": f"https://www.binance.com/en/trade/{position['symbol']}?type=spot",
        "detected_at": _fmt_time(now),
        "trader_action_at": _fmt_time(binance_ts) if binance_ts > 0 else None,
        "detection_latency_sec": round(now - binance_ts, 3) if binance_ts > 0 else None,
        "poll_gap_sec": round(now - prev_updated, 3) if prev_updated else None,
        "action": action,
        "side": "LONG" if position["size"] > 0 else "SHORT",
        "coin": position["coin"],
        "symbol": position["symbol"],
        "size": position["size"],
        "size_change": size_change,
        "value_usd": position["value_usd"],
        "value_change_usd": value_change,
        "entry_price": position["entry_px"],
        "leverage": position["leverage"],
        "pnl_pct": position["pnl_pct"],
        "unrealized_pnl": position["unrealized_pnl"],
        "previous_position": previous,
    }


def _detect_events(uid: str, old: dict[str, dict], new: dict[str, dict]) -> list[dict]:
    events = []
    for coin, position in new.items():
        previous = old.get(coin)
        if previous is None:
            events.append(_event("OPEN_LONG" if position["size"] > 0 else "OPEN_SHORT", uid, position, None))
        elif position["size"] > 0 and previous["size"] > 0:
            if position["size"] > previous["size"] * 1.05:
                events.append(_event("INCREASE_LONG", uid, position, previous))
            elif position["size"] < previous["size"] * 0.95:
                events.append(_event("DECREASE_LONG", uid, position, previous))
        elif position["size"] * previous["size"] < 0:
            events.append(_event("REVERSE", uid, position, previous))

    for coin, previous in old.items():
        if coin not in new:
            action = "CLOSE_LONG" if previous["size"] > 0 else "CLOSE_SHORT"
            events.append(_event(action, uid, previous, previous))
    return events


def _append_events(path: Path, events: list[dict], lock: threading.Lock) -> None:
    if not events:
        return
    with lock:
        try:
            saved = json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}
            if not isinstance(saved, dict):
                saved = {}
        except (OSError, json.JSONDecodeError):
            saved = {}
        for item in events:
            uid = item["trader_id"]
            bucket = saved.setdefault(uid, {
                "trader_id": uid,
                "trader_id_short": item["trader_id_short"],
                "trader_profile_url": item["trader_profile_url"],
                "trader_positions_url": item["trader_positions_url"],
                "trader_history_url": item["trader_history_url"],
                "first_event_at": item["detected_at"]["berlin"] or item["detected_at"]["utc"],
                "event_count": 0,
                "events": [],
            })
            bucket["last_event_at"] = item["detected_at"]["berlin"] or item["detected_at"]["utc"]
            bucket["last_action"] = item["action"]
            bucket["last_symbol"] = item["symbol"]
            bucket["event_count"] += 1
            bucket["events"].append(item)
        path.write_text(json.dumps(saved, indent=2, ensure_ascii=True), encoding="utf-8")
    for item in events:
        print(
            f"EVENT {item['action']} {item['side']} trader={item['trader_id_short']} "
            f"symbol={item['symbol']} size={item['size']:.8g} "
            f"(Δ{item['size_change']:+.8g}) value=${item['value_usd']:,.0f} "
            f"entry={item['entry_price']:.8g} leverage={item['leverage']:.2f}x "
            f"latency={item['detection_latency_sec']}s\n"
            f"  detected: {item['detected_at']['berlin']}\n"
            f"  profile:  {item['trader_profile_url']}\n"
            f"  chart:    {item['binance_symbol_url']}",
            flush=True,
        )


def watch_traders(
    traders: list[dict], interval: float, workers: int, events_file: str,
    hot_secs: float = 600.0, hot_interval: float = 2.0, cold_batch: int = 20,
) -> None:
    """Überwacht alle gespeicherten Trader bis zum Abbruch mit Ctrl+C."""
    uids = [trader["wallet"] for trader in traders]
    if not uids:
        print("Keine Trader zum Überwachen gefunden.")
        return
    interval = max(0.5, interval)
    workers = max(1, workers)
    path = Path(events_file)
    previous: dict[str, dict[str, dict]] = {}
    write_lock = threading.Lock()
    hot_until: dict[str, float] = {}
    cold_index = 0
    first_cycle = True
    print(
        f"Überwachung gestartet: {len(uids)} Trader, {workers} Threads, "
        f"Basis-Intervall {interval:g}s, Hot-Intervall {hot_interval:g}s, "
        f"Hot-Dauer {hot_secs:g}s, Cold-Batch {cold_batch}, Datei {path}",
        flush=True,
    )
    cycle = 0
    try:
        with concurrent.futures.ThreadPoolExecutor(max_workers=workers) as pool:
            while True:
                cycle += 1
                t0 = time.time()
                now = t0

                if first_cycle:
                    batch = list(uids)
                else:
                    hot = [uid for uid, until in hot_until.items() if until > now]
                    cold_pool = [uid for uid in uids if uid not in set(hot)]
                    take = min(cold_batch, len(cold_pool))
                    if cold_pool and take > 0:
                        end = cold_index + take
                        if end <= len(cold_pool):
                            cold = cold_pool[cold_index:end]
                        else:
                            cold = cold_pool[cold_index:] + cold_pool[: end - len(cold_pool)]
                        cold_index = end % len(cold_pool)
                    else:
                        cold = []
                    batch = hot + cold

                results = pool.map(_read_positions, batch)
                current_batch = dict(results)
                active_batch = sum(1 for positions in current_batch.values() if positions)
                events_this_cycle = 0
                if not first_cycle:
                    for uid, positions in current_batch.items():
                        detected = _detect_events(uid, previous.get(uid, {}), positions)
                        if detected:
                            hot_until[uid] = now + hot_secs
                        events_this_cycle += len(detected)
                        _append_events(path, detected, write_lock)
                for uid, positions in current_batch.items():
                    previous[uid] = positions
                hot_now = sum(1 for until in hot_until.values() if until > now)
                print(
                    f"Zyklus {cycle}: {len(batch)} geprüft in {time.time()-t0:.1f}s, "
                    f"{active_batch} mit offenen Positionen, {events_this_cycle} Events, "
                    f"Hot-List={hot_now}",
                    flush=True,
                )
                first_cycle = False
                sleep_for = hot_interval if hot_until and any(u > time.time() for u in hot_until.values()) else interval
                time.sleep(sleep_for)
    except KeyboardInterrupt:
        print("Überwachung beendet.", flush=True)


if __name__ == "__main__":
    main()