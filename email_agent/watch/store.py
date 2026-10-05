"""The watcher's store: `state/<instance>/events.sqlite` (Revision 12, Stage 8). Adapted from S17 `events/store.py`,
which keeps one JSON file; here SQLite, like our other stores.

S17's three properties are kept:
  - the dedup index outlives everything: an event key (`msg:<id>`, `thread:<id>@<updated_at>`) is never forgotten, so
    a change seen twice (the poll's overlap, a restart) never starts a second run;
  - every refusal is recorded: a quiet night and a night of refused events look different in `--status`;
  - windows (runs and LLM calls per subscription per day) and cursors live here, so they survive a restart: a daily
    ceiling that resets when the process restarts is no ceiling.
Plus ours: the record of writes we sent (`our_writes`), filled by WritePath (every run) and by undo_run.py, which the
governor reads to refuse a change we caused. One watcher per book; agent runs add their writes to the same file
while it runs (WAL and a busy timeout let them take turns).
"""

from __future__ import annotations

import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path

from email_agent.common.sqlite_db import connect, transaction
from email_agent.contracts.events import EventDecision, MailboxEvent, OurWrite

EVENTS_FILE = "events.sqlite"

SCHEMA = [
    """
    CREATE TABLE events (seq INTEGER PRIMARY KEY AUTOINCREMENT, key TEXT NOT NULL UNIQUE, type TEXT NOT NULL,
        source TEXT NOT NULL, thread_id TEXT, observed_at TEXT NOT NULL, event TEXT NOT NULL);
    CREATE INDEX events_source ON events(source, observed_at);
    CREATE TABLE decisions (id INTEGER PRIMARY KEY AUTOINCREMENT, event_key TEXT NOT NULL, subscription_id TEXT,
        admitted INTEGER NOT NULL, control TEXT NOT NULL, at TEXT NOT NULL, decision TEXT NOT NULL);
    CREATE INDEX decisions_event ON decisions(event_key);
    CREATE TABLE windows (day TEXT NOT NULL, subscription_id TEXT NOT NULL, kind TEXT NOT NULL,
        count INTEGER NOT NULL DEFAULT 0, PRIMARY KEY (day, subscription_id, kind));
    CREATE TABLE cursors (name TEXT PRIMARY KEY, value TEXT);
    CREATE TABLE our_writes (row_id TEXT NOT NULL, tool TEXT NOT NULL, at TEXT NOT NULL, via TEXT NOT NULL,
        run_id TEXT, PRIMARY KEY (row_id, at, via));
    CREATE INDEX our_writes_row ON our_writes(row_id, at)
    """,
]


def _iso(t: datetime) -> str:
    return (t if t.tzinfo else t.replace(tzinfo=timezone.utc)).astimezone(timezone.utc).isoformat()


