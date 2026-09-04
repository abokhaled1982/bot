#!/usr/bin/env python3
"""
simulate_copytrader.py — Standalone Copy-Trade Simulator (ohne Dashboard).

Liest `traders_export.json` bei jedem Poll neu ein und spiegelt LONG-Positionen
der markierten Trader als Paper-Trades wider. Preise kommen live vom
Binance-Spot-REST-API. Es werden KEINE echten Orders platziert.

Auswahl-Logik pro Trader-Eintrag in `traders_export.json`:
  aktiv  ⇔  is_copied == 1  UND  win_rate >= --min-win-rate

Wird ein Trader aus der JSON entfernt oder `is_copied` auf 0 gesetzt,
werden alle seine offenen Simulations-Positionen im nächsten Tick zum
aktuellen Spot-Preis geschlossen (Grund `TRADER_UNTRACKED`).

Ausgaben:
  sim_positions_open.json    — aktuell offene Paper-Positionen
  sim_positions_closed.json  — geschlossene Paper-Trades (Historie mit PnL USDT+EUR)
  sim_trader_stats.json      — Status pro Trader: Trades, Winrate, Gesamt-PnL, Verdict

Alte Dateinamen (`sim_positions.json`, `sim_history.json`) werden beim Start
automatisch übernommen, wenn die neuen noch nicht existieren.

Start:
  python3 simulate_copytrader.py
  python3 simulate_copytrader.py --min-win-rate 80 --size-usdt 10 --poll-interval 5
"""
from __future__ import annotations

import argparse
import json
import os
import signal
import sys
import time
from datetime import datetime, timezone
from typing import Any

import requests
from loguru import logger

# Repo-Root in sys.path, damit `src.*` gefunden wird, egal von wo aufgerufen.
_REPO_ROOT = os.path.dirname(os.path.abspath(__file__))
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)

from src.adapters.binance_leaderboard import (  # noqa: E402
    fetch_other_positions,
    futures_symbol_to_spot_symbol,
)

# ── Defaults ──────────────────────────────────────────────────────────────────
DEFAULT_TRADERS_FILE   = "traders_export.json"
DEFAULT_OPEN_FILE      = "sim_positions_open.json"
DEFAULT_CLOSED_FILE    = "sim_positions_closed.json"
DEFAULT_STATS_FILE     = "sim_trader_stats.json"
_LEGACY_OPEN_FILE      = "sim_positions.json"
_LEGACY_CLOSED_FILE    = "sim_history.json"
DEFAULT_POLL           = 5.0
DEFAULT_SIZE_USDT      = 10.0
DEFAULT_MIN_WIN_RATE   = 80.0
DEFAULT_USDT_EUR       = 0.92

BINANCE_SPOT_PRICE_URL = "https://api.binance.com/api/v3/ticker/price"
LEADERBOARD_URL_FMT = (
    "https://www.binance.com/en/copy-trading/lead-details/{uid}?timeRange=30D"
)
MAX_HISTORY = 1000


# ── Helpers ───────────────────────────────────────────────────────────────────
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
    """Neue Datei bevorzugen; sonst altes Format \u00fcbernehmen (einmalige Migration)."""
    if os.path.exists(new_path):
        return _load_json(new_path, default)
    if os.path.exists(legacy_path):
        logger.info(f"[SIM] Migriere {legacy_path} \u2192 {new_path}")
        data = _load_json(legacy_path, default)
        try:
            _save_json(new_path, data)
        except Exception as e:
            logger.warning(f"[SIM] Migration nach {new_path} fehlgeschlagen: {e}")
        return data
    return default


# Symbole ohne Spot-Paar nur einmal loggen, sonst \u00fcberschwemmt es die Ausgabe.
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
                f"[SIM] {symbol}: kein Spot-Preis ({e}) \u2014 wahrscheinlich "
                f"Futures-only, wird bis Neustart uebersprungen"
            )
        return None


def _trader_url(row: dict, uid: str) -> str:
    return str(row.get("trader_profile_url") or LEADERBOARD_URL_FMT.format(uid=uid))


def _load_traders(path: str, min_win_rate: float) -> dict[str, dict]:
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


def _parse_open_longs(rows: list[dict]) -> dict[str, dict]:
    """Public-Position-Rows → coin → {symbol, entry_px_trader}. Nur LONG (>0)."""
    out: dict[str, dict] = {}
    for row in rows:
        raw_symbol = str(row.get("symbol") or "").upper()
        if not raw_symbol:
            continue
        spot_symbol = futures_symbol_to_spot_symbol(raw_symbol)
        coin = spot_symbol.replace("USDT", "")
        try:
            amount = float(row.get("amount", row.get("positionAmount", 0)) or 0)
        except (TypeError, ValueError):
            amount = 0.0
        side = str(row.get("positionSide") or "").upper()
        if side == "SHORT" and amount > 0:
            amount = -amount
        elif side == "LONG" and amount < 0:
            amount = -amount
        if amount <= 0:
            continue  # short/flat → auf Spot nicht abbildbar
        try:
            entry_trader = float(row.get("entryPrice", 0) or 0)
        except (TypeError, ValueError):
            entry_trader = 0.0
        out[coin] = {"symbol": spot_symbol, "entry_px_trader": entry_trader}
    return out


