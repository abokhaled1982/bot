"""
Interaktive Konsolen-Schnittstelle. Blockt in einem eigenen Thread auf
`input()`, damit der asyncio-Loop nicht gestoert wird.
"""
from __future__ import annotations

import sys
import threading

from loguru import logger

from .handler import CommandHandler, HELP_TEXT


class ConsoleREPL:
    def __init__(self, handler: CommandHandler, prompt: str = "trader> ") -> None:
        self._handler = handler
        self._prompt = prompt
        self._thread: threading.Thread | None = None
        self._stop = threading.Event()

    def start(self) -> None:
        if not sys.stdin or not sys.stdin.isatty():
            logger.info("[REPL] kein TTY — Konsolen-REPL deaktiviert")
            return
        self._thread = threading.Thread(
            target=self._loop, name="cmd-repl", daemon=True,
        )
        self._thread.start()
        print()
        print(HELP_TEXT)
        print()

    def _loop(self) -> None:
        while not self._stop.is_set():
            try:
                line = input(self._prompt)
            except EOFError:
                return
            except KeyboardInterrupt:
                return
            if not line.strip():
                continue
            reply = self._handler.dispatch(line, source="repl")
            if reply:
                print(reply)

    def close(self) -> None:
        self._stop.set()
