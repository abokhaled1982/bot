"""
src/utils/trader_store.py — State, den Bot und Dashboard teilen.

WO WAS LIEGT
------------
Die **Trader-Auswahl** (wer wird kopiert, mit welchem Betrag) liegt in
`data/copy_traders.json` — als Klartext-JSON, damit sie zusammen mit den
Trade-Dateien auf einen anderen Rechner kopiert werden kann und auch bei
gestopptem Bot lesbar/editierbar bleibt. Sie ist die Source of Truth.

Die **Telemetrie** (Tracker-Status, Heartbeat, Ereignis-Feed, Simulationen,
Verifikationen) bleibt in SQLite: reine Laufzeitdaten, nicht portabel nötig.

DATENFLUSS
----------
    Dashboard  --schreibt-->  copy_traders.json --liest-->  Bot (Sync-Loop, 3s)
    Bot        --schreibt-->  tracker_state     --liest-->  Dashboard
    Bot        --schreibt-->  bot_heartbeat     --liest-->  Dashboard

NEBENLÄUFIGKEIT
---------------
Zwei Prozesse schreiben gleichzeitig. JSON-Änderungen laufen unter einer
`flock`-Sperre und werden per `os.replace` atomar sichtbar; Leser sehen daher
nie eine halb geschriebene Datei. SQLite nutzt WAL-Journal + busy_timeout und
ausschließlich kurzlebige Verbindungen (keine geteilte Connection über Threads).
"""
from __future__ import annotations

import fcntl
import json
import os
import sqlite3
import tempfile
import time
from typing import Any, Optional

DB_PATH = os.getenv("BOT_DB_PATH", "db/binance_orderflow.db")

# Die Trader-Auswahl liegt bewusst als JSON vor: sie ist die Source of Truth
# und soll ohne DB-Export auf einen anderen Rechner kopierbar sein.
TRADERS_FILE = os.getenv("BOT_TRADERS_FILE", "data/copy_traders.json")

_SCHEMA = (
    """CREATE TABLE IF NOT EXISTS tracker_state (
        wallet          TEXT PRIMARY KEY,
        is_copied       INTEGER NOT NULL DEFAULT 0,
        ws_subscribed   INTEGER NOT NULL DEFAULT 0,
        ws_sub_at       REAL    NOT NULL DEFAULT 0,
        last_fill_at    REAL    NOT NULL DEFAULT 0,
        fill_count      INTEGER NOT NULL DEFAULT 0,
        last_poll_at    REAL    NOT NULL DEFAULT 0,
        poll_count      INTEGER NOT NULL DEFAULT 0,
        poll_error      TEXT    NOT NULL DEFAULT '',
        signal_count    INTEGER NOT NULL DEFAULT 0,
        last_signal_at  REAL    NOT NULL DEFAULT 0,
        open_positions  INTEGER NOT NULL DEFAULT 0,
        positions_json  TEXT    NOT NULL DEFAULT '{}',
        account_usd     REAL    NOT NULL DEFAULT 0,
        updated_at      REAL    NOT NULL DEFAULT 0
    )""",
    """CREATE TABLE IF NOT EXISTS bot_heartbeat (
        id           INTEGER PRIMARY KEY CHECK (id = 1),
        pid          INTEGER NOT NULL DEFAULT 0,
        started_at   REAL    NOT NULL DEFAULT 0,
        updated_at   REAL    NOT NULL DEFAULT 0,
        connected    INTEGER NOT NULL DEFAULT 0,
        ws_connected INTEGER NOT NULL DEFAULT 0,
        dry_run      INTEGER NOT NULL DEFAULT 1,
        tracked      INTEGER NOT NULL DEFAULT 0,
        copied       INTEGER NOT NULL DEFAULT 0,
        open_trades  INTEGER NOT NULL DEFAULT 0,
        api_health   TEXT    NOT NULL DEFAULT 'ok'
    )""",
    """CREATE TABLE IF NOT EXISTS pipeline_events (
        id      INTEGER PRIMARY KEY AUTOINCREMENT,
        ts      REAL NOT NULL,
        kind    TEXT NOT NULL,
        level   TEXT NOT NULL DEFAULT 'info',
        wallet  TEXT NOT NULL DEFAULT '',
        symbol  TEXT NOT NULL DEFAULT '',
        message TEXT NOT NULL DEFAULT ''
    )""",
    "CREATE INDEX IF NOT EXISTS idx_pipeline_events_ts ON pipeline_events(ts DESC)",
    """CREATE TABLE IF NOT EXISTS simulation_requests (
        id          INTEGER PRIMARY KEY AUTOINCREMENT,
        created_at  REAL NOT NULL,
        updated_at  REAL NOT NULL,
        wallet      TEXT NOT NULL,
        action      TEXT NOT NULL,
        coin        TEXT NOT NULL,
        status      TEXT NOT NULL DEFAULT 'pending',
        result      TEXT NOT NULL DEFAULT ''
    )""",
    """CREATE TABLE IF NOT EXISTS trader_verifications (
        wallet       TEXT PRIMARY KEY,
        verified_at  REAL NOT NULL,
        quality_score REAL NOT NULL,
        metrics_json TEXT NOT NULL
    )""",
)

