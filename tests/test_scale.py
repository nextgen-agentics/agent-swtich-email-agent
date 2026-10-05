"""Load tests: a mailbox of 50,000 messages in 10,000 conversations, 300 of them waiting on us. Off by default (they
take about a minute); run them with `uv run pytest -m scale -s` to see the numbers. The platform is the fake one and
the model is scripted, so this measures our own code; on the live platform each call adds about a second."""

from __future__ import annotations

import asyncio
import resource
import sys
import time
from contextlib import asynccontextmanager

import pytest

from email_agent import agent
from email_agent.contracts.events import Subscription
from email_agent.mailbox.store import MailboxStore
from email_agent.watch.watcher import Watcher
from tests.conftest import TODAY
from tests.kit.mail import SALES
from tests.kit.model import ScriptedModel, goal
from tests.kit.platform import now
from tests.kit.runs import budget_spent, final, nodes, syncs

pytestmark = pytest.mark.scale

CONVERSATIONS, PER_CONVERSATION, WAITING = 10_000, 5, 300
REPLY_CHECK = "What needs my reply today?"


def _fill(platform):
    """Each conversation: they ask, we answer, back and forth; the last WAITING end with an unanswered question."""
    for n in range(CONVERSATIONS):
        tid = f"t{n:05d}"
        day = f"2026-{1 + n % 9:02d}-{1 + n % 28:02d}"
        platform.add("EmailThread", id=tid, subject=f"Order {n}", mailbox_id=SALES["id"], folder="inbox",
                     flag_status="not_flagged", is_starred=False, importance="normal", split_category="other",
                     updated_at=f"{day}T18:00:00")
        last_them = n >= CONVERSATIONS - WAITING
        for k in range(PER_CONVERSATION):
            ours = k % 2 == 1 if not last_them else k % 2 == 1 and k < PER_CONVERSATION - 1
            if not last_them and k == PER_CONVERSATION - 1:
                ours = True
            at = f"{day}T{9 + k:02d}:00:00"
            platform.add("EmailMessage", id=f"m{n:05d}-{k}", thread_id=tid, mailbox_id=SALES["id"],
                         from_email=SALES["email"] if ours else f"buyer{n}@customer.in",
                         body_text=f"Message {k} about order {n}." + ("" if ours else " Can you confirm?"),
                         folder="sent" if ours else "inbox", received_at=at, sent_at=at, created_at=at, updated_at=at)
    platform.add("Mailbox", **{k: SALES[k] for k in ("id", "email", "is_default", "is_active")})


@asynccontextmanager
async def _measured(label: str):
    """Seconds, the longest the event loop was blocked, and the process's peak memory."""
    worst = 0.0
    stop = asyncio.Event()

    async def probe():
        nonlocal worst
        while not stop.is_set():
            t = time.perf_counter()
            await asyncio.sleep(0.01)
            worst = max(worst, time.perf_counter() - t - 0.01)
    task = asyncio.create_task(probe())
    started = time.perf_counter()
    numbers: dict[str, float] = {}
    try:
        yield numbers
    finally:
        stop.set()
        await task
        numbers.update(seconds=time.perf_counter() - started, loop_blocked=worst,
                       peak_mb=resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / (1 << 20 if sys.platform == "darwin"
                                                                                     else 1 << 10))
        print(f"\n[scale] {label}: {numbers['seconds']:.1f} s, loop blocked at most {numbers['loop_blocked']:.2f} s, "
              f"peak {numbers['peak_mb']:.0f} MB")


def _triage(**kw):
    return ScriptedModel(goals=[goal("Find the conversations that need our reply today", "triage-replies")],
                         verdict=lambda t: {"needs_reply": True, "why": "they ask us to confirm"}, **kw)


