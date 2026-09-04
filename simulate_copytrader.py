#!/usr/bin/env python3
"""
simulate_copytrader.py — Paper-Copy-Trader auf Basis des Live-Signal-Adapters.

Verwendet denselben `BinanceLeaderboardTrader` wie `monitor_traders.py` und
reagiert auf dessen Copy-Signale:

  COPY_OPEN_LONG  → oeffnet Paper-Position auf Binance-Spot-Preis
  COPY_CLOSE_LONG → schliesst zugehoerige Paper-Position
  (INCREASE / DECREASE / *_SHORT werden ignoriert)

Trader-Auswahl kommt aus `traders_export.json` (`is_copied == 1` UND
`win_rate >= --min-win-rate`). Die Datei wird periodisch neu eingelesen;
neu hinzukommende Trader werden zusaetzlich beobachtet, entfernte Trader
werden aus der Signalliste genommen und ihre offenen Sim-Positionen zum
aktuellen Spot-Preis geschlossen (`TRADER_UNTRACKED`).

Ausgaben:
  sim_positions_open.json    — offene Paper-Positionen
  sim_positions_closed.json  — geschlossene Paper-Trades (PnL USDT+EUR)
  sim_trader_stats.json      — Trades, Winrate, Gesamt-PnL, Verdict pro Trader

Alte Dateinamen (`sim_positions.json`, `sim_history.json`) werden beim Start
einmalig migriert. Es werden KEINE echten Orders platziert.

Start:
  python3 simulate_copytrader.py
  python3 simulate_copytrader.py --min-win-rate 80 --size-usdt 10 --poll-interval 5
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
import time
from datetime import datetime, timezone
from typing import Any

import requests
from loguru import logger

_REPO_ROOT = os.path.dirname(os.path.abspath(__file__))
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)

# Adapter-Defaults setzen, bevor das Modul geladen wird.
os.environ.setdefault("BNLB_AUTO_DISCOVER", "False")
os.environ.setdefault("BNLB_EMIT_AUTO_SIGNALS", "True")
os.environ["DRY_RUN"] = "True"

from src.adapters.binance_leaderboard import (  # noqa: E402
    BinanceLeaderboardTrader,
    CopySignal,
)

# ── Defaults ──────────────────────────────────────────────────────────────────
DEFAULT_TRADERS_FILE   = "traders_export.json"
DEFAULT_OPEN_FILE      = "sim_positions_open.json"
DEFAULT_CLOSED_FILE    = "sim_positions_closed.json"
DEFAULT_STATS_FILE     = "sim_trader_stats.json"
_LEGACY_OPEN_FILE      = "sim_positions.json"
_LEGACY_CLOSED_FILE    = "sim_history.json"
DEFAULT_POLL           = 5.0
DEFAULT_TRADERS_RELOAD = 15.0
DEFAULT_SIZE_USDT      = 10.0
DEFAULT_MIN_WIN_RATE   = 80.0
DEFAULT_USDT_EUR       = 0.92
DEFAULT_STATUS_EVERY   = 30.0

BINANCE_SPOT_PRICE_URL = "https://api.binance.com/api/v3/ticker/price"
LEADERBOARD_URL_FMT = (
    "https://www.binance.com/en/copy-trading/lead-details/{uid}?timeRange=30D"
)
MAX_HISTORY = 1000


# ── I/O-Helpers ───────────────────────────────────────────────────────────────
def _now_iso() -> str:
    return datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")


def _load_json(path: str, default: Any) -> Any:
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except FileNotFoundError:
        return default
    except Exception as e:
        logger.warning(f"{path} unlesbar ({e}) → Default")
        return default


def _save_json(path: str, data: Any) -> None:
    tmp = f"{path}.tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, ensure_ascii=False)
    os.replace(tmp, path)


def _load_with_fallback(new_path: str, legacy_path: str, default: Any) -> Any:
    if os.path.exists(new_path):
        return _load_json(new_path, default)
    if os.path.exists(legacy_path):
        logger.info(f"[SIM] Migriere {legacy_path} → {new_path}")
        data = _load_json(legacy_path, default)
        try:
            _save_json(new_path, data)
        except Exception as e:
            logger.warning(f"[SIM] Migration nach {new_path} fehlgeschlagen: {e}")
        return data
    return default


# Fehlende Spot-Paare nur einmal loggen.
_MISSING_SPOT_LOGGED: set[str] = set()


def _spot_price(symbol: str) -> float | None:
    try:
        r = requests.get(BINANCE_SPOT_PRICE_URL, params={"symbol": symbol}, timeout=8)
        r.raise_for_status()
        return float(r.json()["price"])
    except Exception as e:
        if symbol not in _MISSING_SPOT_LOGGED:
            _MISSING_SPOT_LOGGED.add(symbol)
            logger.warning(
                f"[SIM] {symbol}: kein Spot-Preis ({e}) — vermutlich "
                f"Futures-only, wird bis Neustart uebersprungen"
            )
        return None


def _trader_url(row: dict | None, uid: str) -> str:
    if row:
        url = row.get("trader_profile_url")
        if url:
            return str(url)
    return LEADERBOARD_URL_FMT.format(uid=uid)


def _load_active_traders(path: str, min_win_rate: float) -> dict[str, dict]:
    """UID → Trader-Row, gefiltert auf is_copied==1 UND win_rate>=min_win_rate."""
    data = _load_json(path, {"traders": []})
    out: dict[str, dict] = {}
    for row in data.get("traders", []):
        uid = str(row.get("wallet") or row.get("trader_id") or "").strip()
        if not uid:
            continue
        try:
            is_copied = int(row.get("is_copied", 0))
        except (TypeError, ValueError):
            is_copied = 0
        if is_copied != 1:
            continue
        try:
            win_rate = float(row.get("win_rate") or 0.0)
        except (TypeError, ValueError):
            win_rate = 0.0
        if win_rate < min_win_rate:
            continue
        out[uid] = row
    return out


# ── Trade-Buchhaltung (reine Funktionen) ──────────────────────────────────────
def _open_position(
    open_positions: list[dict], uid: str, trader_row: dict | None,
    coin: str, symbol: str, entry_price: float, entry_trader: float,
    size_usdt: float,
) -> dict:
    qty = size_usdt / entry_price if entry_price > 0 else 0.0
    pos = {
        "trader_id":          uid,
        "trader_url":         _trader_url(trader_row, uid),
        "trader_win_rate":    float((trader_row or {}).get("win_rate") or 0.0),
        "coin":               coin,
        "symbol":             symbol,
        "side":               "LONG",
        "size_usdt":          size_usdt,
        "entry_price":        entry_price,
        "entry_price_trader": entry_trader,
        "qty":                qty,
        "opened_at":          time.time(),
        "opened_at_iso":      _now_iso(),
    }
    open_positions.append(pos)
    return pos


def _close_position(pos: dict, exit_price: float, reason: str, usdt_eur: float) -> dict:
    entry = float(pos["entry_price"])
    size = float(pos["size_usdt"])
    pnl_pct = ((exit_price / entry) - 1.0) * 100 if entry > 0 else 0.0
    pnl_usdt = size * pnl_pct / 100
    return {
        **pos,
        "exit_price":    exit_price,
        "pnl_pct":       round(pnl_pct, 4),
        "pnl_usdt":      round(pnl_usdt, 4),
        "pnl_eur":       round(pnl_usdt * usdt_eur, 4),
        "closed_at":     time.time(),
        "closed_at_iso": _now_iso(),
        "close_reason":  reason,
    }


def _rebuild_stats(history: list[dict]) -> list[dict]:
    agg: dict[str, dict] = {}
    for h in history:
        uid = str(h.get("trader_id") or "")
        if not uid:
            continue
        s = agg.setdefault(uid, {
            "trader_id":  uid,
            "trader_url": h.get("trader_url", ""),
            "trades": 0, "wins": 0, "losses": 0,
            "pnl_usdt": 0.0, "pnl_eur": 0.0,
        })
        s["trader_url"] = h.get("trader_url", s["trader_url"])
        s["trades"] += 1
        pnl = float(h.get("pnl_usdt") or 0.0)
        if pnl > 0:
            s["wins"] += 1
        elif pnl < 0:
            s["losses"] += 1
        s["pnl_usdt"] = round(s["pnl_usdt"] + pnl, 4)
        s["pnl_eur"]  = round(s["pnl_eur"] + float(h.get("pnl_eur") or 0.0), 4)
    for s in agg.values():
        s["win_rate"] = (
            round(100.0 * s["wins"] / s["trades"], 2) if s["trades"] else 0.0
        )
        s["verdict"] = (
            "HELPFUL" if s["pnl_usdt"] > 0
            else "HURTFUL" if s["pnl_usdt"] < 0
            else "NEUTRAL"
        )
    return sorted(agg.values(), key=lambda x: x["pnl_usdt"], reverse=True)


def _persist_open(args: argparse.Namespace, open_positions: list[dict]) -> None:
    _save_json(args.open_file, open_positions)


def _persist_closed(args: argparse.Namespace, history: list[dict]) -> None:
    _save_json(args.closed_file, history[-MAX_HISTORY:])
    _save_json(args.stats_file, _rebuild_stats(history))


# ── Signal-Verarbeitung ───────────────────────────────────────────────────────
def _handle_open(
    sig: CopySignal, traders: dict[str, dict],
    open_positions: list[dict], args: argparse.Namespace,
) -> bool:
    if any(p["trader_id"] == sig.trader and p["coin"] == sig.coin
           for p in open_positions):
        return False
    price = _spot_price(sig.symbol)
    if price is None or price <= 0:
        return False
    row = traders.get(sig.trader)
    _open_position(
        open_positions, sig.trader, row, sig.coin, sig.symbol,
        price, float(sig.entry_price or 0.0), args.size_usdt,
    )
    wr = float((row or {}).get("win_rate") or 0.0)
    logger.info(
        f"[SIM] 📈 OPEN LONG {sig.coin} | Trader {sig.trader} "
        f"(winrate {wr:.1f}%) | entry ${price:.6f} | ${args.size_usdt:.2f} USDT"
    )
    return True


def _handle_close(
    sig: CopySignal, open_positions: list[dict], history: list[dict],
    args: argparse.Namespace, reason: str = "TRADER_CLOSED",
) -> bool:
    match = next(
        (p for p in open_positions
         if p["trader_id"] == sig.trader and p["coin"] == sig.coin),
        None,
    )
    if match is None:
        return False
    price = _spot_price(match["symbol"])
    if price is None or price <= 0:
        return False
    closed = _close_position(match, price, reason, args.usdt_eur_rate)
    history.append(closed)
    open_positions.remove(match)
    logger.info(
        f"[SIM] 🔴 CLOSE LONG {closed['coin']} | Trader {closed['trader_id']} "
        f"| exit ${price:.6f} | PnL {closed['pnl_usdt']:+.2f} USDT "
        f"({closed['pnl_eur']:+.2f} EUR) | {reason}"
    )
    return True


def _close_untracked(
    active_uids: set[str], open_positions: list[dict], history: list[dict],
    args: argparse.Namespace,
) -> int:
    closed = 0
    for pos in list(open_positions):
        if pos["trader_id"] in active_uids:
            continue
        price = _spot_price(pos["symbol"])
        if price is None:
            continue
        rec = _close_position(pos, price, "TRADER_UNTRACKED", args.usdt_eur_rate)
        history.append(rec)
        open_positions.remove(pos)
        closed += 1
        logger.warning(
            f"[SIM] CLOSE {rec['coin']} | Trader {rec['trader_id']} inaktiv "
            f"| exit ${price:.6f} | PnL {rec['pnl_usdt']:+.2f} USDT "
            f"({rec['pnl_eur']:+.2f} EUR)"
        )
    return closed


def _apply_traders_to_adapter(
    adapter: BinanceLeaderboardTrader, traders: dict[str, dict],
) -> None:
    adapter._auto_uids = list(traders.keys())
    adapter._rebuild_uid_list()


# ── Async-Runner ──────────────────────────────────────────────────────────────
async def _traders_reload_loop(
    adapter: BinanceLeaderboardTrader, state: dict, args: argparse.Namespace,
) -> None:
    while True:
        await asyncio.sleep(args.traders_reload)
        try:
            traders = await asyncio.to_thread(
                _load_active_traders, args.traders_file, args.min_win_rate,
            )
        except Exception as e:
            logger.error(f"[SIM] traders_export.json Reload-Fehler: {e}")
            continue
        prev = set(state["traders"].keys())
        curr = set(traders.keys())
        added = curr - prev
        dropped = prev - curr
        state["traders"] = traders
        _apply_traders_to_adapter(adapter, traders)
        if added or dropped:
            logger.info(
                f"[SIM] Trader-Liste aktualisiert | +{len(added)} / -{len(dropped)} "
                f"| aktiv: {len(traders)}"
            )
        if dropped:
            closed = await asyncio.to_thread(
                _close_untracked, curr, state["positions"],
                state["history"], args,
            )
            if closed:
                _persist_open(args, state["positions"])
                _persist_closed(args, state["history"])


async def _signal_loop(
    adapter: BinanceLeaderboardTrader, state: dict, args: argparse.Namespace,
) -> None:
    while True:
        sig: CopySignal = await adapter.signal_queue.get()
        if sig.trader not in state["traders"]:
            continue
        changed = False
        if sig.signal == "COPY_OPEN_LONG":
            changed = await asyncio.to_thread(
                _handle_open, sig, state["traders"], state["positions"], args,
            )
        elif sig.signal == "COPY_CLOSE_LONG":
            changed = await asyncio.to_thread(
                _handle_close, sig, state["positions"], state["history"], args,
            )
        if changed:
            _persist_open(args, state["positions"])
            if state["history"]:
                _persist_closed(args, state["history"])


async def _status_loop(state: dict, interval: float) -> None:
    while True:
        logger.info(
            f"[SIM] Status | offen: {len(state['positions'])} | "
            f"Historie: {len(state['history'])} | aktive Trader: "
            f"{len(state['traders'])}"
        )
        await asyncio.sleep(interval)


async def run(args: argparse.Namespace) -> None:
    state: dict[str, Any] = {
        "positions": _load_with_fallback(args.open_file, _LEGACY_OPEN_FILE, []),
        "history":   _load_with_fallback(args.closed_file, _LEGACY_CLOSED_FILE, []),
        "traders":   {},
    }

    if not os.path.exists(args.open_file):
        _save_json(args.open_file, state["positions"])
    if not os.path.exists(args.closed_file):
        _save_json(args.closed_file, state["history"])
    if not os.path.exists(args.stats_file) or state["history"]:
        _save_json(args.stats_file, _rebuild_stats(state["history"]))

    traders = _load_active_traders(args.traders_file, args.min_win_rate)
    state["traders"] = traders
    logger.info(
        f"[SIM] Geladen: {len(state['positions'])} offene Positionen, "
        f"{len(state['history'])} Trade-Historie, "
        f"{len(traders)} aktive Trader "
        f"(is_copied=1 & win_rate >= {args.min_win_rate}%)"
    )
    if not traders:
        logger.warning(
            "[SIM] Keine aktiven Trader — pruefe is_copied=1 und win_rate "
            "in traders_export.json oder senke --min-win-rate."
        )

    adapter = BinanceLeaderboardTrader(publish_state=False)
    adapter.set_poll_interval(args.poll_interval)
    _apply_traders_to_adapter(adapter, traders)

    adapter_task = asyncio.create_task(adapter.start())
    reload_task  = asyncio.create_task(_traders_reload_loop(adapter, state, args))
    signal_task  = asyncio.create_task(_signal_loop(adapter, state, args))
    status_task  = (
        asyncio.create_task(_status_loop(state, args.status_interval))
        if args.show_status else None
    )
    tasks = [adapter_task, reload_task, signal_task]
    if status_task:
        tasks.append(status_task)
    try:
        await asyncio.gather(*tasks)
    except asyncio.CancelledError:
        pass
    finally:
        for t in tasks:
            t.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)


# ── CLI ───────────────────────────────────────────────────────────────────────
def _parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Copy-Trade Simulator (Live-Signal-Adapter)")
    p.add_argument("--traders-file",    default=DEFAULT_TRADERS_FILE)
    p.add_argument("--open-file",       default=DEFAULT_OPEN_FILE)
    p.add_argument("--closed-file",     default=DEFAULT_CLOSED_FILE)
    p.add_argument("--stats-file",      default=DEFAULT_STATS_FILE)
    p.add_argument("--poll-interval",   type=float, default=DEFAULT_POLL,
                   help="Adapter-Poll-Intervall in Sekunden")
    p.add_argument("--traders-reload",  type=float, default=DEFAULT_TRADERS_RELOAD,
                   help="Intervall fuer erneutes Lesen von traders_export.json")
    p.add_argument("--size-usdt",       type=float, default=DEFAULT_SIZE_USDT)
    p.add_argument("--min-win-rate",    type=float, default=DEFAULT_MIN_WIN_RATE)
    p.add_argument("--usdt-eur-rate",   type=float, default=DEFAULT_USDT_EUR)
    p.add_argument("--show-status",     action="store_true")
    p.add_argument("--status-interval", type=float, default=DEFAULT_STATUS_EVERY)
    return p.parse_args()


def main() -> int:
    args = _parse_args()

    logger.remove()
    logger.add(sys.stderr, level="INFO", format=(
        "<green>{time:HH:mm:ss}</green> | <level>{level: <7}</level> | {message}"
    ))
    logger.info(
        f"[SIM] Start | cwd={os.getcwd()} "
        f"size={args.size_usdt} USDT minWR={args.min_win_rate}% "
        f"poll={args.poll_interval}s reload={args.traders_reload}s "
        f"(1 USDT ≈ {args.usdt_eur_rate:.2f} EUR)"
    )
    logger.info(f"[SIM] Files: traders={os.path.abspath(args.traders_file)}")
    logger.info(f"[SIM]        open   ={os.path.abspath(args.open_file)}")
    logger.info(f"[SIM]        closed ={os.path.abspath(args.closed_file)}")
    logger.info(f"[SIM]        stats  ={os.path.abspath(args.stats_file)}")

    if not os.path.exists(args.traders_file):
        logger.error(
            f"[SIM] traders_export.json nicht gefunden unter "
            f"{os.path.abspath(args.traders_file)} — bitte zuerst "
            f"find_traders.py --append-to ausfuehren."
        )
        return 2

    try:
        asyncio.run(run(args))
    except KeyboardInterrupt:
        logger.info("[SIM] beendet")
    return 0


if __name__ == "__main__":
    sys.exit(main())
