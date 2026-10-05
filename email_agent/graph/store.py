"""RunStore: one run's graph, journal, budgets and write outbox in one SQLite file (`runs/<id>/run.sqlite`).

Adapted from S17 `s17code/core/live_graph/store.py` (GraphStore). What is kept: the graph only changes through a checked
GraphPatch; every change appends a numbered journal event; each planner patch is recorded against the event that caused it
(`applied_triggers`), so after a crash every outcome without a patch is planned again exactly once; resume turns RUNNING
nodes back to PENDING; WAITING nodes are completed by a single-use handle.

What changed, and why:
  - SQLite instead of rewriting one JSON file per change: S17 rewrites the whole checkpoint on every mutation, which grows
    with the run; here each change is one small transaction, and the outcome, its journal event and any fan-out patch
    commit together.
  - stdlib `graphlib` for the cycle check instead of networkx.
  - Budgets are saved here, so a resumed run keeps what it has spent (S17 keeps budgets in memory only).
  - A fault-injection hook (`crash_at`, from EMAIL_AGENT_CRASH_AT=<event_kind>:<n>) raises InjectedCrash right after the
    n-th event of that kind is committed. Off unless set; used only by scripts/agent/drill_resume.py.
  - Every LLM reply is saved with its `llm_call_finished` event (table `llm_replies`, keyed by node and request hash), so
    a node or planner round that runs again after a resume gets the same reply without paying for it again (S17 replays
    the recorded planner reply the same way; here it covers every model call).
"""

from __future__ import annotations

import json
import os
import sqlite3
from collections import Counter
from contextlib import contextmanager
from datetime import datetime, timezone
from graphlib import CycleError, TopologicalSorter
from pathlib import Path
from typing import Any, Iterator, get_args

from pydantic import BaseModel

from email_agent.common.sqlite_db import connect, lock_of
from email_agent.contracts.graph import (
    ACTIVE_STATES,
    PLANNER_TRIGGERS,
    Budget,
    Deferred,
    EventKind,
    GraphPatch,
    GraphSnapshot,
    JournalEvent,
    NodeFailure,
    NodeRecord,
    NodeState,
    TaskSpec,
)

RUN_FILE = "run.sqlite"

SCHEMA = [
    """
    CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
    CREATE TABLE nodes (
        id TEXT PRIMARY KEY, capability TEXT NOT NULL, goal_id TEXT, input TEXT NOT NULL, state TEXT NOT NULL,
        attempt INTEGER NOT NULL DEFAULT 0, timeout_s REAL, wakes_planner INTEGER NOT NULL DEFAULT 1,
        result TEXT, error TEXT, wait TEXT, metadata TEXT NOT NULL, started_at TEXT, ended_at TEXT,
        added_by INTEGER NOT NULL);
    CREATE INDEX nodes_state ON nodes(state);
    CREATE TABLE edges (parent TEXT NOT NULL REFERENCES nodes(id), child TEXT NOT NULL REFERENCES nodes(id),
        PRIMARY KEY (parent, child));
    CREATE INDEX edges_child ON edges(child);
    CREATE TABLE events (seq INTEGER PRIMARY KEY, kind TEXT NOT NULL, node_id TEXT, payload TEXT NOT NULL,
        at TEXT NOT NULL);
    CREATE TABLE applied_triggers (event_seq INTEGER PRIMARY KEY REFERENCES events(seq), patch TEXT NOT NULL);
    CREATE TABLE budgets (name TEXT PRIMARY KEY, lim REAL NOT NULL, spent REAL NOT NULL DEFAULT 0);
    CREATE TABLE waits (handle TEXT NOT NULL, event_type TEXT NOT NULL, node_id TEXT NOT NULL REFERENCES nodes(id),
        PRIMARY KEY (handle, event_type));
    CREATE TABLE outbox (key TEXT PRIMARY KEY, node_id TEXT NOT NULL, tool TEXT NOT NULL, row_id TEXT,
        arguments TEXT NOT NULL, before TEXT, status TEXT NOT NULL, receipt TEXT, error TEXT, updated_at TEXT NOT NULL)
    """,
    """
    CREATE TABLE llm_replies (node_key TEXT NOT NULL, request_hash TEXT NOT NULL, reply TEXT NOT NULL,
        event_seq INTEGER NOT NULL REFERENCES events(seq), PRIMARY KEY (node_key, request_hash))
    """,
]


