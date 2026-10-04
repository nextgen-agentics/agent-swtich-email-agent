"""WriteOutbox: every platform write is recorded before it is sent, so a resumed run never sends it twice.

Adapted from S17 `s17code/events/outbox.py` (ActionOutbox). Kept: the key is sha256(run, node, tool, arguments); a
completed receipt is reused instead of writing again; a record still "started" after a crash means the write may or may
not have happened, so it is never repeated blindly — the worker gets a Deferred("outbox.reconcile") and the node waits.

Changed: the records live in the run's own SQLite file (table `outbox`) next to the journal, and each step journals a
`write_*` event in the same transaction. The row's values before the write (`before`) are saved first: undo and
reconcile both need them. `resolve()` is how a reconcile step (Stage 6) settles an uncertain record after reading the
live row: it happened → completed; it didn't → the record is removed so the next attempt sends it; someone else
changed the row → `give_up()` marks it failed (Stage 6, `email_agent/graph/reconcile.py`).
"""

from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone
from typing import Any, Awaitable, Callable

from email_agent.contracts.graph import Deferred, OutboxRecord, OutboxStatus
from email_agent.graph.store import RunStore

RECONCILE = "outbox.reconcile"


class OutboxFailed(RuntimeError):
    """The platform refused this write earlier in the run; it is not sent again."""


class UncertainWrite(Exception):
    """The send got no answer (a timeout): the write may or may not have happened. Raised by `send`; the record stays
    "started", so the node parks until reconcile reads the live row."""


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _j(value: Any) -> str:
    return json.dumps(value, sort_keys=True, ensure_ascii=False, default=str)


class WriteOutbox:
    def __init__(self, store: RunStore):
        self.store = store

    @staticmethod
    def key(run_id: str, node_id: str, tool: str, arguments: dict[str, Any]) -> str:
        body = json.dumps([run_id, node_id, tool, arguments], sort_keys=True, separators=(",", ":"), default=str)
        return hashlib.sha256(body.encode()).hexdigest()

    def get(self, key: str) -> OutboxRecord | None:
        r = self.store.con.execute("SELECT * FROM outbox WHERE key = ?", (key,)).fetchone()
        if r is None:
            return None
        return OutboxRecord(key=r["key"], node_id=r["node_id"], tool=r["tool"], row_id=r["row_id"],
                            arguments=json.loads(r["arguments"]), before=json.loads(r["before"]) if r["before"] else None,
                            status=r["status"], receipt=json.loads(r["receipt"]) if r["receipt"] else None,
                            error=r["error"], updated_at=r["updated_at"])

    def records(self, status: OutboxStatus | None = None) -> list[OutboxRecord]:
        sql, args = "SELECT key FROM outbox", tuple[Any, ...]()
        if status is not None:
            sql, args = sql + " WHERE status = ?", (status,)
        return [rec for r in self.store.con.execute(sql + " ORDER BY updated_at", args)
                if (rec := self.get(r["key"])) is not None]

    async def execute(self, *, node_id: str, tool: str, arguments: dict[str, Any], send: Callable[[], Awaitable[dict]],
                      row_id: str | None = None, before: dict[str, Any] | None = None) -> dict[str, Any] | Deferred:
        """Send one write at most once. Returns the receipt, or a Deferred when an earlier attempt is uncertain."""
        key = self.key(self.store.run_id, node_id, tool, arguments)
        record = self.get(key)
        if record is not None:
            if record.status == OutboxStatus.COMPLETED:
                return record.receipt or {}
            if record.status == OutboxStatus.FAILED:
                raise OutboxFailed(record.error or "the platform refused this write")
            self.store.record_event("write_uncertain", node_id, {"key": key, "tool": tool, "row_id": row_id})
            return Deferred(handle=key, event_type=RECONCILE, metadata={"tool": tool, "row_id": row_id})
        with self.store.transaction():
            self.store.con.execute(
                """INSERT INTO outbox (key, node_id, tool, row_id, arguments, before, status, updated_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
                (key, node_id, tool, row_id, _j(arguments), _j(before) if before is not None else None,
                 OutboxStatus.STARTED, _now()))
            self.store.append_event("write_started", node_id, {"key": key, "tool": tool, "row_id": row_id})
        try:
            receipt = await send()
        except UncertainWrite as e:
            self.store.record_event("write_uncertain", node_id, {"key": key, "tool": tool, "row_id": row_id,
                                                                 "error": str(e)[:300]})
            return Deferred(handle=key, event_type=RECONCILE, metadata={"tool": tool, "row_id": row_id})
        except Exception as e:
            with self.store.transaction():
                self.store.con.execute("UPDATE outbox SET status = ?, error = ?, updated_at = ? WHERE key = ?",
                                       (OutboxStatus.FAILED, f"{type(e).__name__}: {e}"[:2000], _now(), key))
                self.store.append_event("write_failed", node_id, {"key": key, "tool": tool, "row_id": row_id,
                                                                  "error": f"{type(e).__name__}: {e}"[:500]})
            raise
        with self.store.transaction():
            self.store.con.execute("UPDATE outbox SET status = ?, receipt = ?, updated_at = ? WHERE key = ?",
                                   (OutboxStatus.COMPLETED, _j(receipt), _now(), key))
            self.store.append_event("write_completed", node_id, {"key": key, "tool": tool, "row_id": row_id})
        return receipt

    def resolve(self, key: str, *, happened: bool, receipt: dict[str, Any] | None = None, note: str = "") -> None:
        """Settle an uncertain ("started") record after reading the live row."""
        record = self.get(key)
        if record is None or record.status != OutboxStatus.STARTED:
            return
        with self.store.transaction():
            if happened:
                self.store.con.execute("UPDATE outbox SET status = ?, receipt = ?, updated_at = ? WHERE key = ?",
                                       (OutboxStatus.COMPLETED, _j(receipt or {"reconciled": True}), _now(), key))
            else:
                self.store.con.execute("DELETE FROM outbox WHERE key = ?", (key,))
            self.store.append_event("write_reconciled", record.node_id,
                                    {"key": key, "tool": record.tool, "row_id": record.row_id, "happened": happened,
                                     "note": note})

    def give_up(self, key: str, error: str) -> None:
        """Settle an uncertain record as failed: the live row holds neither what we sent nor what was there before
        (someone else changed it), so it is never sent again."""
        record = self.get(key)
        if record is None or record.status != OutboxStatus.STARTED:
            return
        with self.store.transaction():
            self.store.con.execute("UPDATE outbox SET status = ?, error = ?, updated_at = ? WHERE key = ?",
                                   (OutboxStatus.FAILED, error[:2000], _now(), key))
            self.store.append_event("write_reconciled", record.node_id,
                                    {"key": key, "tool": record.tool, "row_id": record.row_id, "happened": None,
                                     "note": error[:300]})
