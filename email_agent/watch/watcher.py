"""The inbox watcher (Revision 12, Stage 8): turn mailbox changes into scoped, governed, dry-by-default runs.
Adapted from S17 `events/engine.py` (match, admit, dispatch) and `events/governor.py`.

    uv run python -m email_agent.watch --instance suryodaya --once         # one poll
    uv run python -m email_agent.watch --instance suryodaya                # poll every WATCH_INTERVAL_S until Ctrl-C
    uv run python -m email_agent.watch --instance suryodaya --status
    uv run python -m email_agent.watch --instance suryodaya --replay <message_id>
    uv run python -m email_agent.watch --instance suryodaya --once --since 2026-10-04T08:00:00

One poll:
  1. sync the local mailbox copy (incremental: one call per table when nothing changed) — the platform has no push;
  2. read what changed since the watcher's own cursors (not the sync's: agent runs sync the same copy):
       new_inbound     a message from someone else, created after the cursor (a re-read old message is not new)
       thread_changed  a conversation whose updated_at moved
     The first poll only sets the cursors, so starting the watcher never raises the whole mailbox's history;
  3. each event is stored once by its key (seen again → skipped), then the governor admits or refuses it (a change we
     caused is refused: governor.py), then it is matched to your subscriptions (watch/subscriptions.yaml);
  4. each match claims a run slot (daily ceilings) and starts `agent.run` with the subscription's request, limited by
     code to the event's conversation and mailbox, dry unless the subscription says live AND --live was given; at most
     WATCH_MAX_RUNS at once. Every decision — refusal, no subscription, run and how it ended — is stored.
Not taken from S17: the LLM relevance gate (the run's own goals step and judging decide).
"""

from __future__ import annotations

import argparse
import asyncio
import json
import secrets
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Awaitable, Callable

import yaml

from email_agent.agent import run as agent_run
from email_agent.config import Settings, get_settings
from email_agent.contracts.agent import RunContext, RunOutcome
from email_agent.contracts.events import (
    EventDecision,
    MailboxEvent,
    PollReport,
    Subscription,
    SubscriptionFile,
)
from email_agent.mailbox.store import MailboxStore
from email_agent.mailbox.sync import OVERLAP, MailboxSync
from email_agent.platform.context import build_context
from email_agent.platform.mcp_session import McpSession
from email_agent.watch.governor import Governor
from email_agent.watch.store import EventStore

RunFn = Callable[..., Awaitable[RunOutcome]]


def load_subscriptions(path: Path, instance: str) -> list[Subscription]:
    if not path.is_file():
        return []
    data = SubscriptionFile.model_validate(yaml.safe_load(path.read_text()) or {})
    return [s for s in data.subscriptions if s.enabled and instance in s.instances]


def _minus(value: str, delta: timedelta) -> str:
    """A platform time string minus `delta`, in the platform's own format (naive UTC, microseconds)."""
    t = datetime.fromisoformat(value.replace("Z", "+00:00"))
    t = t.astimezone(timezone.utc).replace(tzinfo=None) if t.tzinfo else t
    return (t - delta).isoformat(timespec="microseconds")


