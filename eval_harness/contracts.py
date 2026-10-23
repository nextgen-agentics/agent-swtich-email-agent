"""Contracts of our test suite (eval_harness): the correct answers (answer keys), tasks, verdicts, results.json.

Answer keys are decided by you. A script only suggests; a task whose key still has undecided items in its
mailboxes is scored `unevaluated`, never a pass, and is left out of the official run.
"""

from __future__ import annotations

from datetime import date, datetime, timezone
from typing import Any, Literal

from pydantic import BaseModel, Field, StrictBool, model_validator

# ── answer keys (eval_harness/ground_truth/needs-reply/<instance>.yaml and price/<instance>.yaml) ───────────────
# Only what the checks read, plus the subject so you can find the conversation. The context to decide each item (their
# text, our price lines, a suggestion) is in docs/project/answer-key-decisions.md, written by
# scripts/agent/propose_ground_truth.py. null = undecided = the task is not run officially and scores `unevaluated`.

class NeedsReplyItem(BaseModel):
    thread_id: str
    mailbox: str                           # which of our addresses the conversation is in
    subject: str | None = None
    needs_reply: bool | None = None        # YOU decide: true = needs my reply, false = does not
    note: str | None = None                # your remark, optional


class PriceItem(BaseModel):
    thread_id: str
    mailbox: str
    subject: str | None = None
    agreed: bool | None = None             # YOU decide: true = they agreed our price in this conversation
    message_id: str | None = None          # their accepting message (the mail the agent must find); None = not checked
    reference: str | None = None           # our quote reference, as the agent should record it; None = not checked
    unit_price: float | None = None
    total: float | None = None
    note: str | None = None


class AnswerKey(BaseModel):
    instance: str
    as_of: date                            # the day the mail was read for this key
    our_mailboxes: list[str]


class NeedsReplyKey(AnswerKey):
    items: list[NeedsReplyItem]


class PriceKey(AnswerKey):
    items: list[PriceItem]


# ── tasks, saved runs, verdicts ───────────────────────────────────────────────

PredicateName = Literal["needs_reply_flagged", "price_agreements_recorded", "summaries_written", "memory_recorded",
                        "refused_without_writes"]
VerdictStatus = Literal["approve", "revise", "unevaluated"]


class PredicateSpec(BaseModel):
    name: PredicateName
    params: dict[str, Any] = Field(default_factory=dict)


class Task(BaseModel):
    id: str
    prompt: str
    instance: str
    predicate: PredicateName | None = None             # one check (the original form) …
    predicates: list[PredicateSpec] = Field(default_factory=list)   # … or several; the verdict is the worst
    mailboxes: list[str] | None = None     # addresses to work in; None = the instance default (config.INSTANCES)
    max_minutes: float | None = Field(default=None, gt=0)   # official run: this task's time cap (None = the default)
    note: str | None = None

    @model_validator(mode="after")
    def _one_form(self) -> "Task":
        if self.predicate and not self.predicates:
            self.predicates = [PredicateSpec(name=self.predicate)]
        if not self.predicates:
            raise ValueError(f"task {self.id}: give `predicate` or `predicates`")
        return self


class TaskFile(BaseModel):
    tasks: list[Task] = Field(min_length=1)


class ThreadFlag(BaseModel):
    """The state of one conversation the checks look at, as our test suite read it from the database (the name is
    from when only flags were checked; saved runs from before 2026-10-03 hold just the two flag fields)."""

    flag_status: str | None = None
    flag_due_date: str | None = None
    is_starred: bool | None = None
    importance: str | None = None
    split_category: str | None = None
    summary: str | None = None
    summary_updated_at: str | None = None


class SavedRun(BaseModel):
    """Written to eval_runs/<batch>/<task>.json right after the run — before any scoring."""

    task: Task
    run_id: str | None = None
    run_dir: str | None = None
    today: date
    provider: str | None = None                                   # the model that ran it
    model: str | None = None
    route: str | None = None                                      # every LLM option in priority order
    served_by: dict[str, int] = Field(default_factory=dict)       # option → LLM calls it answered (from outcome)
    dry_run: bool = False                                         # writes were recorded, not sent → unevaluated
    before: dict[str, ThreadFlag] = Field(default_factory=dict)   # thread id → flags before the run
    started_at_server: datetime | None = None     # the server's clock (HTTP Date) just before the run, minus 2 s
    me_id: str | None = None                      # our login's user id: whose rows are "ours"
    stopped: str | None = None                                    # FinalAnswer.stopped: done / crashed / …
    error: str | None = None                                      # the run crashed or was interrupted
    saved_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))


class CheckResult(BaseModel):
    """One check's own verdict; a task with several checks gets the worst of them."""

    name: PredicateName
    status: VerdictStatus
    reason: str
    details: dict[str, list[str]] = Field(default_factory=dict)


class Verdict(BaseModel):
    task_id: str
    instance: str
    run_id: str | None
    status: VerdictStatus
    reason: str
    details: dict[str, list[str]] = Field(default_factory=dict)
    run_stopped: str | None = None         # how the agent run ended (done / crashed / …), shown in report.md
    served_by: str | None = None           # which model(s) answered, so a verdict can be tied to a model
    checks: list[CheckResult] = Field(default_factory=list)


class ScoreReport(BaseModel):
    batch: str
    scored_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    verdicts: list[Verdict]

    @property
    def counts(self) -> dict[str, int]:
        out = {"approve": 0, "revise": 0, "unevaluated": 0}
        for v in self.verdicts:
            out[v.status] += 1
        return out


# ── the official run's results file (Revision 18; docs/project/official-runs.md) ──────────────────────────────

class ResultTask(BaseModel):
    """One row of results.json, in the AgentSwitch runner's format: our verdict approve → passed; revise and
    unevaluated → not passed (unevaluated is never a pass)."""

    id: str = Field(min_length=1)                     # "<instance>:<task id>"
    title: str = Field(min_length=1)
    passed: StrictBool                                # true or false only ("yes", 1 … are refused, as the runner does)
    score: float | None = Field(default=None, ge=0.0, le=1.0)
    evidence: str = Field(default="", max_length=500)


class ResultsFile(BaseModel):
    """results.json: what the AgentSwitch runner reads after an official run (1 to 200 tasks)."""

    tasks: list[ResultTask] = Field(min_length=1, max_length=200)
    summary: str
