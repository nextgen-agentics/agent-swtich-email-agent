"""Graph contracts (Revision 12): the run graph, its patches, its journal, budgets and the write outbox.

Adapted from S17 `s17code/core/live_graph/core.py` (TaskSpec, GraphPatch, Event, GraphSnapshot, Deferred), which uses
frozen dataclasses; here they are frozen pydantic models, so every value crossing the store, the executor, the planner and
the workers is validated. Added for our needs: per-node timeout and goal, FanOut (code-made shard nodes), NodeFailure
kinds, Budget, OutboxRecord.
"""

from __future__ import annotations

from datetime import datetime, timezone
from enum import StrEnum
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

NODE_ID = r"^[A-Za-z][A-Za-z0-9_.:-]{0,95}$"     # S17's rule plus ':' and '.', for ids like judge:triage:001


def _now() -> datetime:
    return datetime.now(timezone.utc)


class _Frozen(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")


class NodeState(StrEnum):
    PENDING = "pending"          # waits for its parents (or a free worker)
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    CANCELLED = "cancelled"
    WAITING = "waiting"          # parked until an outside event (approval, outbox reconcile); holds no worker


ACTIVE_STATES = {NodeState.PENDING, NodeState.RUNNING, NodeState.WAITING}


class TaskSpec(_Frozen):
    """One unit of work. Ids are chosen by the planner (or by a fan-out) and stay stable on resume."""

    id: str = Field(pattern=NODE_ID)
    capability: str
    input: dict[str, Any] = Field(default_factory=dict)
    goal_id: str | None = None
    timeout_s: float | None = Field(None, gt=0)       # None = the executor's default
    wakes_planner: bool = True                         # False for shards: only their join (or a failure) wakes it
    metadata: dict[str, Any] = Field(default_factory=dict)


class GraphPatch(_Frozen):
    """The only way the graph changes. `connect` holds (parent, child) pairs: the child waits for the parent."""

    add: list[TaskSpec] = Field(default_factory=list)
    connect: list[tuple[str, str]] = Field(default_factory=list)
    cancel: list[str] = Field(default_factory=list)
    wait: list[str] = Field(default_factory=list)
    resume: list[str] = Field(default_factory=list)
    finish: bool = False
    reason: str = ""
    metadata: dict[str, Any] = Field(default_factory=dict)


class FanOut(_Frozen):
    """What a fan-out worker returns: its own result plus the nodes to add (shards and their join). The executor records
    the outcome and adds the nodes in one transaction, so a crash can never leave a half-expanded graph."""

    result: dict[str, Any] = Field(default_factory=dict)
    patch: GraphPatch


APPROVAL = "approval.received"        # a write node waiting for your yes (the approval gate, Stage 6)


class Deferred(_Frozen):
    """A worker started something that finishes through a later outside event (S17). The node becomes WAITING."""

    handle: str
    event_type: str                                    # e.g. "approval.received", "outbox.reconcile"
    metadata: dict[str, Any] = Field(default_factory=dict)


FailureKind = Literal["error", "timeout", "budget", "contract", "unknown_capability", "cancelled"]


class NodeFailure(_Frozen):
    kind: FailureKind
    message: str


EventKind = Literal[
    # run
    "run_started", "run_resumed", "context_built", "run_finished", "run_crashed", "run_interrupted",
    # graph
    "graph_patched", "patch_rejected", "critic_reviewed", "fanned_out",
    # nodes
    "node_started", "node_succeeded", "node_failed", "node_cancelled", "node_waiting", "external_event_received",
    # LLM
    "llm_call_started", "llm_call_finished",
    # local mailbox copy
    "sync_started", "sync_finished", "cache_hit", "search_fallback",
    # writes
    "write_started", "write_completed", "write_failed", "write_uncertain", "write_reconciled",
    # checks (Stage 5)
    "validator_checked", "answer_scored",
    # budgets
    "budget_exhausted",
]

PLANNER_TRIGGERS = {"run_started", "node_succeeded", "node_failed"}


class JournalEvent(_Frozen):
    """One numbered line of the run's append-only journal (`events` table in run.sqlite)."""

    seq: int
    kind: EventKind
    node_id: str | None = None
    payload: dict[str, Any] = Field(default_factory=dict)
    at: datetime


class NodeRecord(_Frozen):
    """One node as stored."""

    id: str
    capability: str
    goal_id: str | None = None
    input: dict[str, Any] = Field(default_factory=dict)
    state: NodeState
    attempt: int = 0
    timeout_s: float | None = None
    wakes_planner: bool = True
    result: dict[str, Any] | None = None
    error: NodeFailure | None = None
    wait: Deferred | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)
    started_at: datetime | None = None
    ended_at: datetime | None = None

    def task(self) -> TaskSpec:
        return TaskSpec(id=self.id, capability=self.capability, input=self.input, goal_id=self.goal_id,
                        timeout_s=self.timeout_s, wakes_planner=self.wakes_planner, metadata=self.metadata)


class GraphSnapshot(_Frozen):
    """What the planner sees: every node and edge, and whether the run is finished."""

    run_id: str
    finished: bool
    nodes: dict[str, NodeRecord]
    edges: list[tuple[str, str]]

    def by_state(self, state: NodeState) -> list[NodeRecord]:
        return [n for n in self.nodes.values() if n.state == state]


class Budget(_Frozen):
    """A saved per-run limit (LLM calls, tokens, planner rounds, nodes). Survives a resume."""

    name: str
    limit: float
    spent: float = 0.0


class OutboxStatus(StrEnum):
    STARTED = "started"          # recorded before sending; still "started" after a crash = the write may or may not have happened
    COMPLETED = "completed"
    FAILED = "failed"


class OutboxRecord(_Frozen):
    """One platform write, recorded before it is sent (adapted from S17 `events/outbox.py`)."""

    key: str                                           # sha256(run_id, node_id, tool, arguments)
    node_id: str
    tool: str
    row_id: str | None = None
    arguments: dict[str, Any]
    before: dict[str, Any] | None = None               # the row's values before the write, for undo and reconcile
    status: OutboxStatus
    receipt: dict[str, Any] | None = None
    error: str | None = None
    updated_at: datetime = Field(default_factory=_now)


ReconcileVerdict = Literal["happened", "not_sent", "changed", "unreadable", "dry_run"]


class ReconcileItem(_Frozen):
    """What reconcile decided for one write a crash (or an unanswered call) left "started"."""

    key: str
    node_id: str
    tool: str
    row_id: str | None = None
    verdict: ReconcileVerdict
    # happened: the live row holds what we sent (or the created row exists) → completed, recorded once
    # not_sent: the live row still holds `before` (or no such row was created) → the record is dropped; sent on rerun
    # changed: the row holds neither (someone else changed it) → failed, never sent again
    # unreadable: the live row could not be read → left uncertain; its node keeps waiting
    # dry_run: a dry run sends nothing → dropped; recorded on rerun
    detail: str = ""


class ReconcileReport(_Frozen):
    items: list[ReconcileItem] = Field(default_factory=list)
    backfilled: int = 0                                # completed writes missing from writes.jsonl, added
    released: list[str] = Field(default_factory=list)  # waiting nodes put back to pending


class RunReport(_Frozen):
    """What one executor pass ended with."""

    run_id: str
    finished: bool
    executed: list[str]
    waiting: list[str]
    stalled: bool = False      # not finished and nothing running or waiting: the planner stopped without finishing
