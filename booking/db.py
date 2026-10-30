"""SQLite storage: appointments, conversation state and processed webhook ids.

Times are stored in UTC, so comparisons stay correct in time zones with daylight-saving
changes, and are returned in the business time zone (see `use_timezone`).
"""
from __future__ import annotations

import json
import sqlite3
import threading
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone, tzinfo
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


def _utc(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).isoformat(timespec="seconds")


class Database:
    def __init__(self, path: Path | str = ":memory:", tz: tzinfo = timezone.utc):
        if str(path) != ":memory:":
            Path(path).parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(str(path), check_same_thread=False, isolation_level=None)
        self.conn.row_factory = sqlite3.Row
        self.lock = threading.RLock()  # one connection shared by the web server's threads
        self.tz = tz
        self._inserts = 0
        self.conn.executescript(SCHEMA)

    def use_timezone(self, tz: tzinfo) -> None:
        """Appointments are returned in this time zone (the business's)."""
        self.tz = tz

    def _rows(self, sql: str, params=()) -> list[sqlite3.Row]:
        with self.lock:
            return self.conn.execute(sql, params).fetchall()

    def _local(self, iso: str) -> datetime:
        return datetime.fromisoformat(iso).astimezone(self.tz)

    # ---------------------------------------------------------------- idempotency
    def first_time_seen(self, message_id: str) -> bool:
        """Meta retries webhooks; this makes each message be processed exactly once."""
        now = datetime.now(timezone.utc)
        with self.lock:
            try:
                self.conn.execute("INSERT INTO processed_messages VALUES (?, ?)", (message_id, now.isoformat()))
            except sqlite3.IntegrityError:
                return False
            self._inserts += 1
            if self._inserts % 500 == 0:  # retries stop after a few days; keep the table small
                self.conn.execute("DELETE FROM processed_messages WHERE received_at < ?",
                                  ((now - timedelta(days=7)).isoformat(),))
            return True

    # ---------------------------------------------------------------- sessions
    def get_session(self, phone: str) -> dict:
        rows = self._rows("SELECT * FROM sessions WHERE phone=?", (phone,))
        if not rows:
            return {"state": "idle", "data": {}, "paused_until": None, "name": None, "last_inbound": None}
        row = rows[0]
        return {"state": row["state"], "data": json.loads(row["data"]), "paused_until": row["paused_until"],
                "name": row["name"], "last_inbound": row["last_inbound"]}

    def save_session(self, phone: str, state: str, data: dict | None = None, **fields) -> None:
        with self.lock:
            current = self.get_session(phone)
            values = {**current, "state": state, "data": data if data is not None else {}, **fields}
            self.conn.execute(
                "INSERT INTO sessions (phone, state, data, paused_until, name, last_inbound) VALUES (?,?,?,?,?,?) "
                "ON CONFLICT(phone) DO UPDATE SET state=excluded.state, data=excluded.data, "
                "paused_until=excluded.paused_until, name=excluded.name, last_inbound=excluded.last_inbound",
                (phone, values["state"], json.dumps(values["data"]), values["paused_until"], values["name"],
                 values["last_inbound"]))

    # ---------------------------------------------------------------- appointments
    def booked_intervals(self, start: datetime, end: datetime) -> list[tuple[datetime, datetime]]:
        rows = self._rows("SELECT start, end FROM appointments WHERE status='booked' AND start < ? AND end > ?",
                          (_utc(end), _utc(start)))
        return [(self._local(r["start"]), self._local(r["end"])) for r in rows]

    def book(self, phone: str, name: str, service_id: str, start: datetime, end: datetime) -> int:
        """Atomically re-check the slot and insert, so two customers can't take the same time."""
        with self.lock:
            self.conn.execute("BEGIN IMMEDIATE")
            try:
                clash = self.conn.execute(
                    "SELECT 1 FROM appointments WHERE status='booked' AND start < ? AND end > ?",
                    (_utc(end), _utc(start))).fetchone()
                if clash:
                    raise SlotTaken()
                cur = self.conn.execute(
                    "INSERT INTO appointments (phone, customer_name, service_id, start, end, created_at) "
                    "VALUES (?,?,?,?,?,?)",
                    (phone, name, service_id, _utc(start), _utc(end), _utc(datetime.now(timezone.utc))))
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
                           self._local(r["start"]), self._local(r["end"]), r["status"], bool(r["reminded"]))

    def get(self, appointment_id: int) -> Appointment | None:
        rows = self._rows("SELECT * FROM appointments WHERE id=?", (appointment_id,))
        return self._to_appt(rows[0]) if rows else None

    def upcoming_for(self, phone: str, now: datetime) -> list[Appointment]:
        rows = self._rows("SELECT * FROM appointments WHERE phone=? AND status='booked' AND start >= ? ORDER BY start",
                          (phone, _utc(now)))
        return [self._to_appt(r) for r in rows]

    def between(self, start: datetime, end: datetime) -> list[Appointment]:
        rows = self._rows("SELECT * FROM appointments WHERE status='booked' AND start >= ? AND start < ? ORDER BY start",
                          (_utc(start), _utc(end)))
        return [self._to_appt(r) for r in rows]

    def due_reminders(self, now: datetime, hours_ahead: float = 24) -> list[Appointment]:
        rows = self._rows(
            "SELECT * FROM appointments WHERE status='booked' AND reminded=0 AND start > ? AND start <= ? ORDER BY start",
            (_utc(now), _utc(now + timedelta(hours=hours_ahead))))
        return [self._to_appt(r) for r in rows]

    def mark_reminded(self, appointment_id: int) -> None:
        with self.lock:
            self.conn.execute("UPDATE appointments SET reminded=1 WHERE id=?", (appointment_id,))