# So viele Ereignisse bleiben erhalten; ältere werden beim Schreiben gekappt.
MAX_EVENTS = int(os.getenv("PIPELINE_EVENT_LIMIT", "2000"))


def _connect() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH, timeout=15.0)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=15000")
    return conn


def init_db() -> None:
    """Idempotent: Schema anlegen, falls noch nicht vorhanden."""
    parent = os.path.dirname(DB_PATH)
    if parent:
        os.makedirs(parent, exist_ok=True)
    with _connect() as conn:
        for stmt in _SCHEMA:
            conn.execute(stmt)
    _migrate_traders_from_sqlite()


# ── copy_traders (JSON — Source of Truth, portabel) ──────────────────────────

_TRADER_FIELDS: dict[str, Any] = {
    "wallet": "", "size_usdt": None, "is_copied": 0, "is_focus": 0,
    "note": "", "source": "manual", "account_usd": 0.0,
    "win_rate": 0.0, "trades": 0, "added_at": 0.0, "updated_at": 0.0,
}


def _normalize(row: dict[str, Any]) -> dict[str, Any]:
    return {k: row.get(k, default) for k, default in _TRADER_FIELDS.items()}


def _read_traders() -> list[dict[str, Any]]:
    try:
        with open(TRADERS_FILE, encoding="utf-8") as fh:
            data = json.load(fh)
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        return []
    if not isinstance(data, list):
        return []
    return [_normalize(r) for r in data if isinstance(r, dict) and r.get("wallet")]


def _write_traders(rows: list[dict[str, Any]]) -> None:
    parent = os.path.dirname(TRADERS_FILE) or "."
    os.makedirs(parent, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=parent, prefix=".copy_traders_", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(rows, fh, indent=2, ensure_ascii=False)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, TRADERS_FILE)
    except BaseException:
        if os.path.exists(tmp):
            os.unlink(tmp)
        raise


class _traders_locked:
    """Read-modify-write serialisiert über Prozessgrenzen (Bot + Dashboard)."""

    def __enter__(self) -> list[dict[str, Any]]:
        parent = os.path.dirname(TRADERS_FILE) or "."
        os.makedirs(parent, exist_ok=True)
        self._fh = open(os.path.join(parent, ".copy_traders.lock"), "w")
        fcntl.flock(self._fh, fcntl.LOCK_EX)
        self.rows = _read_traders()
        return self.rows

    def __exit__(self, exc_type, exc, tb) -> None:
        try:
            if exc_type is None:
                _write_traders(self.rows)
        finally:
            fcntl.flock(self._fh, fcntl.LOCK_UN)
            self._fh.close()


def _find(rows: list[dict[str, Any]], wallet: str) -> Optional[dict[str, Any]]:
    return next((r for r in rows if r["wallet"] == wallet), None)


