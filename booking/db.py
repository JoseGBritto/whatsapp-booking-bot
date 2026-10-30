"""SQLite storage: appointments, conversation state and processed webhook ids."""
from __future__ import annotations

import json
import sqlite3
import threading
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

SCHEMA = """
CREATE TABLE IF NOT EXISTS appointments (
    id INTEGER PRIMARY KEY, phone TEXT NOT NULL, customer_name TEXT, service_id TEXT NOT NULL,
    start TEXT NOT NULL, end TEXT NOT NULL, status TEXT NOT NULL DEFAULT 'booked',
    reminded INTEGER NOT NULL DEFAULT 0, created_at TEXT NOT NULL);
CREATE INDEX IF NOT EXISTS idx_appt_start ON appointments(start);
CREATE TABLE IF NOT EXISTS sessions (
    phone TEXT PRIMARY KEY, state TEXT NOT NULL, data TEXT NOT NULL DEFAULT '{}',
    paused_until TEXT, name TEXT, last_inbound TEXT);
CREATE TABLE IF NOT EXISTS processed_messages (id TEXT PRIMARY KEY, received_at TEXT NOT NULL);
"""


class SlotTaken(Exception):
    """Someone else booked an overlapping time first."""


@dataclass
class Appointment:
    id: int
    phone: str
    customer_name: str
    service_id: str
    start: datetime
    end: datetime
    status: str
    reminded: bool


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


class Database:
    def __init__(self, path: Path | str = ":memory:"):
        if str(path) != ":memory:":
            Path(path).parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(str(path), check_same_thread=False, isolation_level=None)
        self.conn.row_factory = sqlite3.Row
        self.lock = threading.RLock()
        self.conn.executescript(SCHEMA)

    # ---------------------------------------------------------------- idempotency
    def first_time_seen(self, message_id: str) -> bool:
        """Meta retries webhooks; this makes each message be processed exactly once."""
        with self.lock:
            try:
                self.conn.execute("INSERT INTO processed_messages VALUES (?, ?)", (message_id, _now()))
                return True
            except sqlite3.IntegrityError:
                return False

    # ---------------------------------------------------------------- sessions
    def get_session(self, phone: str) -> dict:
        row = self.conn.execute("SELECT * FROM sessions WHERE phone=?", (phone,)).fetchone()
        if not row:
            return {"state": "idle", "data": {}, "paused_until": None, "name": None, "last_inbound": None}
        return {"state": row["state"], "data": json.loads(row["data"]), "paused_until": row["paused_until"],
                "name": row["name"], "last_inbound": row["last_inbound"]}

    def save_session(self, phone: str, state: str, data: dict | None = None, **fields) -> None:
        current = self.get_session(phone)
        values = {**current, "state": state, "data": data if data is not None else {}, **fields}
        with self.lock:
            self.conn.execute(
                "INSERT INTO sessions (phone, state, data, paused_until, name, last_inbound) VALUES (?,?,?,?,?,?) "
                "ON CONFLICT(phone) DO UPDATE SET state=excluded.state, data=excluded.data, "
                "paused_until=excluded.paused_until, name=excluded.name, last_inbound=excluded.last_inbound",
                (phone, values["state"], json.dumps(values["data"]), values["paused_until"], values["name"],
                 values["last_inbound"]))

    # ---------------------------------------------------------------- appointments
    def booked_intervals(self, start: datetime, end: datetime) -> list[tuple[datetime, datetime]]:
        rows = self.conn.execute(
            "SELECT start, end FROM appointments WHERE status='booked' AND start < ? AND end > ?",
            (end.isoformat(), start.isoformat()))
        return [(datetime.fromisoformat(r["start"]), datetime.fromisoformat(r["end"])) for r in rows]

    def book(self, phone: str, name: str, service_id: str, start: datetime, end: datetime) -> int:
        """Atomically re-check the slot and insert, so two customers can't take the same time."""
        with self.lock:
            self.conn.execute("BEGIN IMMEDIATE")
            try:
                clash = self.conn.execute(
                    "SELECT 1 FROM appointments WHERE status='booked' AND start < ? AND end > ?",
                    (end.isoformat(), start.isoformat())).fetchone()
                if clash:
                    raise SlotTaken()
                cur = self.conn.execute(
                    "INSERT INTO appointments (phone, customer_name, service_id, start, end, created_at) "
                    "VALUES (?,?,?,?,?,?)", (phone, name, service_id, start.isoformat(), end.isoformat(), _now()))
                self.conn.execute("COMMIT")
                return cur.lastrowid
            except Exception:
                self.conn.execute("ROLLBACK")
                raise

    def cancel(self, appointment_id: int, phone: str | None = None) -> bool:
        sql, params = "UPDATE appointments SET status='cancelled' WHERE id=? AND status='booked'", [appointment_id]
        if phone:
            sql += " AND phone=?"
            params.append(phone)
        with self.lock:
            return self.conn.execute(sql, params).rowcount > 0

    def _to_appt(self, r) -> Appointment:
        return Appointment(r["id"], r["phone"], r["customer_name"] or "", r["service_id"],
                           datetime.fromisoformat(r["start"]), datetime.fromisoformat(r["end"]),
                           r["status"], bool(r["reminded"]))

    def get(self, appointment_id: int) -> Appointment | None:
        r = self.conn.execute("SELECT * FROM appointments WHERE id=?", (appointment_id,)).fetchone()
        return self._to_appt(r) if r else None

    def upcoming_for(self, phone: str, now: datetime) -> list[Appointment]:
        rows = self.conn.execute(
            "SELECT * FROM appointments WHERE phone=? AND status='booked' AND start >= ? ORDER BY start",
            (phone, now.isoformat()))
        return [self._to_appt(r) for r in rows]

    def between(self, start: datetime, end: datetime) -> list[Appointment]:
        rows = self.conn.execute(
            "SELECT * FROM appointments WHERE status='booked' AND start >= ? AND start < ? ORDER BY start",
            (start.isoformat(), end.isoformat()))
        return [self._to_appt(r) for r in rows]

    def due_reminders(self, now: datetime, hours_ahead: float = 24) -> list[Appointment]:
        from datetime import timedelta
        rows = self.conn.execute(
            "SELECT * FROM appointments WHERE status='booked' AND reminded=0 AND start > ? AND start <= ? ORDER BY start",
            (now.isoformat(), (now + timedelta(hours=hours_ahead)).isoformat()))
        return [self._to_appt(r) for r in rows]

    def mark_reminded(self, appointment_id: int) -> None:
        with self.lock:
            self.conn.execute("UPDATE appointments SET reminded=1 WHERE id=?", (appointment_id,))
