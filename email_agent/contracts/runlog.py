"""Run-log contracts: one line of runs/<run_id>/steps.jsonl per event, in loop order; and
RunSummary, everything run_report.py reads back from a run folder to write report.md."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Annotated, Literal

from pydantic import BaseModel, Field

from email_agent.contracts.agent import (
    ActionResult,
    FinalAnswer,
    Goal,
    RunContext,
    RunError,
    RunOutcome,
    RunRequest,
    WriteRecord,
)
from email_agent.contracts.llm import LlmReply, LlmRequest
from email_agent.contracts.mirror import SyncReport


def _now() -> datetime:
    return datetime.now(timezone.utc)


class LlmStep(BaseModel):
    kind: Literal["llm"] = "llm"
    iter: int
    layer: Literal["goals", "planner", "judge", "answer", "critic", "validator"]
    node_id: str | None = None          # the graph node that made the call (Revision 12)
    request: LlmRequest
    reply: LlmReply | None = None
    valid: bool
    error: str | None = None            # why the reply failed its contract, if it did
    at: datetime = Field(default_factory=_now)


class ActionStep(BaseModel):
    kind: Literal["action"] = "action"
    iter: int
    goal_id: str
    result: ActionResult
    at: datetime = Field(default_factory=_now)


class PlanStep(BaseModel):
    """One planner round (Revision 12): what woke it, the goals (first round), what it added, what was rejected."""

    kind: Literal["plan"] = "plan"
    iter: int                                  # planner round
    trigger: str                               # "run_started", or "<node id> succeeded/failed"
    goals: list[Goal] = Field(default_factory=list)
    added: list[str] = Field(default_factory=list)          # "id: capability(args)" per new node
    cancelled: list[str] = Field(default_factory=list)
    rejected: list[str] = Field(default_factory=list)       # why earlier attempts this round were refused
    finished: bool = False
    reason: str = ""
    at: datetime = Field(default_factory=_now)


class NodeStep(BaseModel):
    """One graph node finished, failed, parked or was cancelled (Revision 12)."""

    kind: Literal["node"] = "node"
    iter: int
    node_id: str
    capability: str
    goal_id: str | None = None
    state: Literal["succeeded", "failed", "waiting", "cancelled", "fanned_out"]
    summary: str = ""                          # a short line: counts, the answer's length, the error
    seconds: float = 0.0
    at: datetime = Field(default_factory=_now)


class SyncStep(BaseModel):
    """The local mailbox copy was brought up to date (once per run, before the first mailbox read)."""

    kind: Literal["sync"] = "sync"
    iter: int
    report: SyncReport
    at: datetime = Field(default_factory=_now)


class ErrorStep(BaseModel):
    """The run crashed or was interrupted; always the last line of steps.jsonl when present."""

    kind: Literal["error"] = "error"
    iter: int
    error: RunError
    at: datetime = Field(default_factory=_now)


Step = Annotated[LlmStep | ActionStep | SyncStep | PlanStep | NodeStep | ErrorStep,
                 Field(discriminator="kind")]


class StepLine(BaseModel):
    """Wrapper used to read steps.jsonl back with the right type per line."""

    step: Step


class RunSummary(BaseModel):
    """One run folder read back from disk. Every file is optional, so runs from before
    request.json / crash handling existed (2026-10-03) can still be summarised."""

    run_id: str
    run_dir: str
    request: RunRequest | None = None
    context: RunContext | None = None
    steps: list[Step] = Field(default_factory=list)
    unreadable_lines: int = 0               # steps.jsonl lines that match no step model (older formats, e.g. the
                                            # Perception/Decision loop's goals/decision/memory lines before Revision 12)
    writes: list[WriteRecord] = Field(default_factory=list)
    final: FinalAnswer | None = None
    outcome: RunOutcome | None = None
