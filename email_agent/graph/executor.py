"""GraphExecutor: runs the ready nodes of one run in parallel and asks the planner what to do after each outcome.

Adapted from S17 `s17code/core/live_graph/core.py` (LiveGraphExecutor). Kept: ready nodes start as asyncio tasks up to
`max_workers`; the loop wakes on the FIRST task to finish (no wave barrier, so a fast node's follow-up starts while slow
siblings still run); each outcome is journalled and then planned; a cancelled node's late result is dropped; a Deferred
result parks the node as WAITING so it frees its worker; on start every planner event without a patch is replayed.

Added, and why:
  - a time limit per node (`asyncio.timeout`): S17 has none, so a hung worker held its slot forever;
  - typed failures (timeout, budget, contract, error, unknown capability) the planner can read;
  - FanOut: a worker may return shard nodes, added in the same transaction as its outcome; shard successes don't wake
    the planner (only their join does), so planner calls grow with the request, not with the mailbox;
  - an optional "nodes" budget charged per launched node;
  - if the run dies (crash, Ctrl-C), in-flight workers are cancelled before the error goes up, so nothing keeps
    running against a store that is about to be reopened;
  - if the planner itself fails, the run finishes visibly with the reason (S17 does the same after its repair loop).
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any, Awaitable, Callable, Protocol

from pydantic import BaseModel, ValidationError

from email_agent.contracts.graph import (
    Deferred,
    FanOut,
    GraphPatch,
    GraphSnapshot,
    JournalEvent,
    NodeFailure,
    NodeState,
    RunReport,
    TaskSpec,
)
from email_agent.graph.store import BudgetExceeded, RunStore

log = logging.getLogger(__name__)

WorkerResult = BaseModel | dict[str, Any] | Deferred | FanOut
Worker = Callable[[TaskSpec], Awaitable[WorkerResult]]
NODE_BUDGET = "nodes"


class Planner(Protocol):
    async def plan(self, graph: GraphSnapshot, event: JournalEvent) -> GraphPatch: ...


class _Outcome(BaseModel):
    result: dict[str, Any] | None = None
    failure: NodeFailure | None = None
    deferred: Deferred | None = None
    fanout: FanOut | None = None


class GraphExecutor:
    def __init__(self, store: RunStore, planner: Planner, workers: dict[str, Worker], *,
                 max_workers: int = 6, default_timeout_s: float = 300.0):
        if max_workers < 1:
            raise ValueError("max_workers must be at least 1")
        self.store, self.planner, self.workers = store, planner, workers
        self.max_workers, self.default_timeout_s = max_workers, default_timeout_s

    async def run(self, *, run_id: str | None = None, context: dict[str, Any] | None = None,
                  resume: bool = False) -> RunReport:
        """Start a new run (`run_id`) or continue a stopped one (`resume=True`), until it is finished, waiting on an
        outside event, or stalled."""
        if resume:
            self.store.resume()
        else:
            if run_id is None:
                raise ValueError("a new run needs a run_id")
            if not self.store.start(run_id, context):
                raise ValueError(f"{self.store.path} already holds run {self.store.run_id}; use resume=True")
        await self._replay_pending_planner_events()

        executed: list[str] = []
        in_flight: dict[asyncio.Task[_Outcome], TaskSpec] = {}
        try:
            while True:
                if not self.store.is_finished():
                    self._launch(in_flight)
                if not in_flight:
                    break
                done, _ = await asyncio.wait(in_flight, return_when=asyncio.FIRST_COMPLETED)
                for future in sorted(done, key=lambda f: in_flight[f].id):     # stable order → replayable journal
                    task = in_flight.pop(future)
                    try:
                        outcome = future.result()
                    except asyncio.CancelledError:
                        continue                       # the graph cancelled it; the store already journalled that
                    if self.store.node_state(task.id) == NodeState.CANCELLED:
                        continue                       # cancelled while it ran: its late result is dropped
                    executed.append(task.id)
                    await self._record(task, outcome)
                    self._cancel_cancelled(in_flight)
                if self.store.is_finished():
                    self._cancel_cancelled(in_flight, everything=True)
        except BaseException:
            await self._abandon(in_flight)
            raise
        return self._report(executed)

    # ── launching and running nodes ───────────────────────────────────────────
    def _launch(self, in_flight: dict[asyncio.Task[_Outcome], TaskSpec]) -> None:
        capacity = self.max_workers - len(in_flight)
        if capacity <= 0:
            return
        ready = self.store.ready(limit=capacity)
        if not ready:
            return
        self.store.mark_running(ready)
        for task in ready:
            in_flight[asyncio.create_task(self._execute(task), name=f"node:{task.id}")] = task

    async def _execute(self, task: TaskSpec) -> _Outcome:
        worker = self.workers.get(task.capability)
        if worker is None:
            return _Outcome(failure=NodeFailure(kind="unknown_capability", message=f"no worker for {task.capability!r}"))
        try:
            if any(b.name == NODE_BUDGET for b in self.store.budgets()):
                self.store.spend(NODE_BUDGET, 1, node_id=task.id)
            async with asyncio.timeout(task.timeout_s or self.default_timeout_s):
                value = await worker(task)
        except TimeoutError:
            return _Outcome(failure=NodeFailure(kind="timeout",
                                                message=f"ran longer than {task.timeout_s or self.default_timeout_s}s"))
        except BudgetExceeded as e:
            return _Outcome(failure=NodeFailure(kind="budget", message=str(e)))
        except ValidationError as e:
            return _Outcome(failure=NodeFailure(kind="contract", message=str(e)[:2000]))
        except Exception as e:  # noqa: BLE001 — a worker failure becomes a planner-visible outcome
            return _Outcome(failure=NodeFailure(kind="error", message=f"{type(e).__name__}: {e}"[:2000]))
        if isinstance(value, Deferred):
            return _Outcome(deferred=value)
        if isinstance(value, FanOut):
            return _Outcome(fanout=value)
        if isinstance(value, BaseModel):
            return _Outcome(result=value.model_dump(mode="json"))
        if isinstance(value, dict):
            return _Outcome(result=value)
        return _Outcome(failure=NodeFailure(kind="contract", message=f"worker returned {type(value).__name__}"))

    async def _record(self, task: TaskSpec, outcome: _Outcome) -> None:
        if outcome.deferred is not None:
            self.store.record_waiting(task.id, outcome.deferred)
            return
        if outcome.fanout is not None:
            patch = self._rewire(task.id, outcome.fanout.patch)
            try:
                event, needs_plan = self.store.record_outcome(task.id, result=outcome.fanout.result, patch=patch)
            except ValueError as e:            # a bad fan-out patch fails the node instead of the run
                event, needs_plan = self.store.record_outcome(
                    task.id, failure=NodeFailure(kind="contract", message=f"fan-out patch refused: {e}"))
        else:
            event, needs_plan = self.store.record_outcome(task.id, result=outcome.result, failure=outcome.failure)
        if needs_plan and not self.store.is_finished():
            await self._plan(event)

    def _rewire(self, node_id: str, patch: GraphPatch) -> GraphPatch:
        """Whatever already waits for the fan-out node must also wait for the fan-out's final node (its
        `metadata["final"]`, else the last node it adds): a fan-out "succeeds" when it has only *started* its work.
        (Found live: an answer waiting on judge_threads ran before the shards and ended the goal.)"""
        if not patch.add:
            return patch
        final = patch.metadata.get("final") or patch.add[-1].id
        snap = self.store.snapshot()
        waiting = [child for parent, child in snap.edges
                   if parent == node_id and snap.nodes[child].state == NodeState.PENDING]
        return patch.model_copy(update={"connect": [*patch.connect, *((final, c) for c in waiting)]}) if waiting else patch

    # ── planning ──────────────────────────────────────────────────────────────
    async def _plan(self, event: JournalEvent) -> None:
        try:
            patch = await self.planner.plan(self.store.snapshot(), event)
            self.store.apply_patch(patch, trigger_event=event.seq)
        except Exception as e:  # noqa: BLE001 — the run ends visibly rather than hanging
            log.warning("planner failed on event %s: %s", event.seq, e)
            self.store.record_event("patch_rejected", None, {"trigger_event": event.seq,
                                                             "error": f"{type(e).__name__}: {e}"[:2000]})
            self.store.apply_patch(GraphPatch(finish=True, reason=f"planner failed: {type(e).__name__}: {e}"[:500],
                                              metadata={"planner_failed": True}), trigger_event=event.seq)

    async def _replay_pending_planner_events(self) -> None:
        for event in self.store.pending_planner_events():
            if self.store.is_finished():
                return
            await self._plan(event)

    # ── cancellation and the end of a pass ────────────────────────────────────
    def _cancel_cancelled(self, in_flight: dict[asyncio.Task[_Outcome], TaskSpec], *, everything: bool = False) -> None:
        """The store records a cancel first; then the asyncio task is cancelled (safe even if we die in between)."""
        for future, task in list(in_flight.items()):
            if everything or self.store.node_state(task.id) == NodeState.CANCELLED:
                future.cancel()

    @staticmethod
    async def _abandon(in_flight: dict[asyncio.Task[_Outcome], TaskSpec]) -> None:
        for future in in_flight:
            future.cancel()
        if in_flight:
            await asyncio.gather(*in_flight, return_exceptions=True)

    def _report(self, executed: list[str]) -> RunReport:
        snap = self.store.snapshot()
        waiting = [n.id for n in snap.by_state(NodeState.WAITING)]
        return RunReport(run_id=snap.run_id, finished=snap.finished, executed=executed, waiting=waiting,
                         stalled=not snap.finished and not waiting)