def _open_position(
    positions: list[dict], uid: str, trader_row: dict, coin: str, symbol: str,
    entry_price: float, entry_trader: float, size_usdt: float,
) -> dict:
    qty = size_usdt / entry_price if entry_price > 0 else 0.0
    pos = {
        "trader_id":        uid,
        "trader_url":       _trader_url(trader_row, uid),
        "trader_win_rate":  float(trader_row.get("win_rate") or 0.0),
        "coin":             coin,
        "symbol":           symbol,
        "side":             "LONG",
        "size_usdt":        size_usdt,
        "entry_price":      entry_price,
        "entry_price_trader": entry_trader,
        "qty":              qty,
        "opened_at":        time.time(),
        "opened_at_iso":    _now_iso(),
    }
    positions.append(pos)
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
        s["pnl_eur"]  = round(s["pnl_eur"]  + float(h.get("pnl_eur") or 0.0), 4)
    for s in agg.values():
        s["win_rate"] = (
            round(100.0 * s["wins"] / s["trades"], 2) if s["trades"] else 0.0
        )
        s["verdict"] = (
            "HELPFUL" if s["pnl_usdt"] > 0
            else "HURTFUL" if s["pnl_usdt"] < 0
            else "NEUTRAL"
        )
    # sortiert nach Gesamt-PnL absteigend — beste Trader oben
    return sorted(agg.values(), key=lambda x: x["pnl_usdt"], reverse=True)


# ── Kern-Tick ─────────────────────────────────────────────────────────────────
def _persist_open(args: argparse.Namespace, open_positions: list[dict]) -> None:
    _save_json(args.open_file, open_positions)


def _persist_closed(args: argparse.Namespace, history: list[dict]) -> None:
    _save_json(args.closed_file, history[-MAX_HISTORY:])
    _save_json(args.stats_file, _rebuild_stats(history))


def _tick(state: dict, args: argparse.Namespace) -> None:
    traders = _load_traders(args.traders_file, args.min_win_rate)
    open_positions: list[dict] = state["positions"]
    history: list[dict] = state["history"]
    opened = closed_cnt = trader_actions = 0
    logger.info(
        f"[SIM] Tick start | {len(traders)} aktive Trader | "
        f"{len(open_positions)} Sim-Positionen aktuell offen"
    )

    # 1) Positionen von deaktivierten/entfernten Tradern schließen
    active_uids = set(traders.keys())
    for pos in list(open_positions):
        if pos["trader_id"] in active_uids:
            continue
        price = _spot_price(pos["symbol"])
        if price is None:
            logger.warning(
                f"[SIM] kein Preis für {pos['symbol']} — halte "
                f"Position von {pos['trader_id']} offen"
            )
            continue
        closed = _close_position(pos, price, "TRADER_UNTRACKED", args.usdt_eur_rate)
        history.append(closed)
        open_positions.remove(pos)
        closed_cnt += 1
        _persist_open(args, open_positions)
        _persist_closed(args, history)
        logger.warning(
            f"[SIM] CLOSE {closed['coin']} | Trader {closed['trader_id']} inaktiv "
            f"| exit ${price:.6f} | PnL {closed['pnl_usdt']:+.2f} USDT "
            f"({closed['pnl_eur']:+.2f} EUR)"
        )

    # 2) Diff pro aktivem Trader
    for uid, row in traders.items():
        try:
            rows = fetch_other_positions(uid)
        except Exception as e:
            logger.error(f"[SIM] fetch {uid}: {e}")
            continue
        trader_longs = _parse_open_longs(rows)
        trader_actions += len(trader_longs)
        ours = {p["coin"]: p for p in open_positions if p["trader_id"] == uid}

        # a) Coin nicht mehr in Trader-Positionen → schließen
        for coin, pos in list(ours.items()):
            if coin in trader_longs:
                continue
            price = _spot_price(pos["symbol"])
            if price is None:
                continue
            closed = _close_position(pos, price, "TRADER_CLOSED", args.usdt_eur_rate)
            history.append(closed)
            open_positions.remove(pos)
            closed_cnt += 1
            _persist_open(args, open_positions)
            _persist_closed(args, history)
            logger.info(
                f"[SIM] 🔴 CLOSE LONG {coin} | Trader {uid} closed "
                f"| exit ${price:.6f} | PnL {closed['pnl_usdt']:+.2f} USDT "
                f"({closed['pnl_eur']:+.2f} EUR)"
            )

        # b) Neuer Coin beim Trader → öffnen
        for coin, info in trader_longs.items():
            if coin in ours:
                continue
            symbol = info["symbol"]
            price = _spot_price(symbol)
            if price is None or price <= 0:
                continue
            _open_position(
                open_positions, uid, row, coin, symbol,
                price, info["entry_px_trader"], args.size_usdt,
            )
            opened += 1
            _persist_open(args, open_positions)
            logger.info(
                f"[SIM] 📈 OPEN LONG {coin} | Trader {uid} "
                f"(winrate {row.get('win_rate', 0):.1f}%) "
                f"| entry ${price:.6f} | ${args.size_usdt:.2f} USDT"
            )

    # 3) Abschluss: Zusammenfassung (Dateien wurden inkrementell geschrieben)
    _persist_open(args, open_positions)
    if history:
        _persist_closed(args, history)
    logger.info(
        f"[SIM] Tick done | {len(traders)} aktive Trader | "
        f"{trader_actions} Trader-Longs gesehen | "
        f"+{opened} neu geoeffnet, -{closed_cnt} geschlossen | "
        f"jetzt {len(open_positions)} Sim-Positionen offen"
    )


