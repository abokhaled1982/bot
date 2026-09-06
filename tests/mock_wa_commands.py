#!/usr/bin/env python3
"""
mock_wa_commands.py — Kommando-Webhook end-to-end testen, ohne echte
WhatsApp-Bridge und ohne DB-/JSON-Schreibvorgaenge.

Startet `CommandWebhookServer` mit einem `MockNotifier` (faengt Antworten
ab statt sie an die Bridge zu senden) und In-Memory-State (keine Positionen/
Historie aus Datei oder DB). `get_price`/`get_balance` bleiben an den echten
`real_executor` angebunden -> Status/Balance/Preis-Kommandos sprechen wirklich
mit Binance. `open_manual`/`close_position` sind Stubs, die einen Fehler
werfen statt zu handeln -> /buy, /sell, /copy etc. loesen NIE einen echten
Trade aus.

Beispiele:
  python3 mock_wa_commands.py                              # Standard-Sequenz
  python3 mock_wa_commands.py --cmd "balance BTC"           # ein Kommando
  python3 mock_wa_commands.py --cmd status --cmd "price ETH"
  python3 mock_wa_commands.py --port 3199                   # abweichender Port
"""
from __future__ import annotations

import argparse
import os
import sys
import time

_REPO_ROOT = os.path.dirname(os.path.abspath(__file__))
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)

import requests  # noqa: E402
from dotenv import load_dotenv  # noqa: E402

load_dotenv(os.path.join(_REPO_ROOT, ".env"))

from src.commands import CommandHandler, CommandWebhookServer  # noqa: E402
import src.execution.real_executor as ex  # noqa: E402

DEFAULT_COMMANDS = [
    "status", "balance USDT", "balance BTC", "price BTC",
    "positions", "following", "help",
]


class MockNotifier:
    """Faengt Nachrichten ab statt sie an die echte WhatsApp-Bridge zu schicken."""

    def __init__(self) -> None:
        self.sent: list[tuple[str | None, str]] = []

    def send(self, message: str, to: str | None = None) -> None:
        self.sent.append((to, message))
        print(f"[MOCK-WA -> {to}] {message}")

    def health(self) -> dict:
        return {"ok": True, "mock": True}


def _open_manual(coin: str, sym: str, usdt: float, trader: str, wr: float) -> str:
    raise AssertionError("Kauf haette in diesem Test NICHT ausgeloest werden duerfen!")


def _close_position(coin: str, trader: str | None, reason: str) -> str:
    raise AssertionError("Close haette in diesem Test NICHT ausgeloest werden duerfen!")


def main() -> None:
    p = argparse.ArgumentParser(description="WhatsApp-Kommandofluss mocken (kein echter Versand/Trade)")
    p.add_argument("--host", default="127.0.0.1")
    p.add_argument("--port", type=int, default=3199)
    p.add_argument("--from-id", default="4915155555555@c.us")
    p.add_argument("--cmd", action="append", dest="cmds", default=None,
                    help="Kommando (mehrfach angebbar). Ohne Angabe: Standard-Sequenz.")
    args = p.parse_args()
    commands = args.cmds or DEFAULT_COMMANDS

    fake_state = {"positions": [], "history": []}
    fake_traders: dict[str, float] = {}

    handler = CommandHandler(
        state_provider=lambda: fake_state,
        traders_provider=lambda: fake_traders,
        open_manual=_open_manual,
        close_position=_close_position,
        get_price=ex.get_price,              # echte Binance-Kommunikation
        get_balance=ex.get_account_balance,  # echte Binance-Kommunikation
        default_size_usdt=10.0,
    )

    notifier = MockNotifier()
    webhook = CommandWebhookServer(handler, notifier, host=args.host, port=args.port, token="")
    webhook.start()
    time.sleep(0.3)

    try:
        for cmd in commands:
            print(f"\n=== simuliere WhatsApp-Nachricht: {cmd!r} ===")
            payload = {
                "from": args.from_id, "body": cmd, "timestamp": time.time(),
                "chatName": "Test", "isGroup": False,
            }
            r = requests.post(f"http://{args.host}:{args.port}/wa", json=payload, timeout=10)
            print("webhook http status:", r.status_code, r.json())
            time.sleep(0.2)

        print("\n=== Zusammenfassung der vom Mock-Notifier abgefangenen Antworten ===")
        for to, msg in notifier.sent:
            print("-----")
            print(msg)
    finally:
        webhook.close()

    print("\nOK: keine Datei/DB wurde durch diesen Test veraendert (nur In-Memory State).")


if __name__ == "__main__":
    main()
