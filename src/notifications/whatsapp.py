"""
src/notifications/whatsapp.py — Client fuer die Node-WhatsApp-Bridge.

Die Bridge (siehe `whatsapp_bridge/`) laeuft lokal auf HTTP.
Der Client hier ist bewusst simpel gehalten:

  * Sendet in einem Hintergrund-Thread, damit Notifier den Signalpfad
    NICHT blockieren koennen (Netz-Hicksen, Bridge-Restart, etc.).
  * Ratelimit + Dedup (gleiche Nachricht innerhalb `dedup_window_sec`
    wird verworfen) gegen Spam bei Signal-Bursts.
  * `NullNotifier` als drop-in, wenn keine Bridge konfiguriert ist.

Konfiguration ueber ENV:
  WHATSAPP_BRIDGE_URL   Basis-URL, default http://127.0.0.1:3000
  WHATSAPP_BRIDGE_TOKEN Bearer-Token wenn die Bridge einen setzt
  WHATSAPP_TO           Empfaenger (Telefonnr. mit Laendercode ohne + oder Chat-ID `xxxx@c.us`)
"""
from __future__ import annotations

import os
import queue
import threading
import time
from dataclasses import dataclass
from typing import Optional

import requests
from loguru import logger


BRIDGE_URL   = os.getenv("WHATSAPP_BRIDGE_URL", "http://127.0.0.1:3000")
BRIDGE_TOKEN = os.getenv("WHATSAPP_BRIDGE_TOKEN", "")
BRIDGE_TO    = os.getenv("WHATSAPP_TO", "")


@dataclass(frozen=True)
class _Msg:
    to:      str
    body:    str


class NullNotifier:
    """Fallback wenn keine Bridge konfiguriert ist."""

    def send(self, message: str, to: Optional[str] = None) -> None:  # noqa: D401
        logger.debug(f"[NOTIFY-NULL] {message}")

    def health(self) -> dict:
        return {"ok": False, "reason": "null notifier"}

    def close(self) -> None:
        pass


class WhatsAppNotifier:
    """Async Notifier ueber die Node-Bridge."""

    def __init__(
        self,
        bridge_url: str  = BRIDGE_URL,
        token:      str  = BRIDGE_TOKEN,
        default_to: str  = BRIDGE_TO,
        min_interval_sec: float = 1.0,
        dedup_window_sec: float = 5.0,
        max_queue:  int  = 200,
    ) -> None:
        self._url    = bridge_url.rstrip("/")
        self._token  = token
        self._to     = default_to
        self._min_iv = float(min_interval_sec)
        self._dedup  = float(dedup_window_sec)
        self._queue: "queue.Queue[_Msg]" = queue.Queue(maxsize=max_queue)
        self._recent: dict[str, float] = {}
        self._recent_lock = threading.Lock()
        self._stop = threading.Event()
        self._last_send_at = 0.0
        self._worker = threading.Thread(target=self._loop, daemon=True, name="whatsapp-notify")
        self._worker.start()
        logger.info(f"[NOTIFY] WhatsApp bridge={self._url} to={self._to or '(none)'}")

    def _headers(self) -> dict[str, str]:
        h = {"Content-Type": "application/json"}
        if self._token:
            h["Authorization"] = f"Bearer {self._token}"
        return h

    def health(self) -> dict:
        try:
            r = requests.get(f"{self._url}/health", headers=self._headers(), timeout=3)
            r.raise_for_status()
            return r.json()
        except Exception as e:
            return {"ok": False, "error": str(e)}

    def send(self, message: str, to: Optional[str] = None) -> None:
        target = (to or self._to).strip()
        if not target:
            logger.debug("[NOTIFY] no recipient configured — skip")
            return
        body = message.strip()
        if not body:
            return

        now = time.time()
        key = f"{target}|{body}"
        with self._recent_lock:
            last = self._recent.get(key, 0.0)
            if now - last < self._dedup:
                logger.debug(f"[NOTIFY] dedup drop ({body[:40]!r})")
                return
            self._recent[key] = now
            if len(self._recent) > 512:
                cutoff = now - max(self._dedup, 60.0)
                self._recent = {k: t for k, t in self._recent.items() if t > cutoff}

        try:
            self._queue.put_nowait(_Msg(to=target, body=body))
        except queue.Full:
            logger.warning("[NOTIFY] queue full — drop message")

    def close(self, timeout: float = 2.0) -> None:
        self._stop.set()
        self._worker.join(timeout=timeout)

    def _loop(self) -> None:
        while not self._stop.is_set():
            try:
                msg = self._queue.get(timeout=0.5)
            except queue.Empty:
                continue
            wait = self._min_iv - (time.time() - self._last_send_at)
            if wait > 0:
                time.sleep(wait)
            self._send_now(msg)
            self._last_send_at = time.time()

    def _send_now(self, msg: _Msg) -> None:
        payload = {"to": msg.to, "message": msg.body}
        try:
            r = requests.post(
                f"{self._url}/send", json=payload,
                headers=self._headers(), timeout=8,
            )
            if r.status_code == 503:
                logger.warning("[NOTIFY] bridge not ready (503) — dropping msg")
                return
            if not r.ok:
                logger.warning(f"[NOTIFY] send failed {r.status_code}: {r.text[:200]}")
                return
            logger.debug(f"[NOTIFY] sent → {msg.to} ({len(msg.body)} chars)")
        except requests.RequestException as e:
            logger.warning(f"[NOTIFY] bridge unreachable: {e}")


def get_notifier() -> WhatsAppNotifier | NullNotifier:
    """Baut den Default-Notifier aus ENV, oder NullNotifier wenn nichts konfiguriert."""
    if not BRIDGE_TO:
        return NullNotifier()
    return WhatsAppNotifier()
