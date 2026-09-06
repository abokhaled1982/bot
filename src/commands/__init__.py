"""
Command-Schnittstelle fuer den Live-Copy-Trader.

Zwei Frontends senden Text-Kommandos hierher:
  * whatsapp_bridge → HTTP-Webhook → server.py
  * Konsole-REPL → stdin.py

Die Handler-Klasse haelt keinen State selbst, sondern ruft Callbacks in
live_copytrader.py auf.
"""
from .handler import CommandHandler, split_command  # noqa: F401
from .server import CommandWebhookServer            # noqa: F401
from .stdin import ConsoleREPL                       # noqa: F401

__all__ = ["CommandHandler", "CommandWebhookServer", "ConsoleREPL", "split_command"]