class Watcher:
    def __init__(self, settings: Settings, instance: str, *, live: bool = False, run: RunFn = agent_run,
                 subscriptions: list[Subscription] | None = None):
        self.settings, self.instance, self.live, self.run = settings, instance, live, run
        self.subs = subscriptions if subscriptions is not None else \
            load_subscriptions(settings.subscriptions_file, instance)
        self.store = EventStore.for_instance(settings.state_dir, instance)
        self.governor = Governor(self.store)
        self.ctx: RunContext | None = None
        self._slots = asyncio.Semaphore(max(1, settings.watch_max_runs))

    def close(self) -> None:
        self.store.close()

    # ── one poll ─────────────────────────────────────────────────────────────
    async def poll(self, since: str | None = None) -> PollReport:
        started = time.perf_counter()
        self.store.beat()
        report = PollReport(instance=self.instance)
        mirror = MailboxStore.for_instance(self.settings.state_dir, self.instance)
        try:
            async with McpSession(self.settings, self.instance) as mcp:
                if self.ctx is None:              # who and where, once per watcher (mailboxes rarely change)
                    self.ctx = await build_context(self.settings, mcp, self.instance, "(watcher)", "watcher")
                sync = await MailboxSync(mcp, mirror, self.instance, self.ctx.mailboxes,
                                         set(self.ctx.our_addresses)).run()
            report.sync_calls = sync.calls
            events = self._changes(mirror, report, since)
        finally:
            mirror.close()
        if not report.baseline:
            await self._process(events, report)
        report.seconds = round(time.perf_counter() - started, 2)
        return report

    def _changes(self, mirror: MailboxStore, report: PollReport, since: str | None) -> list[MailboxEvent]:
        """The events since the watcher's cursors (or `since`), and the cursors moved on."""
        assert self.ctx is not None
        ids = [m.id for m in self.ctx.mailboxes]
        address = {m.id: m.email for m in self.ctx.mailboxes}
        ours = set(self.ctx.our_addresses)
        msg_mark, thr_mark = since or self.store.cursor("messages"), since or self.store.cursor("threads")
        newest_msg, newest_thr = mirror.newest_change(ids)
        if msg_mark is None or thr_mark is None:
            # First poll: the cursors start at the newest change, and what the next poll's overlap will read again is
            # marked as seen now (found live: without it the second poll raised this morning's 56 undo changes).
            report.baseline = True
            msg_mark, thr_mark = newest_msg, newest_thr
            if msg_mark is None or thr_mark is None:
                return []
        msg_from, thr_from = _minus(msg_mark, OVERLAP), _minus(thr_mark, OVERLAP)
        report.since = {"messages": msg_from, "threads": thr_from}
        events: list[MailboxEvent] = []
        for m in mirror.messages_since(ids, msg_from):
            created = m.created_at or m.updated_at or ""
            if (m.from_email or "").lower() in ours or not m.thread_id or created <= msg_from:
                continue                                   # ours, or an old message that was only re-read/re-filed
            events.append(MailboxEvent(key=f"msg:{m.id}", type="new_inbound", source=f"mailbox:{address.get(m.mailbox_id or '')}",
                                       thread_id=m.thread_id, message_id=m.id, subject=m.subject,
                                       sender=m.from_email, occurred_at=created, actor=m.created_by))
        for t in mirror.threads_since(ids, thr_from):
            events.append(MailboxEvent(key=f"thread:{t.id}@{t.updated_at}", type="thread_changed",
                                       source=f"mailbox:{address.get(t.mailbox_id or '')}", thread_id=t.id,
                                       subject=t.subject, occurred_at=t.updated_at, actor=t.updated_by))
        if report.baseline:
            for e in events:
                self.store.ingest(e)
            events = []
        if since is None:                                  # --since looks back without moving the cursors
            self.store.set_cursor("messages", max(filter(None, [msg_mark, newest_msg])))
            self.store.set_cursor("threads", max(filter(None, [thr_mark, newest_thr])))
        return events

    # ── governing and dispatch ───────────────────────────────────────────────
    async def _process(self, events: list[MailboxEvent], report: PollReport) -> None:
        jobs: list[tuple[MailboxEvent, Subscription]] = []
        for event in events:
            if not self.store.ingest(event):
                report.duplicates += 1
                continue
            report.events += 1
            verdict = self.governor.admit_event(event)
            if not verdict.admitted:
                self._refused(report, EventDecision(event_key=event.key, admitted=False, control=verdict.control,
                                                    reason=verdict.reason))
                continue
            matching = [s for s in self.subs if event.type in s.event_types]
            if not matching:
                report.unmatched += 1
                self.store.decide(EventDecision(event_key=event.key, admitted=True, control="no_subscription",
                                                reason=f"no subscription wants {event.type}"))
                continue
            for sub in matching:
                admit = self.governor.admit_run(sub)
                if not admit.admitted:
                    self._refused(report, EventDecision(event_key=event.key, subscription_id=sub.id, admitted=False,
                                                        control=admit.control, reason=admit.reason))
                    continue
                jobs.append((event, sub))
        if jobs:
            async with asyncio.TaskGroup() as tg:
                tasks = [tg.create_task(self._dispatch(e, s)) for e, s in jobs]
            report.runs = [t.result() for t in tasks]

    def _refused(self, report: PollReport, d: EventDecision) -> None:
        self.store.decide(d)
        report.refused[d.control] = report.refused.get(d.control, 0) + 1

    async def _dispatch(self, event: MailboxEvent, sub: Subscription) -> EventDecision:
        dry = not (sub.live and self.live)
        mailbox = event.source.removeprefix("mailbox:") or None
        async with self._slots:
            try:
                outcome = await self.run(sub.request, self.instance, self.settings, dry_run=dry,
                                         threads=[event.thread_id], mailboxes=[mailbox] if mailbox else None)
            except Exception as e:  # noqa: BLE001 — a failed dispatch is a recorded fact, not a quiet night
                d = EventDecision(event_key=event.key, subscription_id=sub.id, admitted=True, control="dispatch_failed",
                                  reason=f"{type(e).__name__}: {e}"[:500], dry_run=dry)
                self.store.decide(d)
                return d
        calls = sum(outcome.served_by.values())
        self.governor.spent(sub, calls)
        d = EventDecision(event_key=event.key, subscription_id=sub.id, admitted=True, run_id=outcome.run_id,
                          run_dir=outcome.run_dir, stopped=outcome.final.stopped, dry_run=dry, llm_calls=calls,
                          reason=f"{len(outcome.writes)} write(s)")
        self.store.decide(d)
        return d

    # ── replay and status ────────────────────────────────────────────────────
    async def replay(self, message_id: str) -> PollReport:
        """A recorded inbound message raised again as a new event (its own key), through the same governor."""
        mirror = MailboxStore.for_instance(self.settings.state_dir, self.instance)
        try:
            m = mirror.message(message_id)
        finally:
            mirror.close()
        if m is None or not m.thread_id:
            raise SystemExit(f"message {message_id} is not in the local copy of {self.instance}")
        async with McpSession(self.settings, self.instance) as mcp:
            if self.ctx is None:
                self.ctx = await build_context(self.settings, mcp, self.instance, "(watcher)", "watcher")
        address = {mb.id: mb.email for mb in self.ctx.mailboxes}
        report = PollReport(instance=self.instance)
        event = MailboxEvent(key=f"replay:msg:{m.id}:{secrets.token_hex(3)}", type="new_inbound",
                             source=f"mailbox:{address.get(m.mailbox_id or '')}", thread_id=m.thread_id,
                             message_id=m.id, subject=m.subject, sender=m.from_email,
                             occurred_at=m.created_at, actor=m.created_by)
        started = time.perf_counter()
        await self._process([event], report)
        report.seconds = round(time.perf_counter() - started, 2)
        return report

    def status(self) -> dict[str, Any]:
        day_start = datetime.now(timezone.utc).replace(hour=0, minute=0, second=0, microsecond=0)
        return {"instance": self.instance, "polls": self.store.cursor("polls"),
                "last_poll_at": self.store.cursor("last_poll_at"),
                "cursors": {"messages": self.store.cursor("messages"), "threads": self.store.cursor("threads")},
                "our_writes_recorded": self.store.our_writes_count(),
                "refused_today": self.store.refusal_counts(day_start),
                "subscriptions": [self.governor.snapshot(s) for s in self.subs],
                "last_decisions": [d.model_dump(mode="json", exclude_none=True) for d in self.store.decisions(10)]}