# ── Main ──────────────────────────────────────────────────────────────────────
def main() -> int:
    p = argparse.ArgumentParser(description="Copy-Trade Simulator (ohne Dashboard)")
    p.add_argument("--traders-file",  default=DEFAULT_TRADERS_FILE)
    p.add_argument("--open-file",     default=DEFAULT_OPEN_FILE,
                   help="Ziel-JSON fuer offene Sim-Positionen "
                        f"(Default {DEFAULT_OPEN_FILE})")
    p.add_argument("--closed-file",   default=DEFAULT_CLOSED_FILE,
                   help="Ziel-JSON fuer geschlossene Sim-Trades "
                        f"(Default {DEFAULT_CLOSED_FILE})")
    p.add_argument("--stats-file",    default=DEFAULT_STATS_FILE)
    p.add_argument("--poll-interval", type=float, default=DEFAULT_POLL)
    p.add_argument("--size-usdt",     type=float, default=DEFAULT_SIZE_USDT)
    p.add_argument("--min-win-rate",  type=float, default=DEFAULT_MIN_WIN_RATE)
    p.add_argument("--usdt-eur-rate", type=float, default=DEFAULT_USDT_EUR)
    args = p.parse_args()

    logger.remove()
    logger.add(sys.stderr, level="INFO", format=(
        "<green>{time:HH:mm:ss}</green> | <level>{level: <7}</level> | {message}"
    ))
    logger.info(
        f"[SIM] Start | cwd={os.getcwd()} "
        f"size={args.size_usdt} USDT minWR={args.min_win_rate}% "
        f"poll={args.poll_interval}s (1 USDT ≈ {args.usdt_eur_rate:.2f} EUR)"
    )
    logger.info(
        f"[SIM] Files: traders={os.path.abspath(args.traders_file)}"
    )
    logger.info(
        f"[SIM]        open   ={os.path.abspath(args.open_file)}"
    )
    logger.info(
        f"[SIM]        closed ={os.path.abspath(args.closed_file)}"
    )
    logger.info(
        f"[SIM]        stats  ={os.path.abspath(args.stats_file)}"
    )

    if not os.path.exists(args.traders_file):
        logger.error(
            f"[SIM] traders_export.json nicht gefunden unter "
            f"{os.path.abspath(args.traders_file)} — "
            f"bitte zuerst find_traders.py --append-to ausfuehren."
        )
        return 2

    state = {
        "positions": _load_with_fallback(args.open_file, _LEGACY_OPEN_FILE, []),
        "history":   _load_with_fallback(args.closed_file, _LEGACY_CLOSED_FILE, []),
    }

    traders_now = _load_traders(args.traders_file, args.min_win_rate)
    logger.info(
        f"[SIM] Geladen: {len(state['positions'])} offene Positionen, "
        f"{len(state['history'])} Trade-Historie, "
        f"{len(traders_now)} aktive Trader "
        f"(is_copied=1 & win_rate>={args.min_win_rate}%)"
    )
    if not traders_now:
        logger.warning(
            "[SIM] Keine aktiven Trader — pruefe is_copied=1 und win_rate in "
            "traders_export.json oder senke --min-win-rate."
        )
    if state["history"]:
        _save_json(args.stats_file, _rebuild_stats(state["history"]))

    running = True

    def _stop(*_: object) -> None:
        nonlocal running
        running = False
        logger.info("[SIM] Signal empfangen — offene Positionen bleiben bestehen")

    signal.signal(signal.SIGINT, _stop)
    signal.signal(signal.SIGTERM, _stop)

    while running:
        try:
            _tick(state, args)
        except Exception as e:
            logger.exception(f"[SIM] Tick-Fehler: {e}")
        end = time.time() + args.poll_interval
        while running and time.time() < end:
            time.sleep(0.5)

    return 0


if __name__ == "__main__":
    sys.exit(main())
