#!/usr/bin/env python3
"""
mock_trades.py — Fake-Trade-Simulator fuer die WhatsApp-Bridge.

KEIN Binance-Zugriff. KEIN echter Trade. Zweck: den kompletten
Notification-Pfad (Notifier → Bridge → WhatsApp) end-to-end pruefen,
indem eine realistische Sequenz aus Start, OPEN, CLOSE, Status,
Fehlermeldung und Shutdown durchgespielt wird.

Beispiele:
  python3 mock_trades.py                    # ganze Sequenz an $WHATSAPP_TO
  python3 mock_trades.py --to 4917xxxxxxxx  # anderer Empfaenger
  python3 mock_trades.py --to <chan>@newsletter   # in einen WhatsApp-Kanal
  python3 mock_trades.py --scenario one-open       # nur ein OPEN + CLOSE
  python3 mock_trades.py --interval 0.5             # schneller
  python3 mock_trades.py --dry-run                  # nur ausgeben, nichts senden
"""
from __future__ import annotations

import argparse
import os
import random
import sys
import time

_REPO_ROOT = os.path.dirname(os.path.abspath(__file__))
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)

from loguru import logger  # noqa: E402

from src.notifications.whatsapp import (  # noqa: E402
    NullNotifier, WhatsAppNotifier,
)


COIN_UNIVERSE = [
    ("BTC",  "BTCUSDT",  60_000.0),
    ("ETH",  "ETHUSDT",   3_200.0),
    ("SOL",  "SOLUSDT",     150.0),
    ("BNB",  "BNBUSDT",     580.0),
    ("XRP",  "XRPUSDT",       0.62),
    ("DOGE", "DOGEUSDT",      0.16),
]


def _fmt_open(coin: str, symbol: str, entry: float, size_usdt: float, trader: str, wr: float) -> str:
    qty = size_usdt / entry
    base = symbol.replace("USDT", "")
    oco  = " OCO ✔"
    tag  = f"{trader[:8]}" if trader != "MANUAL" else "MANUAL"
    return (
        f"📈 OPEN LONG {coin} [{tag}]\n"
        f"Trader win-rate: {wr:.1f}%\n"
        f"Qty: {qty:.6f} {base}\n"
        f"Entry: ${entry:.6f}\n"
        f"Notional: ${size_usdt:.2f} USDT{oco}"
    )


def _fmt_close(coin: str, entry: float, exit_: float, size_usdt: float,
               trader: str, reason: str) -> str:
    qty = size_usdt / entry
    exit_notional = qty * exit_
    pnl_usdt = exit_notional - size_usdt
    pnl_pct  = (pnl_usdt / size_usdt) * 100
    pnl_eur  = pnl_usdt * 0.92
    emoji    = "🟢" if pnl_usdt >= 0 else "🔴"
    tag      = f"{trader[:8]}" if trader != "MANUAL" else "MANUAL"
    return (
        f"{emoji} CLOSE {coin} [{tag}] ({reason})\n"
        f"Entry: ${entry:.6f} → Exit: ${exit_:.6f}\n"
        f"PnL: {pnl_usdt:+.2f} USDT ({pnl_pct:+.2f}%) | {pnl_eur:+.2f} EUR"
    )


def _fmt_status(balance: float, open_cnt: int, day_pnl: float, hist_cnt: int) -> str:
    return (
        f"📊 Status  (✅ aktiv)\n"
        f"Balance:  ${balance:.2f} USDT\n"
        f"Offen:    {open_cnt}\n"
        f"Historie: {hist_cnt}\n"
        f"PnL heute:  {day_pnl:+.2f} USDT"
    )


class _DryNotifier:
    """Nur Ausgabe auf stdout — kein Netzwerk."""

    def send(self, message: str, to=None) -> None:
        target = to or "(default)"
        print(f"── to={target}\n{message}\n")

    def close(self) -> None: ...
    def health(self) -> dict: return {"ok": True, "dry_run": True}


def _make_notifier(dry: bool, to: str):
    if dry:
        return _DryNotifier()
    n = WhatsAppNotifier(default_to=to or "")
    h = n.health()
    if not h.get("ready"):
        logger.warning(f"[MOCK] Bridge nicht ready: {h}")
    return n