async def _watch(w: Watcher, args: argparse.Namespace) -> None:
    if args.replay:
        print(_report_text(await w.replay(args.replay)))
        return
    while True:
        report = await w.poll(since=args.since)
        print(_report_text(report), flush=True)
        if args.once or args.since:
            return
        await asyncio.sleep(w.settings.watch_interval_s)


def _report_text(r: PollReport) -> str:
    lines = [r.line()]
    for d in r.runs:
        lines.append(f"  run {d.run_id or '-'}: {d.stopped or d.control} · {'dry run' if d.dry_run else 'LIVE'} · "
                     f"{d.llm_calls} LLM call(s) · {d.reason} → {d.run_dir}/report.md")
    return "\n".join(lines)


def main() -> None:
    ap = argparse.ArgumentParser(description="Inbox watcher: mailbox changes → governed, scoped, dry-by-default runs")
    ap.add_argument("--instance", default="suryodaya")
    ap.add_argument("--once", action="store_true", help="one poll, then stop")
    ap.add_argument("--since", help="look back from this platform time (e.g. 2026-10-04T08:00:00) without moving "
                                    "the cursors; one poll")
    ap.add_argument("--replay", metavar="MESSAGE_ID", help="raise this recorded inbound message as a new event")
    ap.add_argument("--status", action="store_true", help="cursors, ceilings used today, refusals, last decisions")
    ap.add_argument("--live", action="store_true", help="allow subscriptions marked live: true to write for real")
    args = ap.parse_args()
    w = Watcher(get_settings(), args.instance, live=args.live)
    try:
        if args.status:
            print(json.dumps(w.status(), indent=1, default=str))
            return
        asyncio.run(_watch(w, args))
    except KeyboardInterrupt:
        print("\n— watcher stopped (Ctrl-C)")
    finally:
        w.close()


if __name__ == "__main__":
    main()
