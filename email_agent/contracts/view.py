"""Run-page contracts (Revision 15): everything runs/<run_id>/view.html and a harness batch's view.html show.

The page is a projection of files the run already saved (run.sqlite, steps.jsonl, writes.jsonl, final.json,
outcome.json, spans.jsonl, undo.jsonl). record/run_view.py builds a RunView, the page's script draws it; harness/view.py
builds a BatchView. Nothing here is read back by the agent.
"""

from __future__ import annotations

from datetime import date, datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

from email_agent.contracts.agent import Goal, RunError
from email_agent.contracts.graph import Budget
from email_agent.contracts.llm import ChatMessage, Usage
from email_agent.contracts.mirror import SyncReport


class ViewHeader(BaseModel):
    run_id: str
    request: str
    instance: str
    mailboxes: list[str] = Field(default_factory=list)
    today: date | None = None
    dry_run: bool | None = None                 # None = unknown (the run stopped before request.json existed)
    stopped: str                                # done, interrupted, crashed, waiting, …, or "unknown" (killed hard)
    reason: str | None = None
    started_at: datetime | None = None
    ended_at: datetime | None = None
    seconds: float | None = None
    model: str | None = None
    route: str | None = None
    served_by: dict[str, int] = Field(default_factory=dict)
    usage: Usage = Field(default_factory=Usage)
    calls: int = 0
    budgets: list[Budget] = Field(default_factory=list)
    error: RunError | None = None
    next_step: str | None = None                # the command that continues, approves or rejects the run
    has_graph: bool = False                     # False for runs of the old loop (no run.sqlite)


class ViewCall(BaseModel):
    """One model call: what was asked, what came back, who answered."""

    n: int                                      # 1-based, in the order made
    layer: str                                  # goals, planner, judge, critic, validator, answer
    node_id: str | None = None                  # None = the planner itself
    round: int                                  # planner round the run was in
    at: datetime
    valid: bool
    error: str | None = None
    answered_by: str | None = None              # provider/model key #n
    fallback_from: list[str] = Field(default_factory=list)
    reused: bool = False
    usage: Usage = Field(default_factory=Usage)
    seconds: float | None = None
    system: str
    messages: list[ChatMessage]
    response_schema: dict[str, Any] | None = None
    reply: str | None = None


class ViewNode(BaseModel):
    id: str
    capability: str
    goal_id: str | None = None
    state: str
    attempt: int = 0
    input: dict[str, Any] = Field(default_factory=dict)
    result: dict[str, Any] | None = None
    error: str | None = None
    waiting_on: str | None = None
    started_at: datetime | None = None
    ended_at: datetime | None = None
    seconds: float | None = None
    added_by: str | None = None                 # "planner round 2", "fan-out of judge_g1"
    fanned_from: str | None = None              # the judge task a batch, join, check or write task came from
    calls: list[int] = Field(default_factory=list)      # ViewCall.n made by this task
    writes: list[int] = Field(default_factory=list)     # ViewWrite.n made by this task


class ViewRound(BaseModel):
    n: int
    trigger: str
    at: datetime
    goals: list[Goal] = Field(default_factory=list)
    added: list[str] = Field(default_factory=list)
    cancelled: list[str] = Field(default_factory=list)
    rejected: list[str] = Field(default_factory=list)
    finished: bool = False
    reason: str = ""
    calls: list[int] = Field(default_factory=list)


class ViewCritic(BaseModel):
    """The evidence check before an answer or a refusal."""

    node_id: str | None = None
    goal_id: str | None = None
    ready: bool
    overruled: bool = False
    ending: str = ""
    reason: str = ""
    missing: list[str] = Field(default_factory=list)
    at: datetime


class ViewValidator(BaseModel):
    """The second model's check of the judges' verdicts."""

    node_id: str | None = None
    skill: str | None = None
    judge_model: str | None = None
    validator_model: str | None = None
    checked: int = 0
    agreed: int = 0
    held: list[Any] = Field(default_factory=list)
    possible_misses: list[Any] = Field(default_factory=list)


class ViewScore(BaseModel):
    """The verifier's score of one answer (evidence only, never a pass mark)."""

    node_id: str | None = None
    score: float | None = None
    issues: list[str] = Field(default_factory=list)


class ViewWrite(BaseModel):
    n: int
    tool: str
    row_id: str | None = None
    fields: dict[str, Any] = Field(default_factory=dict)
    before: dict[str, Any] = Field(default_factory=dict)
    dry_run: bool = False
    node_id: str | None = None
    status: str | None = None                   # the outbox's status: started, completed, failed, uncertain
    receipt: dict[str, Any] | None = None
    error: str | None = None
    undo: str | None = None                     # what undo_run.py did with it, when the run was undone
    at: datetime | None = None


class ViewProblem(BaseModel):
    where: Literal["run", "task", "call", "write", "planner", "check"]
    ref: str | None = None                      # task id, "call 3", "write 2", "round 1"
    title: str
    detail: str = ""


class ViewSpan(BaseModel):
    name: str
    kind: str
    depth: int
    start_ms: float                             # from the run's first span
    end_ms: float
    status: str = "unset"
    message: str | None = None


class UndoMark(BaseModel):
    """One line of undo.jsonl, as far as the page needs it (the full contract lives with scripts/agent/undo_run.py)."""

    model_config = ConfigDict(extra="ignore")

    tool: str
    row_id: str | None = None
    action: str
    detail: str = ""


class RunView(BaseModel):
    kind: Literal["run"] = "run"
    generated_at: datetime
    header: ViewHeader
    goals: list[Goal] = Field(default_factory=list)
    answer: str | None = None
    nodes: list[ViewNode] = Field(default_factory=list)
    edges: list[tuple[str, str]] = Field(default_factory=list)
    rounds: list[ViewRound] = Field(default_factory=list)
    critics: list[ViewCritic] = Field(default_factory=list)
    validators: list[ViewValidator] = Field(default_factory=list)
    scores: list[ViewScore] = Field(default_factory=list)
    calls: list[ViewCall] = Field(default_factory=list)
    writes: list[ViewWrite] = Field(default_factory=list)
    problems: list[ViewProblem] = Field(default_factory=list)
    spans: list[ViewSpan] = Field(default_factory=list)
    house_rules: str | None = None
    history: list[str] = Field(default_factory=list)
    sync: SyncReport | None = None
    older_lines: list[dict[str, Any]] = Field(default_factory=list)   # old-loop log lines no current model reads


class BatchRow(BaseModel):
    task_id: str
    instance: str
    status: str                                 # approve, revise, unevaluated
    reason: str
    run_stopped: str | None = None
    served_by: str | None = None
    run_id: str | None = None
    page: str | None = None                     # relative link to the run's view.html, when it exists
    checks: list[dict[str, Any]] = Field(default_factory=list)


class BatchView(BaseModel):
    kind: Literal["batch"] = "batch"
    generated_at: datetime
    batch: str
    scored_at: datetime | None = None
    counts: dict[str, int] = Field(default_factory=dict)
    rows: list[BatchRow] = Field(default_factory=list)