# ── Szenarien ─────────────────────────────────────────────────────────────────
def scenario_full(notifier, interval: float, to: str) -> None:
    notifier.send("🤖 Copy-Trader gestartet (MOCK-TEST)\nBalance: $250.00 USDT", to=to)
    time.sleep(interval)

    positions: list[dict] = []
    rng = random.Random(42)

    # Drei Opens
    for coin, sym, price in rng.sample(COIN_UNIVERSE, 3):
        entry  = price * rng.uniform(0.995, 1.005)
        size   = rng.choice([20.0, 25.0, 30.0])
        trader = rng.choice(["7d3e0f9a1b", "MANUAL", "42c1a8b7ff"])
        wr     = rng.uniform(75, 92)
        positions.append({"coin": coin, "entry": entry, "size": size, "trader": trader})
        notifier.send(_fmt_open(coin, sym, entry, size, trader, wr), to=to)
        time.sleep(interval)

    # Status
    notifier.send(_fmt_status(250.0 - sum(p["size"] for p in positions),
                              len(positions), 0.0, 0), to=to)
    time.sleep(interval)

    # Fehlermeldung, damit du siehst wie Alerts aussehen
    notifier.send("❌ OPEN AVAX fehlgeschlagen: MIN_NOTIONAL 10.0 USDT (code -1013)", to=to)
    time.sleep(interval)

    # Zwei Closes (einer +, einer −)
    closed_pnl = 0.0
    for i, pos in enumerate(positions[:2]):
        drift = 1.02 if i == 0 else 0.985
        exit_ = pos["entry"] * drift
        pnl_usdt = (pos["size"] / pos["entry"]) * exit_ - pos["size"]
        closed_pnl += pnl_usdt
        notifier.send(_fmt_close(pos["coin"], pos["entry"], exit_, pos["size"],
                                 pos["trader"], "TRADER_CLOSED"), to=to)
        time.sleep(interval)

    # Update
    notifier.send(_fmt_status(250.0 - positions[-1]["size"] + closed_pnl,
                              1, closed_pnl, 2), to=to)
    time.sleep(interval)

    notifier.send("🛑 Copy-Trader gestoppt (MOCK-TEST)", to=to)


def scenario_one_open(notifier, interval: float, to: str) -> None:
    coin, sym, price = COIN_UNIVERSE[0]
    entry = price * 1.001
    size  = 20.0
    notifier.send(_fmt_open(coin, sym, entry, size, "MANUAL", 0.0), to=to)
    time.sleep(interval)
    exit_ = entry * 1.015
    notifier.send(_fmt_close(coin, entry, exit_, size, "MANUAL", "MANUAL_SELL"), to=to)


def scenario_status(notifier, _interval: float, to: str) -> None:
    notifier.send(_fmt_status(247.42, 2, +3.11, 12), to=to)


SCENARIOS = {
    "full":     scenario_full,
    "one-open": scenario_one_open,
    "status":   scenario_status,
}


def main() -> int:
    p = argparse.ArgumentParser(description="Fake-Trade-Simulator (kein Binance-Zugriff)")
    p.add_argument("--to",       default=os.getenv("WHATSAPP_TO", ""),
                   help="Empfaenger (Rufnummer, xxxx@c.us, xxxx@g.us, xxxx@newsletter)")
    p.add_argument("--scenario", default="full", choices=sorted(SCENARIOS.keys()))
    p.add_argument("--interval", type=float, default=1.5,
                   help="Sekunden zwischen Nachrichten")
    p.add_argument("--dry-run",  action="store_true",
                   help="Nur auf stdout ausgeben, nichts senden")
    args = p.parse_args()

    if not args.dry_run and not args.to:
        print("❌ Kein --to und $WHATSAPP_TO leer. Nichts zu tun.", file=sys.stderr)
        return 2

    logger.remove()
    logger.add(sys.stderr, level="INFO", format=(
        "<green>{time:HH:mm:ss}</green> | {level: <7} | {message}"
    ))
    logger.info(f"[MOCK] Szenario={args.scenario}  interval={args.interval}s  "
                f"to={args.to or '(dry-run)'}")

    notifier = _make_notifier(args.dry_run, args.to)
    try:
        SCENARIOS[args.scenario](notifier, args.interval, args.to)
        if not args.dry_run:
            time.sleep(max(args.interval, 2.0))   # let queue drain
    finally:
        notifier.close()
    print("\n✅ Sequenz beendet.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
