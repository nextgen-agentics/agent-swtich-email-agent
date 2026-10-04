"""Harness contracts: the correct answers (ground truth) and, later, tasks and verdicts.

Ground truth is decided by you, in the web UI. The script only proposes candidates;
a task whose ground truth still has undecided items is scored `unevaluated`, never a pass.
"""

from __future__ import annotations

from datetime import date, datetime, timezone
from typing import Any, Literal

from pydantic import BaseModel, Field, model_validator

Proposal = Literal["needs_reply", "fyi_or_thanks", "unclear", "waiting_on_them", "no_inbound"]


class NeedsReplyCandidate(BaseModel):
    thread_id: str
    subject: str | None = None
    mailbox: str | None = None             # which of our addresses; None in files from before 2026-10-03
    counterpart: str | None = None
    newest_real_from: Literal["us", "them"] | None = None
    newest_real_at: str | None = None
    newest_real_text: str = ""
    mirror_copies: int = 0
    proposal: Proposal                     # the script's suggestion (plain rules, not an LLM)
    reason: str
    needs_reply: bool | None = None        # YOU decide: true = needs my reply, false = does not
    note: str | None = None                # your remark, optional


class GroundTruth(BaseModel):
    instance: str
    as_of: date
    generated_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    our_mailboxes: list[str]
    needs_reply: list[NeedsReplyCandidate]

    def undecided(self) -> list[NeedsReplyCandidate]:
        return [c for c in self.needs_reply if c.needs_reply is None]

    def expected_needs_reply(self) -> set[str]:
        return {c.thread_id for c in self.needs_reply if c.needs_reply}

    def mailbox_of(self, c: NeedsReplyCandidate) -> str | None:
        """Files from before 2026-10-03 cover one mailbox and do not name it per item."""
        return c.mailbox or (self.our_mailboxes[0] if len(self.our_mailboxes) == 1 else None)


# ── answer keys for the other features (harness/ground_truth/<feature>/<instance>.yaml) ──────────────────────

class AnswerKey(BaseModel):
    """What every answer-key file shares. The script proposes, you decide; null = undecided = unevaluated."""

    instance: str
    as_of: date
    generated_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    our_mailboxes: list[str]


PriceProposal = Literal["agreed", "lost", "pending", "quote_only", "unclear"]


class PriceCandidate(BaseModel):
    thread_id: str
    subject: str | None = None
    mailbox: str | None = None
    counterpart: str | None = None
    references: list[str] = Field(default_factory=list)
    our_price_lines: list[str] = Field(default_factory=list)
    their_last_text: str = ""
    proposal: PriceProposal
    reason: str
    agreed: bool | None = None             # YOU decide: true = they agreed our price in this conversation
    reference: str | None = None           # our quote reference, as the agent should record it
    unit_price: float | None = None        # pre-filled from our price line; correct it if wrong
    total: float | None = None
    note: str | None = None


class PriceAnswerKey(AnswerKey):
    candidates: list[PriceCandidate]


class FollowUpCandidate(BaseModel):
    thread_id: str
    subject: str | None = None
    mailbox: str | None = None
    counterpart: str | None = None
    our_last_at: str | None = None
    newest_real_text: str = ""
    proposal: Literal["waiting_on_them", "not_waiting"]
    reason: str
    needs_follow_up: bool | None = None    # YOU decide: true = a "no reply" reminder belongs on it
    note: str | None = None


class FollowUpAnswerKey(AnswerKey):
    candidates: list[FollowUpCandidate]


Importance = Literal["low", "normal", "high"]
Category = Literal["important", "team", "vip", "news", "social", "other"]


class SortCandidate(BaseModel):
    thread_id: str
    subject: str | None = None
    mailbox: str | None = None
    counterpart: str | None = None
    newest_real_from: Literal["us", "them"] | None = None
    newest_real_text: str = ""
    proposed_importance: Importance
    proposed_category: Category
    reason: str
    importance: Importance | Literal["any"] | None = None          # YOU decide (any = either answer is fine)
    split_category: Category | Literal["any"] | None = None
    note: str | None = None


class SortAnswerKey(AnswerKey):
    candidates: list[SortCandidate]


# ── tasks, saved runs, verdicts ───────────────────────────────────────────────

PredicateName = Literal["needs_reply_flagged", "price_agreements_recorded", "summaries_written", "followups_created",
                        "memory_recorded", "inbox_sorted", "refused_without_writes"]
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
    as_of: date | None = None              # pin "today"; None = the real date
    mailboxes: list[str] | None = None     # addresses to work in; None = the instance default (config.INSTANCES)
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
    """The state of one conversation the checks look at, as the harness read it from the database (the name is
    from when only flags were checked; saved runs from before 2026-10-03 hold just the two flag fields)."""

    flag_status: str | None = None
    flag_due_date: str | None = None
    is_starred: bool | None = None
    importance: str | None = None
    split_category: str | None = None
    summary: str | None = None
    summary_updated_at: str | None = None


class SavedRun(BaseModel):
    """Written to harness_runs/<batch>/<task>.json right after the run — before any scoring."""

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