def upsert_trader(
    wallet: str,
    *,
    size_usdt: Optional[float] = None,
    is_copied: Optional[bool] = None,
    is_focus: Optional[bool] = None,
    note: Optional[str] = None,
    source: Optional[str] = None,
) -> None:
    """Trader anlegen oder ändern. `None` lässt das jeweilige Feld unverändert."""
    wallet = wallet.strip()
    if not wallet:
        return
    now = time.time()
    with _traders_locked() as rows:
        row = _find(rows, wallet)
        if row is None:
            row = _normalize({"wallet": wallet, "added_at": now})
            rows.append(row)
        if size_usdt is not None:
            row["size_usdt"] = float(size_usdt) if size_usdt > 0 else None
        if is_copied is not None:
            row["is_copied"] = 1 if is_copied else 0
        if is_focus is not None:
            row["is_focus"] = 1 if is_focus else 0
        if note is not None:
            row["note"] = note[:500]
        if source is not None:
            row["source"] = source[:20]
        row["updated_at"] = now


def set_focus(wallet: Optional[str]) -> None:
    """Genau einen Trader anpinnen (oder mit `None` alle Anpinnungen lösen)."""
    now = time.time()
    w = (wallet or "").strip()
    with _traders_locked() as rows:
        for r in rows:
            if r["is_focus"]:
                r["is_focus"] = 0
                r["updated_at"] = now
        if w:
            row = _find(rows, w)
            if row is None:
                row = _normalize({"wallet": w, "added_at": now})
                rows.append(row)
            row["is_focus"] = 1
            row["updated_at"] = now


def update_trader_stats(
    wallet: str, *, account_usd: float = 0.0,
    win_rate: float = 0.0, trades: int = 0,
) -> None:
    """Zuletzt bekannte Trader-Kennzahlen mitschreiben (nur zur Anzeige)."""
    with _traders_locked() as rows:
        row = _find(rows, wallet.strip())
        if row is None:
            return
        row.update(
            account_usd=float(account_usd), win_rate=float(win_rate),
            trades=int(trades), updated_at=time.time(),
        )


def remove_trader(wallet: str) -> None:
    """Trader vollständig entfernen (inkl. Tracking-Telemetrie)."""
    w = wallet.strip()
    with _traders_locked() as rows:
        rows[:] = [r for r in rows if r["wallet"] != w]
    with _connect() as conn:
        conn.execute("DELETE FROM tracker_state WHERE wallet = ?", (w,))
        conn.execute("DELETE FROM trader_verifications WHERE wallet = ?", (w,))


def list_traders() -> list[dict[str, Any]]:
    """Alle gewählten Trader — angepinnte zuerst, dann kopierte, dann Rest."""
    return sorted(
        _read_traders(),
        key=lambda r: (-int(r["is_focus"]), -int(r["is_copied"]), float(r["added_at"])),
    )


def get_focus() -> Optional[str]:
    return next((r["wallet"] for r in _read_traders() if r["is_focus"]), None)


def _migrate_traders_from_sqlite() -> None:
    """Einmalig: bestehende copy_traders-Tabelle in die JSON-Datei übernehmen."""
    if os.path.exists(TRADERS_FILE):
        return
    try:
        with _connect() as conn:
            rows = conn.execute("SELECT * FROM copy_traders").fetchall()
    except sqlite3.Error:
        rows = []
    _write_traders([_normalize(dict(r)) for r in rows])


def save_verification(wallet: str, metrics: dict[str, Any]) -> None:
    """Den exakten Scanner-Befund zum Zeitpunkt der Übernahme speichern."""
    snapshot_keys = (
        "quality_score", "day_roi", "day_pnl", "week_roi", "week_pnl",
        "month_roi", "month_pnl", "follower_count", "position_shared",
        "last_active_age", "metrics_source",
    )
    snapshot = {key: metrics.get(key) for key in snapshot_keys}
    with _connect() as conn:
        conn.execute(
            """INSERT INTO trader_verifications
               (wallet, verified_at, quality_score, metrics_json)
               VALUES (?,?,?,?)
               ON CONFLICT(wallet) DO UPDATE SET
                   verified_at=excluded.verified_at,
                   quality_score=excluded.quality_score,
                   metrics_json=excluded.metrics_json""",
            (wallet.strip(), time.time(), float(metrics["quality_score"]),
             json.dumps(snapshot)),
        )


