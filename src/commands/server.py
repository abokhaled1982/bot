"""
Kleiner HTTP-Server, den die Node-Bridge per Webhook anspricht.

Bindet nur auf 127.0.0.1. Erwartet POST /wa mit JSON
`{ from, body, timestamp, chatName, isGroup }` — genau die Payload,
die whatsapp_bridge/server.js verschickt.

Antwort auf WhatsApp geht nicht ueber die HTTP-Response zurueck, sondern
ueber den `notifier` — so kann der Text der Bridge unabhaengig zugestellt
werden und der Webhook bleibt „fire-and-forget".
"""
from __future__ import annotations

import json
import os
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Optional

from loguru import logger

from .handler import CommandHandler


class CommandWebhookServer:
    def __init__(
        self,
        handler: CommandHandler,
        notifier,
        *,
        host: str = "127.0.0.1",
        port: int = 3100,
        token: str = "",
        allow_from: Optional[list[str]] = None,
    ) -> None:
        self._handler = handler
        self._notifier = notifier
        self._host = host
        self._port = int(port)
        self._token = token or ""
        self._allow = [x for x in (allow_from or []) if x]
        self._server: Optional[ThreadingHTTPServer] = None
        self._thread: Optional[threading.Thread] = None

    def start(self) -> None:
        outer = self

        class Req(BaseHTTPRequestHandler):
            def log_message(self, fmt, *args):
                return  # ruhig lassen — Loguru macht Logs

            def _json(self, status: int, payload: dict) -> None:
                body = json.dumps(payload).encode("utf-8")
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def do_GET(self):
                if self.path == "/health":
                    self._json(200, {"ok": True})
                    return
                self._json(404, {"ok": False, "error": "not found"})

            def do_POST(self):
                if outer._token:
                    got = (self.headers.get("Authorization") or "").removeprefix("Bearer ").strip()
                    if got != outer._token:
                        self._json(401, {"ok": False, "error": "unauthorized"})
                        return
                if self.path not in ("/wa", "/webhook"):
                    self._json(404, {"ok": False, "error": "not found"})
                    return
                try:
                    length = int(self.headers.get("Content-Length") or "0")
                    raw = self.rfile.read(length) if length else b"{}"
                    data = json.loads(raw.decode("utf-8") or "{}")
                except Exception as e:
                    self._json(400, {"ok": False, "error": f"bad json: {e}"})
                    return

                from_id = str(data.get("from") or "").strip()
                body = str(data.get("body") or "").strip()
                if outer._allow and not any(from_id.startswith(x) for x in outer._allow):
                    self._json(200, {"ok": True, "ignored": True})
                    return
                if not body:
                    self._json(200, {"ok": True})
                    return

                try:
                    reply = outer._handler.dispatch(body, source=f"wa:{from_id[:12]}")
                except Exception as e:
                    reply = f"❌ interner Fehler: {e}"
                    logger.exception("[CMD-WH] dispatch")
                if reply:
                    try:
                        outer._notifier.send(reply, to=from_id or None)
                    except Exception:
                        logger.exception("[CMD-WH] notify")
                self._json(200, {"ok": True})

        try:
            self._server = ThreadingHTTPServer((self._host, self._port), Req)
        except OSError as e:
            logger.error(f"[CMD-WH] Port {self._host}:{self._port} nicht bindbar: {e}")
            raise

        self._thread = threading.Thread(
            target=self._server.serve_forever,
            name="cmd-webhook",
            daemon=True,
        )
        self._thread.start()
        logger.info(
            f"[CMD-WH] listening on http://{self._host}:{self._port}/wa "
            f"({'token auth' if self._token else 'no auth'})"
        )

    def close(self, timeout: float = 2.0) -> None:
        srv = self._server
        if srv is not None:
            try:
                srv.shutdown()
                srv.server_close()
            except Exception:
                pass
        t = self._thread
        if t is not None and t.is_alive():
            t.join(timeout=timeout)


def default_webhook_token() -> str:
    return os.getenv("LIVE_WEBHOOK_TOKEN", "")


def default_webhook_url(port: int) -> str:
    return f"http://127.0.0.1:{port}/wa"