class EventStore:
    def __init__(self, path: Path):
        self.con: sqlite3.Connection = connect(path, SCHEMA)

    @classmethod
    def for_instance(cls, state_dir: Path, instance: str) -> "EventStore":
        return cls(state_dir / instance / EVENTS_FILE)

    def close(self) -> None:
        self.con.close()

    # ── events (deduplicated forever) ────────────────────────────────────────
    def ingest(self, event: MailboxEvent) -> bool:
        """True if this event is new; False if its key was seen before (nothing is stored again)."""
        with transaction(self.con):
            cur = self.con.execute(
                "INSERT OR IGNORE INTO events (key, type, source, thread_id, observed_at, event) VALUES (?, ?, ?, ?, ?, ?)",
                (event.key, event.type, event.source, event.thread_id, _iso(event.observed_at), event.model_dump_json()))
        return cur.rowcount == 1

    def count_recent(self, source: str, since: datetime) -> int:
        return self.con.execute("SELECT COUNT(*) FROM events WHERE source = ? AND observed_at >= ?",
                                (source, _iso(since))).fetchone()[0]

    def decide(self, d: EventDecision) -> None:
        with transaction(self.con):
            self.con.execute("INSERT INTO decisions (event_key, subscription_id, admitted, control, at, decision) "
                             "VALUES (?, ?, ?, ?, ?, ?)",
                             (d.event_key, d.subscription_id, int(d.admitted), d.control, _iso(d.at), d.model_dump_json()))

    def decisions(self, limit: int = 20) -> list[EventDecision]:
        rows = self.con.execute("SELECT decision FROM decisions ORDER BY id DESC LIMIT ?", (limit,)).fetchall()
        return [EventDecision.model_validate_json(r["decision"]) for r in rows]

    def refusal_counts(self, since: datetime) -> dict[str, int]:
        rows = self.con.execute("SELECT control, COUNT(*) AS n FROM decisions WHERE admitted = 0 AND at >= ? "
                                "GROUP BY control", (_iso(since),)).fetchall()
        return {r["control"]: r["n"] for r in rows}

    def event(self, key: str) -> MailboxEvent | None:
        row = self.con.execute("SELECT event FROM events WHERE key = ?", (key,)).fetchone()
        return MailboxEvent.model_validate_json(row["event"]) if row else None

    # ── windows (S17 window_reserve / window_record) ─────────────────────────
    def window(self, day: str, subscription_id: str, kind: str) -> int:
        row = self.con.execute("SELECT count FROM windows WHERE day = ? AND subscription_id = ? AND kind = ?",
                               (day, subscription_id, kind)).fetchone()
        return row["count"] if row else 0

    def reserve(self, day: str, subscription_id: str, kind: str, limit: int) -> tuple[bool, int]:
        """Claim one slot if fewer than `limit` are used (atomically): (claimed, used before)."""
        with transaction(self.con):
            used = self.window(day, subscription_id, kind)
            if used >= limit:
                return False, used
            self._add(day, subscription_id, kind, 1)
        return True, used

    def reserve_up_to(self, day: str, subscription_id: str, kind: str, want: int, limit: int) -> int:
        """Claim up to `want` of what is left under `limit` (atomically); returns how much was claimed."""
        with transaction(self.con):
            granted = max(0, min(want, limit - self.window(day, subscription_id, kind)))
            if granted:
                self._add(day, subscription_id, kind, granted)
        return granted

    def record(self, day: str, subscription_id: str, kind: str, amount: int) -> None:
        with transaction(self.con):
            self._add(day, subscription_id, kind, amount)

    def _add(self, day: str, subscription_id: str, kind: str, amount: int) -> None:
        self.con.execute("INSERT INTO windows (day, subscription_id, kind, count) VALUES (?, ?, ?, ?) "
                         "ON CONFLICT (day, subscription_id, kind) DO UPDATE SET count = count + excluded.count",
                         (day, subscription_id, kind, amount))

    # ── cursors and heartbeat ────────────────────────────────────────────────
    def cursor(self, name: str) -> str | None:
        row = self.con.execute("SELECT value FROM cursors WHERE name = ?", (name,)).fetchone()
        return row["value"] if row else None

    def set_cursor(self, name: str, value: str | None) -> None:
        with transaction(self.con):
            self.con.execute("INSERT OR REPLACE INTO cursors (name, value) VALUES (?, ?)", (name, value))

    def beat(self) -> None:
        polls = int(self.cursor("polls") or 0) + 1
        self.set_cursor("polls", str(polls))
        self.set_cursor("last_poll_at", datetime.now(timezone.utc).isoformat())

    # ── writes we sent ───────────────────────────────────────────────────────
    def add_our_writes(self, writes: list[OurWrite]) -> int:
        with transaction(self.con):
            n = 0
            for w in writes:
                cur = self.con.execute("INSERT OR IGNORE INTO our_writes (row_id, tool, at, via, run_id) "
                                       "VALUES (?, ?, ?, ?, ?)", (w.row_id, w.tool, _iso(w.at), w.via, w.run_id))
                n += cur.rowcount
        return n

    def our_write_near(self, row_id: str, at: datetime, slack: timedelta) -> OurWrite | None:
        """A write we sent to this row within `slack` of `at` (the change's platform time), if any."""
        row = self.con.execute("SELECT row_id, tool, at, via, run_id FROM our_writes WHERE row_id = ? AND at BETWEEN ? "
                               "AND ? ORDER BY at DESC LIMIT 1",
                               (row_id, _iso(at - slack), _iso(at + slack))).fetchone()
        return OurWrite(row_id=row["row_id"], tool=row["tool"], at=datetime.fromisoformat(row["at"]), via=row["via"],
                        run_id=row["run_id"]) if row else None

    def our_writes_count(self) -> int:
        return self.con.execute("SELECT COUNT(*) FROM our_writes").fetchone()[0]


def record_our_writes(state_dir: Path, instance: str, writes: list[OurWrite]) -> None:
    """Add writes we sent to the book's record (WritePath and undo_run.py call this; never fails the caller's work
    silently: an error is raised to the caller, who logs it)."""
    if not writes:
        return
    store = EventStore.for_instance(state_dir, instance)
    try:
        store.add_our_writes(writes)
    finally:
        store.close()