def get_verification(wallet: str) -> Optional[dict[str, Any]]:
    try:
        with _connect() as conn:
            row = conn.execute(
                "SELECT * FROM trader_verifications WHERE wallet = ?",
                (wallet.strip(),),
            ).fetchone()
    except sqlite3.Error:
        return None
    if not row:
        return None
    result = dict(row)
    try:
        result["metrics"] = json.loads(result.pop("metrics_json"))
    except json.JSONDecodeError:
        result["metrics"] = {}
    return result


# ── tracker_state (Bot schreibt, Dashboard liest) ────────────────────────────

def publish_tracker_state(rows: list[dict[str, Any]]) -> None:
    """Tracking-Telemetrie des Bots veröffentlichen (ein Batch pro Zyklus)."""
    if not rows:
        return
    now = time.time()
    payload = [
        (
            r["wallet"], int(bool(r.get("is_copied"))),
            int(bool(r.get("ws_subscribed"))), float(r.get("ws_sub_at", 0)),
            float(r.get("last_fill_at", 0)), int(r.get("fill_count", 0)),
            float(r.get("last_poll_at", 0)), int(r.get("poll_count", 0)),
            str(r.get("poll_error", ""))[:200],
            int(r.get("signal_count", 0)), float(r.get("last_signal_at", 0)),
            int(r.get("open_positions", 0)),
            json.dumps(r.get("positions", {}), default=str),
            float(r.get("account_usd", 0)), now,
        )
        for r in rows
    ]
    with _connect() as conn:
        conn.executemany(
            """INSERT INTO tracker_state (
                   wallet, is_copied, ws_subscribed, ws_sub_at, last_fill_at,
                   fill_count, last_poll_at, poll_count, poll_error,
                   signal_count, last_signal_at, open_positions,
                   positions_json, account_usd, updated_at)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
               ON CONFLICT(wallet) DO UPDATE SET
                   is_copied=excluded.is_copied,
                   ws_subscribed=excluded.ws_subscribed,
                   ws_sub_at=excluded.ws_sub_at,
                   last_fill_at=excluded.last_fill_at,
                   fill_count=excluded.fill_count,
                   last_poll_at=excluded.last_poll_at,
                   poll_count=excluded.poll_count,
                   poll_error=excluded.poll_error,
                   signal_count=excluded.signal_count,
                   last_signal_at=excluded.last_signal_at,
                   open_positions=excluded.open_positions,
                   positions_json=excluded.positions_json,
                   account_usd=excluded.account_usd,
                   updated_at=excluded.updated_at""",
            payload,
        )


def get_tracker_state() -> dict[str, dict[str, Any]]:
    """wallet -> Telemetrie. `positions` ist bereits deserialisiert."""
    try:
        with _connect() as conn:
            rows = conn.execute("SELECT * FROM tracker_state").fetchall()
    except sqlite3.Error:
        return {}
    out: dict[str, dict[str, Any]] = {}
    for row in rows:
        rec = dict(row)
        try:
            rec["positions"] = json.loads(rec.pop("positions_json") or "{}")
        except json.JSONDecodeError:
            rec["positions"] = {}
        out[rec["wallet"]] = rec
    return out


# ── bot_heartbeat (Bot schreibt, Dashboard liest) ────────────────────────────

def publish_heartbeat(**fields: Any) -> None:
    """Lebenszeichen des Bot-Prozesses schreiben."""
    now = time.time()
    with _connect() as conn:
        conn.execute(
            "INSERT OR IGNORE INTO bot_heartbeat (id, started_at) VALUES (1, ?)",
            (fields.get("started_at", now),),
        )
        conn.execute(
            """UPDATE bot_heartbeat SET
                   pid = ?, updated_at = ?, connected = ?, ws_connected = ?,
                   dry_run = ?, tracked = ?, copied = ?, open_trades = ?,
                   api_health = ?
               WHERE id = 1""",
            (
                int(fields.get("pid", os.getpid())), now,
                int(bool(fields.get("connected"))),
                int(bool(fields.get("ws_connected"))),
                int(bool(fields.get("dry_run", True))),
                int(fields.get("tracked", 0)), int(fields.get("copied", 0)),
                int(fields.get("open_trades", 0)),
                str(fields.get("api_health", "ok"))[:20],
            ),
        )


