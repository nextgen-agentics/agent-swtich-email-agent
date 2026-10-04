"""Run-log contracts: one line of runs/<run_id>/steps.jsonl per event, in loop order; and
RunSummary, everything run_report.py reads back from a run folder to write report.md."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Annotated, Literal

from pydantic import BaseModel, Field

from email_agent.contracts.agent import (ActionResult, DecisionOutput, FinalAnswer, Goal, HistoryItem, RunContext,
                                         RunError, RunOutcome, RunRequest, WriteRecord)
from email_agent.contracts.llm import LlmReply, LlmRequest


def _now() -> datetime:
    return datetime.now(timezone.utc)


class LlmStep(BaseModel):
    kind: Literal["llm"] = "llm"
    iter: int
    layer: Literal["perception", "decision"]
    request: LlmRequest
    reply: LlmReply | None = None
    valid: bool
    error: str | None = None            # why the reply failed its contract, if it did
    at: datetime = Field(default_factory=_now)


class GoalsStep(BaseModel):
    kind: Literal["goals"] = "goals"
    iter: int
    goals: list[Goal]
    at: datetime = Field(default_factory=_now)


class DecisionStep(BaseModel):
    kind: Literal["decision"] = "decision"
    iter: int
    goal_id: str
    skill: str
    offered_tools: list[str]
    output: DecisionOutput
    at: datetime = Field(default_factory=_now)


class ActionStep(BaseModel):
    kind: Literal["action"] = "action"
    iter: int
    goal_id: str
    result: ActionResult
    at: datetime = Field(default_factory=_now)


class MemoryStep(BaseModel):
    kind: Literal["memory"] = "memory"
    iter: int
    recorded: HistoryItem
    at: datetime = Field(default_factory=_now)


class ErrorStep(BaseModel):
    """The run crashed or was interrupted; always the last line of steps.jsonl when present."""

    kind: Literal["error"] = "error"
    iter: int
    error: RunError
    at: datetime = Field(default_factory=_now)


Step = Annotated[LlmStep | GoalsStep | DecisionStep | ActionStep | MemoryStep | ErrorStep,
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
    unreadable_lines: int = 0               # steps.jsonl lines that match no step model (older formats)
    writes: list[WriteRecord] = Field(default_factory=list)
    final: FinalAnswer | None = None
    outcome: RunOutcome | None = None