class GraphMutationError(ValueError):
    """A patch that would break the graph (unknown node, cycle, change to finished work, …). Nothing is written."""


class BudgetExceeded(RuntimeError):
    def __init__(self, budget: Budget, amount: float):
        super().__init__(f"budget '{budget.name}' would go over its limit: {budget.spent} + {amount} > {budget.limit}")
        self.budget = budget


class InjectedCrash(BaseException):
    """Raised by the fault-injection hook. A BaseException, so no `except Exception` in a worker can swallow it."""


class CrashPoint(BaseModel):
    kind: str
    n: int = 1

    @classmethod
    def from_env(cls) -> "CrashPoint | None":
        raw = os.environ.get("EMAIL_AGENT_CRASH_AT", "").strip()
        if not raw:
            return None
        kind, _, n = raw.partition(":")
        if kind not in get_args(EventKind):
            raise ValueError(f"EMAIL_AGENT_CRASH_AT: unknown event kind {kind!r}")
        return cls(kind=kind, n=int(n or 1))


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _j(value: Any) -> str:
    if isinstance(value, BaseModel):
        return value.model_dump_json()
    return json.dumps(value, sort_keys=True, ensure_ascii=False, default=str)


def _load(text: str | None) -> Any:
    return None if text is None else json.loads(text)


