#!/usr/bin/env python3
"""
live_copytrader.py — Live-Copy-Trader mit WhatsApp-Benachrichtigung.

Baut auf denselben Signal-Adapter wie `simulate_copytrader.py`, ersetzt aber
die Paper-Buchhaltung durch echte Orders via `src.execution.real_executor`
und schickt jedes Event ueber `src.notifications.WhatsAppNotifier` raus.

Trader-Auswahl:  data/traders_export.json  (is_copied=1 UND win_rate>=--min-win-rate)
Positionen:      data/live_positions_open.json / live_positions_closed.json
                 live_trader_stats.json — pro Trader Gewinne/Verluste

Signal-Behandlung:
  COPY_OPEN_LONG  → market BUY + OCO (Stop-Loss / Take-Profit) auf Binance-Spot,
                    Position im JSON-Store, WhatsApp-Push.
  COPY_CLOSE_LONG → market SELL der offenen Menge, PnL berechnen, WhatsApp-Push.

Risiko-Gates (harte No-Go's):
  * `STOP_BOT`-Datei blockt alle neuen Opens (Closes weiterhin erlaubt).
  * Maximal --max-positions offen.
  * Realisierter Tages-Loss > --max-daily-loss-usd → keine neuen Opens mehr.
  * Kontostand < --min-balance-usdt → keine neuen Opens.
  * Order fehlgeschlagen → sofortige Warn-Msg auf WhatsApp.

Dieses Skript platziert ECHTE ORDERS wenn `DRY_RUN=False`. Standard ist
`DRY_RUN=True` (dann simuliert der Executor die Antwort und loggt).
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import signal
import sys
import threading
import time
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from typing import Any, Optional

from loguru import logger

_REPO_ROOT = os.path.dirname(os.path.abspath(__file__))
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)

os.environ.setdefault("BNLB_AUTO_DISCOVER", "False")
os.environ.setdefault("BNLB_EMIT_AUTO_SIGNALS", "True")

from src.adapters.binance_leaderboard import (  # noqa: E402
    BinanceLeaderboardTrader, CopySignal,
)
from src.commands import (  # noqa: E402
    CommandHandler, CommandWebhookServer, ConsoleREPL,
)
from src.commands.server import default_webhook_token  # noqa: E402
from src.execution import real_executor as ex  # noqa: E402
from src.notifications import get_notifier  # noqa: E402


# ── Defaults ──────────────────────────────────────────────────────────────────
DEFAULT_TRADERS_FILE   = "data/traders_export.json"
DEFAULT_OPEN_FILE      = "data/live_positions_open.json"
DEFAULT_CLOSED_FILE    = "data/live_positions_closed.json"
DEFAULT_STATS_FILE     = "data/live_trader_stats.json"
DEFAULT_POLL           = 3.0
DEFAULT_TRADERS_RELOAD = 15.0
DEFAULT_SIZE_USDT      = 20.0
DEFAULT_MIN_WIN_RATE   = 80.0
DEFAULT_MIN_COPY_USD   = 50.0
DEFAULT_USDT_EUR       = 0.92
DEFAULT_MAX_POS        = 5
DEFAULT_MAX_DAILY_LOSS = 30.0
DEFAULT_MIN_BALANCE    = 15.0
STOP_FILE              = "STOP_BOT"
MAX_HISTORY            = 1000


# ── I/O ───────────────────────────────────────────────────────────────────────
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
    parent = os.path.dirname(path)
    if parent:
        os.makedirs(parent, exist_ok=True)
    tmp = f"{path}.tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, ensure_ascii=False)
    os.replace(tmp, path)


def _load_active_traders(path: str, min_win_rate: float) -> dict[str, dict]:
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


# ── Position-Struktur & Statistik ─────────────────────────────────────────────
@dataclass
class Position:
    trader_id:       str
    trader_win_rate: float
    coin:            str
    symbol:          str
    side:            str
    size_usdt:       float
    entry_price:     float
    entry_price_trader: float
    qty:             float
    order_id:        Optional[str] = None
    client_id:       Optional[str] = None
    opened_at:       float = 0.0
    opened_at_iso:   str = ""


def _rebuild_stats(history: list[dict]) -> list[dict]:
    agg: dict[str, dict] = {}
    for h in history:
        uid = str(h.get("trader_id") or "")
        if not uid:
            continue
        s = agg.setdefault(uid, {
            "trader_id": uid,
            "trades": 0, "wins": 0, "losses": 0,
            "pnl_usdt": 0.0, "pnl_eur": 0.0,
        })
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


def _daily_realized_pnl(history: list[dict]) -> float:
    today = datetime.now(timezone.utc).astimezone().date()
    total = 0.0
    for h in history:
        iso = h.get("closed_at_iso") or ""
        try:
            d = datetime.fromisoformat(iso).astimezone().date()
        except Exception:
            continue
        if d == today:
            total += float(h.get("pnl_usdt") or 0.0)
    return total


def _persist(state: dict, args: argparse.Namespace) -> None:
    with state["lock"]:
        _save_json(args.open_file,   list(state["positions"]))
        _save_json(args.closed_file, list(state["history"][-MAX_HISTORY:]))
        _save_json(args.stats_file,  _rebuild_stats(state["history"]))


# ── Risiko-Gates ──────────────────────────────────────────────────────────────
def _blocked_reason(state: dict, args: argparse.Namespace) -> str:
    if os.path.exists(STOP_FILE):
        return f"STOP_BOT-Datei vorhanden ({STOP_FILE})"
    if len(state["positions"]) >= args.max_positions:
        return f"max positions ({args.max_positions}) erreicht"
    day_pnl = _daily_realized_pnl(state["history"])
    if day_pnl <= -abs(args.max_daily_loss_usd):
        return f"tagesloss {day_pnl:+.2f} USDT ueberschritten"
    return ""


# ── Signal-Handler ────────────────────────────────────────────────────────────
def _handle_open(
    sig: CopySignal, traders: dict[str, dict], state: dict,
    args: argparse.Namespace, notifier,
) -> bool:
    with state["lock"]:
        exists = any(p["trader_id"] == sig.trader and p["coin"] == sig.coin
                     for p in state["positions"])
    if exists:
        return False
    reason = _blocked_reason(state, args)
    if reason:
        logger.warning(f"[LIVE] OPEN {sig.coin} geblockt: {reason}")
        notifier.send(f"⛔ OPEN {sig.coin} geblockt: {reason}")
        return False

    balance = ex.get_account_balance("USDT")
    if balance < max(args.size_usdt, args.min_balance_usdt):
        msg = (f"⛔ OPEN {sig.coin} geblockt: Balance ${balance:.2f} USDT < "
               f"min ${args.min_balance_usdt:.2f}")
        logger.warning(msg)
        notifier.send(msg)
        return False

    buy, oco = ex.buy_and_protect(
        sig.symbol, args.size_usdt,
        trader=sig.trader, coin=sig.coin,
        price_hint=float(sig.entry_price or 0.0) or None,
    )
    if not buy.ok or buy.qty <= 0:
        msg = f"❌ OPEN {sig.coin} fehlgeschlagen: {buy.reason} (code {buy.error_code})"
        logger.error(msg)
        notifier.send(msg)
        return False

    row = traders.get(sig.trader) or {}
    pos = Position(
        trader_id=sig.trader,
        trader_win_rate=float(row.get("win_rate") or 0.0),
        coin=sig.coin, symbol=sig.symbol, side="LONG",
        size_usdt=buy.total_usdt, entry_price=buy.price,
        entry_price_trader=float(sig.entry_price or 0.0),
        qty=buy.qty, order_id=buy.order_id, client_id=buy.client_id,
        opened_at=time.time(), opened_at_iso=_now_iso(),
    )
    with state["lock"]:
        state["positions"].append(asdict(pos))

    oco_note = ""
    if oco is not None:
        oco_note = " OCO ✔" if oco.ok else f" OCO ✖ ({oco.reason})"
    msg = (
        f"📈 OPEN LONG {sig.coin}\n"
        f"Trader win-rate: {pos.trader_win_rate:.1f}%\n"
        f"Qty: {buy.qty:.6f} {info_base(sig.symbol)}\n"
        f"Entry: ${buy.price:.6f}\n"
        f"Notional: ${buy.total_usdt:.2f} USDT{oco_note}"
    )
    logger.success(f"[LIVE] {msg}")
    notifier.send(msg)
    return True


def _handle_close(
    sig: CopySignal, state: dict, args: argparse.Namespace, notifier,
    reason_tag: str = "TRADER_CLOSED",
) -> bool:
    with state["lock"]:
        match = next(
            (p for p in state["positions"]
             if p["trader_id"] == sig.trader and p["coin"] == sig.coin),
            None,
        )
    if match is None:
        return False

    sell = ex.market_sell(match["symbol"], match["qty"],
                          trader=match["trader_id"], coin=match["coin"])
    if not sell.ok or sell.qty <= 0:
        msg = f"❌ CLOSE {sig.coin} fehlgeschlagen: {sell.reason}"
        logger.error(msg)
        notifier.send(msg)
        return False

    entry = float(match["entry_price"])
    size  = float(match["size_usdt"])
    exit_notional = sell.total_usdt or (sell.qty * sell.price)
    pnl_usdt = exit_notional - size
    pnl_pct  = (pnl_usdt / size * 100) if size > 0 else 0.0
    pnl_eur  = pnl_usdt * args.usdt_eur_rate

    closed = {
        **match,
        "exit_price":    sell.price,
        "pnl_pct":       round(pnl_pct, 4),
        "pnl_usdt":      round(pnl_usdt, 4),
        "pnl_eur":       round(pnl_eur, 4),
        "closed_at":     time.time(),
        "closed_at_iso": _now_iso(),
        "close_reason":  reason_tag,
    }
    with state["lock"]:
        state["history"].append(closed)
        if match in state["positions"]:
            state["positions"].remove(match)

    emoji = "🟢" if pnl_usdt >= 0 else "🔴"
    msg = (
        f"{emoji} CLOSE {sig.coin} ({reason_tag})\n"
        f"Entry: ${entry:.6f} → Exit: ${sell.price:.6f}\n"
        f"PnL: {pnl_usdt:+.2f} USDT ({pnl_pct:+.2f}%) | {pnl_eur:+.2f} EUR"
    )
    logger.log("SUCCESS" if pnl_usdt >= 0 else "ERROR", f"[LIVE] {msg}")
    notifier.send(msg)
    return True


def _close_untracked(active_uids: set[str], state: dict,
                     args: argparse.Namespace, notifier) -> int:
    n = 0
    for pos in list(state["positions"]):
        if pos["trader_id"] in active_uids:
            continue
        pseudo = CopySignal(
            trader=pos["trader_id"], coin=pos["coin"], symbol=pos["symbol"],
            signal="COPY_CLOSE_LONG", size_usd=pos["size_usdt"],
            entry_price=pos["entry_price"], leverage=1.0, pnl_pct=0.0,
        )
        if _handle_close(pseudo, state, args, notifier, reason_tag="TRADER_UNTRACKED"):
            n += 1
    return n


# ── Manuelle Aktionen (Kommandos) ─────────────────────────────────────────────
def _open_manual(
    coin: str, symbol: str, usdt: float, trader_id: str, win_rate: float,
    *, state: dict, args: argparse.Namespace, notifier,
) -> str:
    with state["lock"]:
        exists = any(
            p["trader_id"] == trader_id and p["coin"] == coin
            for p in state["positions"]
        )
    if exists:
        return f"ℹ️ Position {coin} [{trader_id[:8]}] existiert bereits."

    if trader_id != "MANUAL":
        reason = _blocked_reason(state, args)
        if reason:
            return f"⛔ OPEN {coin} geblockt: {reason}"
    elif os.path.exists(STOP_FILE):
        return f"⛔ STOP_BOT gesetzt — MANUAL-Open nur nach /resume."

    balance = ex.get_account_balance("USDT")
    if balance < max(usdt, args.min_balance_usdt):
        return (f"⛔ OPEN {coin} geblockt: Balance ${balance:.2f} USDT < "
                f"${max(usdt, args.min_balance_usdt):.2f}")

    buy, oco = ex.buy_and_protect(symbol, usdt, trader=trader_id, coin=coin)
    if not buy.ok or buy.qty <= 0:
        return f"❌ OPEN {coin} fehlgeschlagen: {buy.reason} (code {buy.error_code})"

    pos = Position(
        trader_id=trader_id,
        trader_win_rate=float(win_rate or 0.0),
        coin=coin, symbol=symbol, side="LONG",
        size_usdt=buy.total_usdt, entry_price=buy.price,
        entry_price_trader=0.0,
        qty=buy.qty, order_id=buy.order_id, client_id=buy.client_id,
        opened_at=time.time(), opened_at_iso=_now_iso(),
    )
    with state["lock"]:
        state["positions"].append(asdict(pos))
    _persist(state, args)

    oco_note = ""
    if oco is not None:
        oco_note = " OCO ✔" if oco.ok else f" OCO ✖ ({oco.reason})"
    msg = (
        f"📈 OPEN LONG {coin} [{trader_id[:8]}]\n"
        f"Qty: {buy.qty:.6f} {info_base(symbol)}\n"
        f"Entry: ${buy.price:.6f}\n"
        f"Notional: ${buy.total_usdt:.2f} USDT{oco_note}"
    )
    logger.success(f"[LIVE] {msg}")
    return msg


def _close_by_coin(
    coin: str, trader_id: Optional[str], reason_tag: str,
    *, state: dict, args: argparse.Namespace, notifier,
) -> str:
    with state["lock"]:
        matches = [
            p for p in state["positions"]
            if p["coin"] == coin and (trader_id is None or p["trader_id"] == trader_id)
        ]
    if not matches:
        who = f" [{trader_id}]" if trader_id else ""
        return f"ℹ️ keine offene Position {coin}{who}."

    lines: list[str] = []
    for match in matches:
        pseudo = CopySignal(
            trader=match["trader_id"], coin=match["coin"], symbol=match["symbol"],
            signal="COPY_CLOSE_LONG", size_usd=match["size_usdt"],
            entry_price=match["entry_price"], leverage=1.0, pnl_pct=0.0,
        )
        ok = _handle_close(pseudo, state, args, notifier, reason_tag=reason_tag)
        if ok:
            lines.append(f"✅ {coin} [{match['trader_id'][:8]}] geschlossen")
        else:
            lines.append(f"❌ {coin} [{match['trader_id'][:8]}] fehlgeschlagen")
    _persist(state, args)
    return "\n".join(lines)


def _cmd_get_price(symbol: str) -> Optional[float]:
    try:
        return ex.get_price(symbol)
    except Exception:
        return None


def _cmd_get_balance(asset: str) -> float:
    try:
        return ex.get_account_balance(asset)
    except Exception:
        return 0.0


def info_base(symbol: str) -> str:
    return symbol.replace("USDT", "").replace("USD", "").replace("BUSD", "")


def _apply_traders_to_adapter(
    adapter: BinanceLeaderboardTrader, traders: dict[str, dict],
) -> None:
    adapter._auto_uids = list(traders.keys())
    adapter._rebuild_uid_list()


# ── Async-Loops ───────────────────────────────────────────────────────────────
async def _traders_reload_loop(adapter, state, args, notifier) -> None:
    while True:
        await asyncio.sleep(args.traders_reload)
        try:
            traders = await asyncio.to_thread(
                _load_active_traders, args.traders_file, args.min_win_rate,
            )
        except Exception as e:
            logger.error(f"[LIVE] traders_export.json Reload-Fehler: {e}")
            continue
        prev = set(state["traders"].keys())
        curr = set(traders.keys())
        state["traders"] = traders
        _apply_traders_to_adapter(adapter, traders)
        added, dropped = curr - prev, prev - curr
        if added or dropped:
            logger.info(
                f"[LIVE] Trader-Liste aktualisiert +{len(added)} / -{len(dropped)} "
                f"| aktiv: {len(traders)}"
            )
            if dropped:
                closed = await asyncio.to_thread(
                    _close_untracked, curr, state, args, notifier,
                )
                if closed:
                    _persist(state, args)


async def _signal_loop(adapter, state, args, notifier) -> None:
    while True:
        sig: CopySignal = await adapter.signal_queue.get()
        if sig.trader not in state["traders"]:
            continue
        changed = False
        if sig.signal == "COPY_OPEN_LONG":
            changed = await asyncio.to_thread(
                _handle_open, sig, state["traders"], state, args, notifier,
            )
        elif sig.signal == "COPY_CLOSE_LONG":
            changed = await asyncio.to_thread(
                _handle_close, sig, state, args, notifier,
            )
        if changed:
            _persist(state, args)


async def _status_loop(state, notifier, interval: float) -> None:
    while True:
        await asyncio.sleep(interval)
        day_pnl = _daily_realized_pnl(state["history"])
        stats = _rebuild_stats(state["history"])[:3]
        try:
            balance = await asyncio.to_thread(ex.get_account_balance, "USDT")
        except Exception:
            balance = 0.0
        top = "\n".join(
            f"  · {s['trader_id'][:6]}… {s['pnl_usdt']:+.2f}$ ({s['trades']}t/{s['win_rate']:.0f}%)"
            for s in stats
        ) or "  (noch keine geschlossenen Trades)"
        msg = (
            f"📊 Status\n"
            f"Balance: ${balance:.2f} USDT\n"
            f"Offen: {len(state['positions'])} | Historie: {len(state['history'])}\n"
            f"PnL heute: {day_pnl:+.2f} USDT\n"
            f"Top Trader:\n{top}"
        )
        logger.info(f"[LIVE] {msg}")
        notifier.send(msg)


# ── Runner ────────────────────────────────────────────────────────────────────
async def run(args: argparse.Namespace) -> None:
    for path in (args.open_file, args.closed_file, args.stats_file):
        parent = os.path.dirname(path)
        if parent:
            os.makedirs(parent, exist_ok=True)

    state: dict[str, Any] = {
        "positions": _load_json(args.open_file, []),
        "history":   _load_json(args.closed_file, []),
        "traders":   {},
        "lock":      threading.RLock(),
    }
    for f in (args.open_file, args.closed_file):
        if not os.path.exists(f):
            _save_json(f, [])
    _save_json(args.stats_file, _rebuild_stats(state["history"]))

    notifier = get_notifier()

    live = os.getenv("DRY_RUN", "True").lower() != "true"
    mode = "LIVE" if live else "DRY-RUN"
    logger.info(
        f"[LIVE] Start {mode} | size=${args.size_usdt:.2f} minWR={args.min_win_rate:.0f}% "
        f"minCopy=${args.min_copy_size_usd:.0f} poll={args.poll_interval:.1f}s "
        f"maxPos={args.max_positions} maxDailyLoss=${args.max_daily_loss_usd:.2f}"
    )
    balance = ex.get_account_balance("USDT")
    logger.info(f"[LIVE] Balance: ${balance:.2f} USDT")
    notifier.send(f"🤖 Copy-Trader gestartet ({mode})\nBalance: ${balance:.2f} USDT")

    traders = _load_active_traders(args.traders_file, args.min_win_rate)
    state["traders"] = traders
    logger.info(f"[LIVE] {len(traders)} aktive Trader "
                f"(is_copied=1 & win_rate>={args.min_win_rate:.0f}%)")

    adapter = BinanceLeaderboardTrader(publish_state=False)
    adapter.set_poll_interval(args.poll_interval)
    adapter.set_min_copy_size(args.min_copy_size_usd)
    _apply_traders_to_adapter(adapter, traders)

    cmd_handler = CommandHandler(
        state_provider=lambda: state,
        traders_provider=lambda: state["traders"],
        open_manual=lambda coin, sym, usdt, trader, wr: _open_manual(
            coin, sym, usdt, trader, wr,
            state=state, args=args, notifier=notifier,
        ),
        close_position=lambda coin, trader, reason: _close_by_coin(
            coin, trader, reason,
            state=state, args=args, notifier=notifier,
        ),
        get_price=_cmd_get_price,
        get_balance=_cmd_get_balance,
        default_size_usdt=args.size_usdt,
    )
    webhook = None
    if args.webhook_port > 0:
        webhook = CommandWebhookServer(
            cmd_handler, notifier,
            port=args.webhook_port,
            token=args.webhook_token or default_webhook_token(),
            allow_from=[x.strip() for x in (args.webhook_allow_from or "").split(",") if x.strip()],
        )
        try:
            webhook.start()
        except OSError:
            webhook = None
    repl = None
    if not args.no_repl:
        repl = ConsoleREPL(cmd_handler)
        repl.start()

    tasks = [
        asyncio.create_task(adapter.start()),
        asyncio.create_task(_traders_reload_loop(adapter, state, args, notifier)),
        asyncio.create_task(_signal_loop(adapter, state, args, notifier)),
    ]
    if args.status_interval > 0:
        tasks.append(asyncio.create_task(
            _status_loop(state, notifier, args.status_interval)
        ))

    loop = asyncio.get_event_loop()
    for s in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(s, lambda: [t.cancel() for t in tasks])
        except NotImplementedError:
            pass

    try:
        await asyncio.gather(*tasks)
    except asyncio.CancelledError:
        pass
    finally:
        for t in tasks:
            t.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        if repl is not None:
            repl.close()
        if webhook is not None:
            webhook.close()
        notifier.send("🛑 Copy-Trader gestoppt")
        notifier.close()


# ── CLI ───────────────────────────────────────────────────────────────────────
def _parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Live Copy-Trader mit WhatsApp-Push")
    p.add_argument("--traders-file",     default=DEFAULT_TRADERS_FILE)
    p.add_argument("--open-file",        default=DEFAULT_OPEN_FILE)
    p.add_argument("--closed-file",      default=DEFAULT_CLOSED_FILE)
    p.add_argument("--stats-file",       default=DEFAULT_STATS_FILE)
    p.add_argument("--poll-interval",    type=float, default=DEFAULT_POLL)
    p.add_argument("--traders-reload",   type=float, default=DEFAULT_TRADERS_RELOAD)
    p.add_argument("--size-usdt",        type=float, default=DEFAULT_SIZE_USDT)
    p.add_argument("--min-win-rate",     type=float, default=DEFAULT_MIN_WIN_RATE)
    p.add_argument("--min-copy-size-usd", type=float, default=DEFAULT_MIN_COPY_USD)
    p.add_argument("--usdt-eur-rate",    type=float, default=DEFAULT_USDT_EUR)
    p.add_argument("--max-positions",    type=int,   default=DEFAULT_MAX_POS)
    p.add_argument("--max-daily-loss-usd", type=float, default=DEFAULT_MAX_DAILY_LOSS)
    p.add_argument("--min-balance-usdt", type=float, default=DEFAULT_MIN_BALANCE)
    p.add_argument("--status-interval",  type=float, default=1800.0,
                   help="Sekunden zwischen Status-Push (0 = aus)")
    p.add_argument("--webhook-port",     type=int, default=3100,
                   help="Port fuer Command-Webhook (0 = aus)")
    p.add_argument("--webhook-token",    default="",
                   help="Bearer-Token, sonst aus ENV LIVE_WEBHOOK_TOKEN")
    p.add_argument("--webhook-allow-from", default="",
                   help="CSV WhatsApp-Absender-Praefixe (zB '49123'); leer = alle")
    p.add_argument("--no-repl",          action="store_true",
                   help="Konsolen-Kommandozeile abschalten")
    return p.parse_args()


def main() -> int:
    args = _parse_args()
    logger.remove()
    logger.add(sys.stderr, level="INFO", format=(
        "<green>{time:HH:mm:ss}</green> | <level>{level: <7}</level> | {message}"
    ))
    logger.add("logs/live_copytrader.log", rotation="10 MB", retention=5, level="DEBUG")

    if not os.path.exists(args.traders_file):
        logger.error(
            f"[LIVE] {os.path.abspath(args.traders_file)} nicht gefunden — "
            f"bitte utils/find_traders.py --append-to zuerst ausfuehren."
        )
        return 2
    try:
        asyncio.run(run(args))
    except KeyboardInterrupt:
        logger.info("[LIVE] beendet")
    return 0


if __name__ == "__main__":
    sys.exit(main())
