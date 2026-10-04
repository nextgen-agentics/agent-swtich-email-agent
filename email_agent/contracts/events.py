"""Inbox watcher contracts (Revision 12, Stage 8). Adapted from S17 `events/models.py` and `events/governor.py`.

    MailboxEvent     one change the watcher saw: a new message from someone else, or a conversation that changed
    Subscription     what you configured: which events start which request, live or dry, and the daily ceilings
    Verdict          the governor's answer for one event or one run, always with its reason
    EventDecision    what happened to one event (refused, no subscription, or a run started), as stored
    OurWrite         one write we sent (agent or undo), so the watcher never answers its own change
    PollReport       one poll: what the sync and the cursor found, and what became of it
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

EventType = Literal["new_inbound", "thread_changed"]
Control = Literal["self_trigger", "source_rate_limit", "max_runs_per_day", "max_llm_calls_per_day",
                  "no_subscription", "dispatch_failed", ""]


def _now() -> datetime:
    return datetime.now(timezone.utc)


class MailboxEvent(BaseModel):
    """One fact the watcher observed (S17 EventEnvelope). Its data is untrusted: it never widens what a run may do."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    key: str                                    # the dedup key: msg:<id>, thread:<id>@<updated_at>, replay:…
    type: EventType
    source: str                                 # mailbox:<address>
    thread_id: str
    message_id: str | None = None
    subject: str | None = None
    sender: str | None = None
    occurred_at: str | None = None              # the platform's time (updated_at / created_at)
    observed_at: datetime = Field(default_factory=_now)
    actor: str | None = None                    # updated_by / created_by: a hint only (we share one login)


class Subscription(BaseModel):
    """Authority and intent configured by a person (watch/subscriptions.yaml), separate from event data."""

    model_config = ConfigDict(extra="forbid")

    id: str = Field(pattern=r"^[A-Za-z0-9_.-]+$")
    instances: list[str] = Field(min_length=1)
    event_types: list[EventType] = Field(min_length=1)
    request: str = Field(min_length=5, max_length=2000,
                         description="What the run is asked. The run is limited by code to the event's conversation.")
    live: bool = False                          # writes for real only if this AND the watcher's --live are set
    max_runs_per_day: int = Field(20, gt=0)
    max_llm_calls_per_day: int = Field(300, gt=0)
    enabled: bool = True


class SubscriptionFile(BaseModel):
    model_config = ConfigDict(extra="forbid")

    subscriptions: list[Subscription] = Field(default_factory=list)


class Verdict(BaseModel):
    """The outcome of one admission check, always explainable (S17)."""

    model_config = ConfigDict(frozen=True)

    admitted: bool
    control: Control = ""
    reason: str = ""
    detail: dict[str, Any] = Field(default_factory=dict)


class EventDecision(BaseModel):
    model_config = ConfigDict(extra="forbid")

    event_key: str
    subscription_id: str | None = None
    admitted: bool
    control: Control = ""
    reason: str = ""
    run_id: str | None = None
    run_dir: str | None = None
    stopped: str | None = None
    dry_run: bool | None = None
    llm_calls: int = 0
    at: datetime = Field(default_factory=_now)


class OurWrite(BaseModel):
    """A write we sent. A conversation change within the watcher's slack of one of these was caused by us."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    row_id: str
    tool: str
    at: datetime
    via: Literal["agent", "undo"]
    run_id: str | None = None


class PollReport(BaseModel):
    model_config = ConfigDict(extra="forbid")

    instance: str
    baseline: bool = False                      # first poll: the cursor was set, no events raised
    sync_calls: int = 0
    since: dict[str, str | None] = Field(default_factory=dict)   # cursor values used for this poll
    events: int = 0                             # new events (after dedup)
    duplicates: int = 0
    refused: dict[str, int] = Field(default_factory=dict)        # control → count
    unmatched: int = 0                          # admitted, but no subscription wants it
    runs: list[EventDecision] = Field(default_factory=list)
    seconds: float = 0.0

    def line(self) -> str:
        if self.baseline:
            return f"{self.instance}: first poll — cursor set, nothing raised ({self.sync_calls} sync call(s))"
        if not self.events:
            seen = f", {self.duplicates} re-read in the overlap, already seen" if self.duplicates else ""
            return f"{self.instance}: no change ({self.sync_calls} sync call(s){seen}, {self.seconds:.1f}s)"
        refused = ", ".join(f"{n} {c}" for c, n in self.refused.items()) or "none"
        return (f"{self.instance}: {self.events} new event(s), {self.duplicates} already seen · refused: {refused} · "
                f"no subscription: {self.unmatched} · runs: {len(self.runs)} ({self.seconds:.1f}s)")
