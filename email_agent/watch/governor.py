"""Bounds on unattended runs, enforced outside the agent (Revision 12, Stage 8). Adapted from S17 `events/governor.py`.

An agent that starts its own runs is not bounded by a per-run ceiling: it just starts more runs. So, as in S17:
  - refused outright: a change we caused. An agent that writes to the mailbox it watches sees its own write come back
    as an event, and answering it is a loop that only a ceiling would end, after the cost is spent;
  - per source (mailbox): at most SOURCE_EVENTS_PER_MINUTE events a minute, so a flood becomes a recorded refusal;
  - per subscription and day: a run ceiling (the slot is claimed at admission, so two events deciding at once cannot
    both take the last one) and an LLM-call ceiling (S17 counts money; our route has no prices, so we count calls).
Every refusal is recorded by the caller (watcher.py), with its control and reason.

Ours: S17 recognises its own events by their actor. Here the agent and the person share one login, and on Keystone even
the inbound seed mail was created under it (checked 2026-10-04), so the actor tells us nothing. A change is ours when a
write we sent (event_store `our_writes`: every agent run and every undo) touched the same row within SELF_SLACK.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from email_agent.contracts.events import MailboxEvent, Subscription, Verdict
from email_agent.watch.store import EventStore

SOURCE_EVENTS_PER_MINUTE = 30
SELF_SLACK = timedelta(minutes=5)      # our clock vs the platform's, and a write's time vs the row's updated_at

ADMITTED = Verdict(admitted=True)


def day_of(moment: datetime) -> str:
    return moment.astimezone(timezone.utc).date().isoformat()


def platform_time(value: str | None) -> datetime | None:
    """Platform times are naive UTC strings (sometimes with a zone)."""
    if not value:
        return None
    try:
        t = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return t if t.tzinfo else t.replace(tzinfo=timezone.utc)


class Governor:
    def __init__(self, store: EventStore, *, source_events_per_minute: int = SOURCE_EVENTS_PER_MINUTE):
        self.store = store
        self.source_events_per_minute = source_events_per_minute

    # ── intake ───────────────────────────────────────────────────────────────
    def admit_event(self, event: MailboxEvent, *, now: datetime | None = None) -> Verdict:
        """Before any subscription is matched. The event is already stored (deduplicated)."""
        moment = now or datetime.now(timezone.utc)
        if event.type == "thread_changed":
            at = platform_time(event.occurred_at)
            ours = self.store.our_write_near(event.thread_id, at, SELF_SLACK) if at else None
            if ours is not None:
                return Verdict(admitted=False, control="self_trigger",
                               reason=f"caused by us: {'our undo of ' if ours.via == 'undo' else ''}{ours.tool} "
                                      f"at {ours.at:%H:%M:%S}"
                                      + (f" (run {ours.run_id})" if ours.run_id else ""),
                               detail={"via": ours.via, "tool": ours.tool, "run_id": ours.run_id})
        recent = self.store.count_recent(event.source, moment - timedelta(minutes=1))
        if recent > self.source_events_per_minute:
            return Verdict(admitted=False, control="source_rate_limit",
                           reason=f"{event.source} raised {recent} events in a minute "
                                  f"(limit {self.source_events_per_minute})",
                           detail={"observed": recent, "limit": self.source_events_per_minute})
        return ADMITTED

    # ── doing ────────────────────────────────────────────────────────────────
    def admit_run(self, sub: Subscription, *, per_run: int | None = None, now: datetime | None = None) -> Verdict:
        """May this subscription start one more run today? Claims the run slot when it says yes, and reserves the
        run's model calls (at most `per_run`, never more than is left today): the run is given that many as its
        budget, so runs started together can never spend past the ceiling (found by the Revision 17 tests: a burst
        of three runs spent twice the ceiling, because each was admitted before any had spent anything)."""
        day = day_of(now or datetime.now(timezone.utc))
        calls = self.store.window(day, sub.id, "llm_calls")
        if calls >= sub.max_llm_calls_per_day:
            return Verdict(admitted=False, control="max_llm_calls_per_day",
                           reason=f"daily LLM-call ceiling reached: {calls} of {sub.max_llm_calls_per_day}",
                           detail={"used": calls, "limit": sub.max_llm_calls_per_day})
        claimed, runs = self.store.reserve(day, sub.id, "runs", sub.max_runs_per_day)
        if not claimed:
            return Verdict(admitted=False, control="max_runs_per_day",
                           reason=f"daily run ceiling reached: {runs} of {sub.max_runs_per_day}",
                           detail={"used": runs, "limit": sub.max_runs_per_day})
        granted = self.store.reserve_up_to(day, sub.id, "llm_calls", per_run or sub.max_llm_calls_per_day,
                                           sub.max_llm_calls_per_day)
        return Verdict(admitted=True, detail={"llm_calls": granted})

    def spent(self, sub: Subscription, llm_calls: int, *, reserved: int = 0, now: datetime | None = None) -> None:
        """What a finished run cost; what it reserved at admission and did not use is given back."""
        self.store.record(day_of(now or datetime.now(timezone.utc)), sub.id, "llm_calls", llm_calls - reserved)

    def snapshot(self, sub: Subscription, *, now: datetime | None = None) -> dict:
        day = day_of(now or datetime.now(timezone.utc))
        return {"subscription": sub.id, "day": day,
                "runs": f"{self.store.window(day, sub.id, 'runs')}/{sub.max_runs_per_day}",
                "llm_calls": f"{self.store.window(day, sub.id, 'llm_calls')}/{sub.max_llm_calls_per_day}",
                "live": sub.live}
