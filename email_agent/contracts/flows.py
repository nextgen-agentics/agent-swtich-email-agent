"""Flow contracts (Revision 12, Stage 4): what one judging shard returns per conversation, and the write plan the join
builds from the verdicts. One verdict model per bulk skill; the shard's reply is a list of them, bound to the model's
JSON schema and checked against the shard's own conversation ids."""

from __future__ import annotations

from collections.abc import Sequence
from datetime import date
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field


class Verdict(BaseModel):
    """What every skill's verdict on one conversation has (each skill adds its own fields)."""

    model_config = ConfigDict(extra="forbid")

    thread_id: str


class ShardReply(BaseModel):
    """What one judging call returns: one verdict per conversation (each skill narrows the verdict type)."""

    verdicts: Sequence[Verdict]


class TriageVerdict(Verdict):
    needs_reply: bool
    why: str = Field(max_length=240, description="A few words: what they ask of us, or why no reply is needed.")


class SortVerdict(Verdict):
    importance: Literal["low", "normal", "high"]
    split_category: Literal["important", "team", "vip", "news", "social", "other"]
    why: str = Field(max_length=200)


class SummaryVerdict(Verdict):
    summary: str = Field(min_length=20, max_length=400)


class FollowUpVerdict(Verdict):
    waiting_on_them: bool
    remind_at: date | None = Field(None, description="YYYY-MM-DD, after today; only when waiting_on_them.")
    note: str | None = Field(None, max_length=200, description="A few words: what we are waiting for.")


class PriceVerdict(Verdict):
    status: Literal["agreed", "lost", "open", "quote_only", "not_price"]
    why: str = Field(max_length=300)
    agreement_message_id: str | None = Field(None, description="Only when agreed: their accepting message's id.")
    party_id: str | None = None
    reference: str | None = None
    item: str | None = None
    quantity: float | None = None
    unit_price: float | None = None
    total: float | None = None
    agreed_on: str | None = Field(None, description="YYYY-MM-DD of their accepting message.")
    record_check: str | None = None


class TriageShard(ShardReply):
    verdicts: list[TriageVerdict]


class SortShard(ShardReply):
    verdicts: list[SortVerdict]


class SummaryShard(ShardReply):
    verdicts: list[SummaryVerdict]


class FollowUpShard(ShardReply):
    verdicts: list[FollowUpVerdict]


class PriceShard(ShardReply):
    verdicts: list[PriceVerdict]


class PlannedWrite(BaseModel):
    """One platform write the join decided on; `tool` is the platform tool or our batch tool's underlying call."""

    tool: Literal["EmailThread.update", "AgentMemory.create", "EmailReminder.create"]
    fields: dict[str, Any]
    thread_id: str
    why: str = ""


class WritePlan(BaseModel):
    skill: str
    writes: list[PlannedWrite] = Field(default_factory=list)
    unchanged: list[str] = Field(default_factory=list)     # conversations already right: nothing to write
    problems: list[str] = Field(default_factory=list)      # verdicts the code checks refused (and why)


class ShardResult(BaseModel):
    skill: str
    verdicts: list[dict[str, Any]]
    cached: int = 0                                         # verdicts taken from the verdict cache (no LLM call)
    judged: int = 0                                         # verdicts from this shard's LLM call
    model: str | None = None                                # who judged (the validator must be someone else)


class JoinResult(BaseModel):
    """What the planner sees after the shards (the first fields; long lists come last, so clipping cuts only them)."""

    skill: str
    candidates: int
    counts: dict[str, int]                                  # e.g. needs_reply: 9, no_reply: 3
    writes_planned: int
    problems: list[str] = Field(default_factory=list)
    verdicts: list[dict[str, Any]] = Field(default_factory=list)
    plan: WritePlan


# ── checks (Stage 5) ─────────────────────────────────────────────────────────

class HeldVerdict(BaseModel):
    """A conversation the two models judged differently."""

    thread_id: str
    subject: str | None = None
    judge: dict[str, Any]
    validator: dict[str, Any]


class ValidatorReport(BaseModel):
    """What the cross-model check found (adapted from S17 `validate_work`: a different model, read-only, hostile)."""

    skill: str
    judge_model: str | None = None
    validator_model: str | None = None
    checked: int = 0
    agreed: int = 0
    held: list[HeldVerdict] = Field(default_factory=list)            # write-causing verdicts the validator disputes
    possible_misses: list[HeldVerdict] = Field(default_factory=list) # sampled "no" verdicts the validator calls "yes"
    skipped: str | None = None                                       # why the check did not run
    plan: WritePlan                                                  # the write plan without the held conversations


class CriticVerdict(BaseModel):
    """The evidence-readiness critic's reply (S17 `_review_prompt`)."""

    ready: bool
    missing: list[str] = Field(default_factory=list, max_length=8)
    reason: str = Field("", max_length=400)


class VerifierScore(BaseModel):
    """A grade for one answer against its evidence (S17 `reasoning/verifier.py`): a score to decide on, a critique to
    learn from. Recorded as evidence only."""

    score: int = Field(ge=0, le=100)
    critique: str = Field(max_length=600)
    issues: list[str] = Field(default_factory=list, max_length=8)