def get_heartbeat() -> Optional[dict[str, Any]]:
    try:
        with _connect() as conn:
            row = conn.execute("SELECT * FROM bot_heartbeat WHERE id = 1").fetchone()
    except sqlite3.Error:
        return None
    return dict(row) if row else None


# ── pipeline_events (Bot schreibt, Dashboard liest) ──────────────────────────

def log_event(
    kind: str, message: str, *,
    wallet: str = "", symbol: str = "", level: str = "info",
) -> None:
    """Eine Pipeline-Entscheidung protokollieren.

    `kind` ist die Kategorie für das Dashboard-Filter, z.B. SIGNAL, SKIP, BUY,
    SELL, TRADER_ADDED. Fehler hier dürfen den Handel nie stoppen — deshalb
    schluckt die Funktion DB-Fehler bewusst.
    """
    try:
        with _connect() as conn:
            conn.execute(
                "INSERT INTO pipeline_events (ts, kind, level, wallet, symbol, message) "
                "VALUES (?,?,?,?,?,?)",
                (time.time(), kind[:30], level[:10], wallet[:64], symbol[:20], message[:500]),
            )
            conn.execute(
                "DELETE FROM pipeline_events WHERE id <= "
                "(SELECT MAX(id) - ? FROM pipeline_events)",
                (MAX_EVENTS,),
            )
    except sqlite3.Error:
        pass


def recent_events(limit: int = 100, wallet: str = "") -> list[dict[str, Any]]:
    sql = "SELECT * FROM pipeline_events"
    args: list[Any] = []
    if wallet:
        sql += " WHERE wallet = ?"
        args.append(wallet)
    sql += " ORDER BY id DESC LIMIT ?"
    args.append(int(limit))
    try:
        with _connect() as conn:
            return [dict(r) for r in conn.execute(sql, args).fetchall()]
    except sqlite3.Error:
        return []


# ── simulation_requests (nur PAPER-Modus) ───────────────────────────────────

def enqueue_simulation(wallet: str, action: str, coin: str) -> int:
    """Einen kontrollierten PAPER-Kauf oder -Verkauf anfordern."""
    action = action.strip().upper()
    coin = coin.strip().upper()
    if action not in {"BUY", "SELL"}:
        raise ValueError("action muss BUY oder SELL sein")
    if not wallet.strip() or not coin or not coin.isalnum():
        raise ValueError("Wallet oder Coin ungültig")
    now = time.time()
    with _connect() as conn:
        cursor = conn.execute(
            """INSERT INTO simulation_requests
               (created_at, updated_at, wallet, action, coin)
               VALUES (?,?,?,?,?)""",
            (now, now, wallet.strip(), action, coin),
        )
        return int(cursor.lastrowid)


def claim_simulations(limit: int = 10) -> list[dict[str, Any]]:
    """Pending-Aufträge atomar beanspruchen, damit jeder genau einmal läuft."""
    with _connect() as conn:
        conn.execute("BEGIN IMMEDIATE")
        rows = conn.execute(
            "SELECT * FROM simulation_requests WHERE status = 'pending' "
            "ORDER BY id ASC LIMIT ?",
            (int(limit),),
        ).fetchall()
        ids = [int(row["id"]) for row in rows]
        if ids:
            placeholders = ",".join("?" for _ in ids)
            conn.execute(
                f"UPDATE simulation_requests SET status = 'processing', "
                f"updated_at = ? WHERE id IN ({placeholders})",
                (time.time(), *ids),
            )
    return [dict(row) for row in rows]


def finish_simulation(request_id: int, success: bool, result: str) -> None:
    with _connect() as conn:
        conn.execute(
            "UPDATE simulation_requests SET status = ?, result = ?, updated_at = ? "
            "WHERE id = ?",
            ("done" if success else "failed", result[:500], time.time(), int(request_id)),
        )


def recent_simulations(wallet: str = "", limit: int = 20) -> list[dict[str, Any]]:
    sql = "SELECT * FROM simulation_requests"
    args: list[Any] = []
    if wallet:
        sql += " WHERE wallet = ?"
        args.append(wallet)
    sql += " ORDER BY id DESC LIMIT ?"
    args.append(int(limit))
    try:
        with _connect() as conn:
            return [dict(row) for row in conn.execute(sql, args).fetchall()]
    except sqlite3.Error:
        return []