class RunStore:
    """One run. Open with `RunStore(run_dir / RUN_FILE)`; a fresh object on the same file is how a restart looks."""

    def __init__(self, path: Path, *, crash_at: CrashPoint | None = None):
        self.path = path
        self.con: sqlite3.Connection = connect(path, SCHEMA)
        self.crash_at = crash_at
        self._committed: Counter[str] = Counter()
        self._written: list[str] = []
        self._depth = 0

    def close(self) -> None:
        self.con.close()

    # ── transactions and the journal ──────────────────────────────────────────
    @contextmanager
    def transaction(self) -> Iterator[None]:
        """All writes happen inside one of these. Nested use joins the outer transaction."""
        if self._depth:
            self._depth += 1
            try:
                yield
            finally:
                self._depth -= 1
            return
        self._written = []
        with lock_of(self.con):
            self.con.execute("BEGIN IMMEDIATE")
            self._depth = 1
            try:
                yield
            except BaseException:
                self.con.execute("ROLLBACK")
                raise
            else:
                self.con.execute("COMMIT")
            finally:
                self._depth = 0
        self._after_commit(self._written)

    def _after_commit(self, kinds: list[str]) -> None:
        for kind in kinds:
            self._committed[kind] += 1
            if self.crash_at and kind == self.crash_at.kind and self._committed[kind] == self.crash_at.n:
                raise InjectedCrash(f"injected crash after event {kind} #{self.crash_at.n}")

    def append_event(self, kind: EventKind, node_id: str | None = None,
                     payload: dict[str, Any] | None = None) -> JournalEvent:
        """Add one journal line. Must be called inside `transaction()`."""
        if not self._depth:
            raise RuntimeError("append_event outside a transaction")
        at = _now()
        cur = self.con.execute("INSERT INTO events (kind, node_id, payload, at) VALUES (?, ?, ?, ?)",
                               (kind, node_id, _j(payload or {}), at))
        self._written.append(kind)
        return JournalEvent(seq=cur.lastrowid, kind=kind, node_id=node_id, payload=payload or {}, at=at)

    def record_event(self, kind: EventKind, node_id: str | None = None,
                     payload: dict[str, Any] | None = None) -> JournalEvent:
        """A journal line on its own (workers: llm_call_started, sync_finished, …)."""
        with self.transaction():
            return self.append_event(kind, node_id, payload)

    def events(self, after: int = 0) -> list[JournalEvent]:
        rows = self.con.execute("SELECT * FROM events WHERE seq > ? ORDER BY seq", (after,)).fetchall()
        return [self._event(r) for r in rows]

    @staticmethod
    def _event(r: sqlite3.Row) -> JournalEvent:
        return JournalEvent(seq=r["seq"], kind=r["kind"], node_id=r["node_id"], payload=_load(r["payload"]), at=r["at"])

    # ── run lifecycle ─────────────────────────────────────────────────────────
    def _meta(self, key: str) -> str | None:
        row = self.con.execute("SELECT value FROM meta WHERE key = ?", (key,)).fetchone()
        return row["value"] if row else None

    def _set_meta(self, key: str, value: str) -> None:
        self.con.execute("INSERT INTO meta (key, value) VALUES (?, ?) "
                         "ON CONFLICT(key) DO UPDATE SET value = excluded.value", (key, value))

    @property
    def run_id(self) -> str:
        run_id = self._meta("run_id")
        if run_id is None:
            raise KeyError(f"{self.path} holds no run yet")
        return run_id

    def start(self, run_id: str, context: dict[str, Any] | None = None) -> bool:
        """Begin a run. False if this file already holds one (then use `resume`)."""
        if self._meta("run_id") is not None:
            return False
        with self.transaction():
            self._set_meta("run_id", run_id)
            self._set_meta("finished", "0")
            self._set_meta("context", _j(context or {}))
            self.append_event("run_started", None, {"run_id": run_id})
        return True

    def context(self) -> dict[str, Any]:
        return _load(self._meta("context")) or {}

    def resume(self) -> list[str]:
        """After a crash: RUNNING nodes go back to PENDING (their attempt stays in the journal). Returns their ids."""
        with self.transaction():
            reset = [r["id"] for r in self.con.execute("SELECT id FROM nodes WHERE state = ?", (NodeState.RUNNING,))]
            self.con.execute("UPDATE nodes SET state = ?, started_at = NULL WHERE state = ?",
                             (NodeState.PENDING, NodeState.RUNNING))
            self.append_event("run_resumed", None, {"reset_to_pending": reset})
        return reset

    def is_finished(self) -> bool:
        return self._meta("finished") == "1"

    # ── reading the graph ─────────────────────────────────────────────────────
    @staticmethod
    def _node(r: sqlite3.Row) -> NodeRecord:
        return NodeRecord(
            id=r["id"], capability=r["capability"], goal_id=r["goal_id"], input=_load(r["input"]), state=r["state"],
            attempt=r["attempt"], timeout_s=r["timeout_s"], wakes_planner=bool(r["wakes_planner"]),
            result=_load(r["result"]), error=_load(r["error"]), wait=_load(r["wait"]), metadata=_load(r["metadata"]),
            started_at=r["started_at"], ended_at=r["ended_at"])

    def node(self, node_id: str) -> NodeRecord:
        row = self.con.execute("SELECT * FROM nodes WHERE id = ?", (node_id,)).fetchone()
        if row is None:
            raise KeyError(f"unknown node {node_id!r}")
        return self._node(row)

    def node_state(self, node_id: str) -> NodeState:
        return self.node(node_id).state

    def added_by(self) -> dict[str, int]:
        """Node id → the journal event whose patch added it (a planner round's or a fan-out's trigger_event)."""
        return {r["id"]: r["added_by"] for r in self.con.execute("SELECT id, added_by FROM nodes")}

    def snapshot(self) -> GraphSnapshot:
        nodes = {r["id"]: self._node(r) for r in self.con.execute("SELECT * FROM nodes ORDER BY id")}
        edges = [(r["parent"], r["child"]) for r in self.con.execute("SELECT * FROM edges ORDER BY parent, child")]
        return GraphSnapshot(run_id=self.run_id, finished=self.is_finished(), nodes=nodes, edges=edges)

    def ready(self, limit: int) -> list[TaskSpec]:
        """PENDING nodes whose parents have all succeeded, in id order (stable, so the journal replays the same)."""
        rows = self.con.execute(
            """SELECT * FROM nodes n WHERE n.state = ? AND NOT EXISTS (
                   SELECT 1 FROM edges e JOIN nodes p ON p.id = e.parent WHERE e.child = n.id AND p.state != ?)
               ORDER BY n.id LIMIT ?""", (NodeState.PENDING, NodeState.SUCCEEDED, limit)).fetchall()
        return [self._node(r).task() for r in rows]

    # ── node state changes ────────────────────────────────────────────────────
    def mark_running(self, tasks: list[TaskSpec]) -> None:
        with self.transaction():
            for task in tasks:
                cur = self.con.execute(
                    "UPDATE nodes SET state = ?, attempt = attempt + 1, started_at = ? WHERE id = ? AND state = ?",
                    (NodeState.RUNNING, _now(), task.id, NodeState.PENDING))
                if cur.rowcount:
                    attempt = self.con.execute("SELECT attempt FROM nodes WHERE id = ?", (task.id,)).fetchone()[0]
                    self.append_event("node_started", task.id, {"capability": task.capability, "attempt": attempt})

    def record_outcome(self, node_id: str, *, result: dict[str, Any] | None = None,
                       failure: NodeFailure | None = None, patch: GraphPatch | None = None) -> tuple[JournalEvent, bool]:
        """A RUNNING node finished. With `patch` (a fan-out), the new nodes are added in the same transaction.
        Returns the outcome event and whether the planner must look at it."""
        with self.transaction():
            node = self.node(node_id)
            if node.state != NodeState.RUNNING:
                raise GraphMutationError(f"cannot record an outcome for {node_id}: it is {node.state}, not running")
            ok = failure is None
            self.con.execute("UPDATE nodes SET state = ?, result = ?, error = ?, ended_at = ? WHERE id = ?",
                             (NodeState.SUCCEEDED if ok else NodeState.FAILED, _j(result) if ok else None,
                              None if ok else _j(failure), _now(), node_id))
            event = self.append_event("node_succeeded" if ok else "node_failed", node_id,
                                      {"capability": node.capability, **({"result_keys": sorted(result or {})} if ok
                                                                         else {"failure": failure.model_dump() if failure else {}})})
            if patch is not None:                      # fan-out: the expansion is this event's reaction
                self._apply_patch(patch, event.seq, kind="fanned_out")
                return event, False
            if ok and not node.wakes_planner:          # a shard: only its join (or a failure) wakes the planner
                self.con.execute("INSERT INTO applied_triggers (event_seq, patch) VALUES (?, ?)",
                                 (event.seq, _j({"skipped": "this node does not wake the planner"})))
                return event, False
            return event, True

    def record_waiting(self, node_id: str, wait: Deferred) -> JournalEvent:
        """Park a RUNNING node until an outside event; it stops holding a worker."""
        with self.transaction():
            if self.node_state(node_id) != NodeState.RUNNING:
                raise GraphMutationError(f"cannot park {node_id}: it is not running")
            self.con.execute("UPDATE nodes SET state = ?, wait = ? WHERE id = ?", (NodeState.WAITING, _j(wait), node_id))
            self.con.execute("INSERT OR REPLACE INTO waits (handle, event_type, node_id) VALUES (?, ?, ?)",
                             (wait.handle, wait.event_type, node_id))
            return self.append_event("node_waiting", node_id, wait.model_dump())

    def waiting(self) -> list[NodeRecord]:
        return [self._node(r) for r in self.con.execute("SELECT * FROM nodes WHERE state = ? ORDER BY id",
                                                        (NodeState.WAITING,))]

    def complete_waiting(self, handle: str, event_type: str, result: dict[str, Any], *,
                         success: bool = True) -> JournalEvent | None:
        """The outside event arrived with the node's outcome. A second delivery of the same handle is a no-op (None)."""
        with self.transaction():
            row = self.con.execute("SELECT node_id FROM waits WHERE handle = ? AND event_type = ?",
                                   (handle, event_type)).fetchone()
            if row is None:
                return None
            node_id = row["node_id"]
            self.con.execute("DELETE FROM waits WHERE handle = ? AND event_type = ?", (handle, event_type))
            self.append_event("external_event_received", node_id, {"handle": handle, "event_type": event_type})
            ok = success
            failure = None if ok else NodeFailure(kind="error", message=str(result.get("error", "outside event failed")))
            self.con.execute("UPDATE nodes SET state = ?, result = ?, error = ?, wait = NULL, ended_at = ? WHERE id = ?",
                             (NodeState.SUCCEEDED if ok else NodeState.FAILED, _j(result) if ok else None,
                              None if ok else _j(failure), _now(), node_id))
            return self.append_event("node_succeeded" if ok else "node_failed", node_id, {"via": event_type})

    def release_waiting(self, handle: str, event_type: str, note: str = "") -> bool:
        """The outside event says "run the node again" (e.g. after an outbox reconcile): WAITING → PENDING.
        Single use, like `complete_waiting`."""
        with self.transaction():
            row = self.con.execute("SELECT node_id FROM waits WHERE handle = ? AND event_type = ?",
                                   (handle, event_type)).fetchone()
            if row is None:
                return False
            self.con.execute("DELETE FROM waits WHERE handle = ? AND event_type = ?", (handle, event_type))
            self.con.execute("UPDATE nodes SET state = ?, wait = NULL WHERE id = ?", (NodeState.PENDING, row["node_id"]))
            self.append_event("external_event_received", row["node_id"],
                              {"handle": handle, "event_type": event_type, "requeued": True, "note": note})
            return True

    # ── planner patches ───────────────────────────────────────────────────────
    def pending_planner_events(self) -> list[JournalEvent]:
        """Planner-trigger events with no patch recorded yet: replayed on start and after a crash."""
        marks = ",".join("?" * len(PLANNER_TRIGGERS))
        rows = self.con.execute(
            f"""SELECT * FROM events e WHERE e.kind IN ({marks})
                AND NOT EXISTS (SELECT 1 FROM applied_triggers a WHERE a.event_seq = e.seq) ORDER BY e.seq""",
            tuple(sorted(PLANNER_TRIGGERS))).fetchall()
        return [self._event(r) for r in rows]

    def apply_patch(self, patch: GraphPatch, *, trigger_event: int) -> bool:
        """Apply the planner's reaction to one event. False if that event already has one (idempotent on replay)."""
        with self.transaction():
            if self.con.execute("SELECT 1 FROM applied_triggers WHERE event_seq = ?", (trigger_event,)).fetchone():
                return False
            self._apply_patch(patch, trigger_event, kind="graph_patched")
            return True

    def _apply_patch(self, patch: GraphPatch, trigger: int, *, kind: EventKind) -> None:
        if self.is_finished():
            raise GraphMutationError("cannot change a finished graph")
        states = {r["id"]: NodeState(r["state"]) for r in self.con.execute("SELECT id, state FROM nodes")}
        add_ids = [t.id for t in patch.add]
        if len(add_ids) != len(set(add_ids)) or set(add_ids) & states.keys():
            raise GraphMutationError(f"patch adds a node id that already exists: {sorted(set(add_ids) & states.keys())}")
        known = states.keys() | set(add_ids)
        for parent, child in patch.connect:
            if parent not in known or child not in known:
                raise GraphMutationError(f"edge {parent} → {child} names an unknown node")
            if parent == child:
                raise GraphMutationError(f"{parent} cannot wait for itself")
            if child in states and states[child] != NodeState.PENDING:
                raise GraphMutationError(f"{child} has already started, so it cannot wait for {parent}")
        for node_id in (*patch.cancel, *patch.wait, *patch.resume):
            if node_id not in known:
                raise GraphMutationError(f"patch names an unknown node {node_id}")
        for node_id in patch.cancel:
            if node_id in states and states[node_id] not in ACTIVE_STATES:
                raise GraphMutationError(f"only active nodes can be cancelled; {node_id} is {states[node_id]}")
        for node_id in patch.wait:
            if node_id in states and states[node_id] != NodeState.PENDING:
                raise GraphMutationError(f"only pending nodes can be parked; {node_id} is {states[node_id]}")
        for node_id in patch.resume:
            if node_id in states and states[node_id] != NodeState.WAITING:
                raise GraphMutationError(f"only waiting nodes can be resumed; {node_id} is {states[node_id]}")
        parents: dict[str, set[str]] = {n: set() for n in known}
        for r in self.con.execute("SELECT parent, child FROM edges"):
            parents[r["child"]].add(r["parent"])
        for parent, child in patch.connect:
            parents[child].add(parent)
        try:
            TopologicalSorter(parents).prepare()
        except CycleError as e:
            raise GraphMutationError(f"patch would make a cycle: {e.args[1]}") from None

        for t in patch.add:
            self.con.execute(
                """INSERT INTO nodes (id, capability, goal_id, input, state, timeout_s, wakes_planner, metadata, added_by)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (t.id, t.capability, t.goal_id, _j(t.input), NodeState.PENDING, t.timeout_s, int(t.wakes_planner),
                 _j(t.metadata), trigger))
        self.con.executemany("INSERT OR IGNORE INTO edges (parent, child) VALUES (?, ?)", patch.connect)
        for node_id in patch.cancel:
            self._cancel(node_id, patch.reason or "cancelled by the planner")
        for node_id in patch.wait:
            self.con.execute("UPDATE nodes SET state = ? WHERE id = ?", (NodeState.WAITING, node_id))
        for node_id in patch.resume:
            self.con.execute("UPDATE nodes SET state = ? WHERE id = ?", (NodeState.PENDING, node_id))
        if patch.finish:
            for r in self.con.execute("SELECT id FROM nodes WHERE state IN (?, ?, ?)", tuple(ACTIVE_STATES)).fetchall():
                self._cancel(r["id"], "the run finished")
            self._set_meta("finished", "1")
        payload = {"trigger_event": trigger, "reason": patch.reason, "add": add_ids,
                   "connect": [list(e) for e in patch.connect], "cancel": patch.cancel, "wait": patch.wait,
                   "resume": patch.resume, "finish": patch.finish, **patch.metadata}
        self.append_event(kind, None, payload)
        self.con.execute("INSERT INTO applied_triggers (event_seq, patch) VALUES (?, ?)", (trigger, _j(payload)))

    def _cancel(self, node_id: str, reason: str) -> None:
        state = self.node_state(node_id)
        self.con.execute("UPDATE nodes SET state = ?, ended_at = ? WHERE id = ?", (NodeState.CANCELLED, _now(), node_id))
        self.con.execute("DELETE FROM waits WHERE node_id = ?", (node_id,))
        self.append_event("node_cancelled", node_id, {"reason": reason, "was_running": state == NodeState.RUNNING})

    # ── saved LLM replies (reused on resume) ──────────────────────────────────
    def saved_reply(self, node_key: str, request_hash: str) -> str | None:
        row = self.con.execute("SELECT reply FROM llm_replies WHERE node_key = ? AND request_hash = ?",
                               (node_key, request_hash)).fetchone()
        return row["reply"] if row else None

    def finish_llm_call(self, node_id: str | None, request_hash: str, reply_json: str,
                        payload: dict[str, Any]) -> JournalEvent:
        """Journal `llm_call_finished` and save the reply in the same transaction: a crash right after it still finds
        the reply on resume."""
        with self.transaction():
            event = self.append_event("llm_call_finished", node_id, payload)
            self.con.execute("INSERT OR REPLACE INTO llm_replies (node_key, request_hash, reply, event_seq) "
                             "VALUES (?, ?, ?, ?)", (node_id or "planner", request_hash, reply_json, event.seq))
            return event

    # ── outside events (approvals) ────────────────────────────────────────────
    def received(self, handle: str, event_type: str) -> dict[str, Any] | None:
        """The payload of the outside event delivered for this handle, or None if none came yet."""
        for r in self.con.execute("SELECT payload FROM events WHERE kind = 'external_event_received' ORDER BY seq DESC"):
            payload = _load(r["payload"])
            if payload.get("handle") == handle and payload.get("event_type") == event_type:
                return payload
        return None

    def resumes(self) -> int:
        return self.con.execute("SELECT COUNT(*) FROM events WHERE kind = 'run_resumed'").fetchone()[0]

    # ── budgets ───────────────────────────────────────────────────────────────
    def set_budget(self, name: str, limit: float) -> Budget:
        """Create a budget; on resume an existing one keeps what it has spent."""
        with self.transaction():
            self.con.execute("INSERT OR IGNORE INTO budgets (name, lim, spent) VALUES (?, ?, 0)", (name, limit))
        return self.budget(name)

    def budget(self, name: str) -> Budget:
        row = self.con.execute("SELECT * FROM budgets WHERE name = ?", (name,)).fetchone()
        if row is None:
            raise KeyError(f"no budget {name!r}")
        return Budget(name=row["name"], limit=row["lim"], spent=row["spent"])

    def budgets(self) -> list[Budget]:
        return [Budget(name=r["name"], limit=r["lim"], spent=r["spent"])
                for r in self.con.execute("SELECT * FROM budgets ORDER BY name")]

    def spend(self, name: str, amount: float, node_id: str | None = None) -> Budget:
        """Charge a budget, or raise BudgetExceeded (and journal `budget_exhausted`) without charging it."""
        over: Budget | None = None
        with self.transaction():
            budget = self.budget(name)
            if budget.spent + amount > budget.limit:
                over = budget
                self.append_event("budget_exhausted", node_id, {"budget": name, "limit": budget.limit,
                                                                 "spent": budget.spent, "asked": amount})
            else:
                self.con.execute("UPDATE budgets SET spent = spent + ? WHERE name = ?", (amount, name))
        if over is not None:
            raise BudgetExceeded(over, amount)
        return self.budget(name)
