"""Long-term memory contracts (Revision 12, Stage 7). Adapted from S17 `core/memory/models.py` (dataclasses there).

    MemoryScope    where a record applies: instance → mailbox → party → conversation (thread) → run
    SourceRef      where a record came from (S17: every durable record needs at least one)
    MemoryRecord   one row of the read copy (`memory.sqlite` `memories`): a platform AgentMemory, or an episode of ours
    Episode        what one run did, saved at its end and shown to the next run's planner
    RecallMemoryInput, RememberFactInput   the arguments of the two memory tools (local tools, see action.py)
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

MemoryKind = Literal["fact", "preference", "instruction", "relationship", "context", "episode"]
MemoryStatus = Literal["current", "switched_off", "gone"]       # gone = no longer on the platform
RememberCategory = Literal["preference", "instruction", "fact", "relationship"]


def utcnow() -> str:
    return datetime.now(timezone.utc).isoformat()


class MemoryScope(BaseModel):
    """The ownership path; a missing lower level means broader. A broader record is readable in a narrower request,
    never the other way (S17 `_scope_where`)."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    instance: str
    mailbox_id: str | None = None
    party_id: str | None = None
    thread_id: str | None = None
    run_id: str | None = None


class SourceRef(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    uri: str                                    # agentswitch:<instance>/AgentMemory/<id>, run:<run_id>/request, …
    author: str                                 # who wrote it: a user id or email, or "agent"
    captured_at: str = Field(default_factory=utcnow)
    excerpt: str | None = None


class MemoryRecord(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str                                     # the platform's AgentMemory id, or episode:<run_id>
    kind: MemoryKind
    scope: MemoryScope
    text: str
    sources: list[SourceRef] = Field(min_length=1)
    origin: Literal["platform", "local"]
    status: MemoryStatus = "current"
    valid_to: str | None = None                 # the platform's expires_at
    importance: float | None = None
    created_at: str = Field(default_factory=utcnow)
    updated_at: str | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)

    def shown(self) -> dict[str, Any]:
        """How a recalled memory is shown to the model."""
        out: dict[str, Any] = {"id": self.id, "category": self.kind, "content": self.text[:600],
                               "applies_to": "party" if self.scope.party_id else "company",
                               "created_at": (self.created_at or "")[:10]}
        if self.metadata.get("source"):
            out["source"] = self.metadata["source"]
        return out


class EpisodeGoal(BaseModel):
    model_config = ConfigDict(extra="forbid")

    text: str
    skill: str | None = None
    outcome: Literal["answered", "refused", "open"]
    reason: str | None = None                   # the refusal reason


class Episode(BaseModel):
    """One run, in a few words. Saved at every exit (and replaced when the run is resumed)."""

    model_config = ConfigDict(extra="forbid")

    run_id: str
    day: str                                    # the run's "today"
    request: str
    stopped: str
    dry_run: bool
    mailboxes: list[str]
    goals: list[EpisodeGoal]
    writes: dict[str, int] = Field(default_factory=dict)      # tool → count

    def line(self) -> str:
        goals = "; ".join(f"{g.skill or 'no skill'}: {g.outcome}" + (f" ({g.reason})" if g.reason else "")
                          for g in self.goals) or "no goals"
        writes = ", ".join(f"{n} {t}" for t, n in sorted(self.writes.items())) or "no writes"
        return (f"{self.day} · \"{self.request[:120]}\" · {self.stopped} · {goals} · {writes}"
                + (" (dry run: nothing was sent)" if self.dry_run else ""))


# ── the memory tools' arguments ──────────────────────────────────────────────

class RecallMemoryInput(BaseModel):
    """`recall_memory`: what we remember (active, unexpired) about one party, or matching some words."""

    model_config = ConfigDict(extra="forbid")

    party_id: str | None = Field(None, min_length=1, description="The party's id (from Party.list / EmailContact.list).")
    query: str | None = Field(None, min_length=2, max_length=200,
                              description="Words to look for in what is remembered (used with or without party_id).")

    @model_validator(mode="after")
    def _one(self) -> "RecallMemoryInput":
        if not self.party_id and not self.query:
            raise ValueError("give party_id, query, or both")
        return self


class RememberFactInput(BaseModel):
    """`remember_fact`: save one thing to remember about a party (AgentMemory). The only way the agent adds memory."""

    model_config = ConfigDict(extra="forbid")

    party_id: str = Field(min_length=1, description="The party's id, exactly as Party.list returned it.")
    category: RememberCategory = Field(description="preference: how they like things done; instruction: what we must "
                                                   "always or never do for them; fact: something true about them that "
                                                   "asks nothing of us; relationship: who is who. Anything they want, "
                                                   "require or expect from us is a preference or an instruction.")
    content: str = Field(min_length=5, max_length=1000, description="The thing to remember, in the person's words, "
                                                                     "one or two sentences.")
    source_message_id: str | None = Field(None, min_length=1, description="Only when it comes from an email: that "
                                                                          "message's id (else it comes from the request).")