async def test_a_large_mailbox_is_copied_once_and_then_kept_up_to_date_with_one_page_per_table(settings, platform):
    _fill(platform)

    async with _measured("cold sync + triage of 10,000 conversations"):
        first = await agent.run(REPLY_CHECK, "suryodaya", settings, today=TODAY, llm=_triage(), dry_run=True)
    async with _measured("second run, nothing changed"):
        second = await agent.run(REPLY_CHECK, "suryodaya", settings, today=TODAY, llm=_triage(), dry_run=True)

    cold, again = syncs(first.run_dir)[0], syncs(second.run_dir)[0]
    assert {t.entity: t.calls for t in cold.tables} == {"EmailThread": 10, "EmailMessage": 50}
    assert cold.facts_recomputed == CONVERSATIONS
    assert {t.entity: t.calls for t in again.tables} == {"EmailThread": 1, "EmailMessage": 1}
    assert all(t.changed == 0 for t in again.tables) and again.facts_recomputed == 0
    copy = MailboxStore.for_instance(settings.state_dir, "suryodaya")
    try:
        assert copy.counts() == (CONVERSATIONS, CONVERSATIONS * PER_CONVERSATION)
    finally:
        copy.close()


async def test_triage_of_a_large_mailbox_flags_exactly_the_conversations_waiting_on_us(settings, platform):
    _fill(platform)
    model = _triage()

    async with _measured("triage, live writes of 300 flags") as numbers:
        out = await agent.run(REPLY_CHECK, "suryodaya", settings, today=TODAY, llm=model)

    assert final(out.run_dir).stopped == "done"
    flagged = [t for t, row in platform.rows["EmailThread"].items() if row["flag_status"] == "flagged"]
    assert len(flagged) == WAITING and all(int(t[1:]) >= CONVERSATIONS - WAITING for t in flagged)
    assert len(model.calls("judge")) == WAITING // 20
    spent = budget_spent(out.run_dir)
    assert spent["nodes"] <= settings.max_nodes and spent["llm_calls"] <= settings.max_llm_calls
    assert numbers["loop_blocked"] < 1.0, "the event loop must stay responsive (node time limits, other runs)"


async def test_sorting_the_whole_large_mailbox_judges_what_the_budget_allows_and_says_what_it_left(settings, platform):
    _fill(platform)
    model = ScriptedModel(goals=[goal("Sort my inbox", "sort-inbox")],
                          verdict=lambda t: {"importance": "high", "split_category": "important", "why": "a customer"})

    async with _measured("sort-inbox over 10,000 conversations"):
        out = await agent.run("Sort my inbox", "suryodaya", settings, today=TODAY, llm=model, dry_run=True)

    spent = budget_spent(out.run_dir)
    assert spent["nodes"] <= settings.max_nodes and spent["llm_calls"] <= settings.max_llm_calls
    judge = nodes(out.run_dir)["judge_g1"].result
    assert final(out.run_dir).stopped == "done", final(out.run_dir).reason
    shards = [n for n in nodes(out.run_dir).values() if n.capability == "judge_shard"]
    assert judge["not_judged"] == CONVERSATIONS - 20 * len(shards) > 0


async def test_the_watcher_on_a_large_mailbox_raises_one_event_for_one_new_mail(settings, platform):
    _fill(platform)
    sub = Subscription(id="reply-check", instances=["suryodaya"], event_types=["new_inbound"], request=REPLY_CHECK)
    runs = []

    async def run(*args, **kwargs):
        runs.append(kwargs)
        return await agent.run(*args, llm=_triage(), today=TODAY, **kwargs)
    w = Watcher(settings, "suryodaya", run=run, subscriptions=[sub])
    async with _measured("watcher first poll (copies the mailbox)"):
        await w.poll()
    platform.add("EmailMessage", thread_id="t00000", mailbox_id=SALES["id"], from_email="buyer0@customer.in",
                 body_text="One more question.", folder="inbox", received_at=now(), created_at=now(), updated_at=now())

    async with _measured("watcher poll with one new mail"):
        report = await w.poll()
    w.close()

    assert report.events == 1 and len(report.runs) == 1
    assert runs[0]["threads"] == ["t00000"]
