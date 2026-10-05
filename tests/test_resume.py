"""A run that dies halfway must be resumable, and a resumed run must never send a write twice. The crash is injected
at a journal event (EMAIL_AGENT_CRASH_AT, the hook the live crash drill uses) or by the fake platform itself."""

from __future__ import annotations

import asyncio

import pytest

from email_agent import agent
from email_agent.common.errors import leaves
from email_agent.graph.store import InjectedCrash
from tests.conftest import TODAY
from tests.kit.mail import them
from tests.kit.model import ScriptedModel, goal, thread_ids
from tests.kit.runs import budget_spent, final, journal, kinds, nodes, only_run, writes

REPLY_CHECK = "What needs my reply today?"


def _triage(**kw):
    return ScriptedModel(goals=[goal("Find the conversations that need our reply today", "triage-replies")],
                         verdict=lambda t: {"needs_reply": True, "why": "they ask us something"}, **kw)


async def _dies(run):
    """The injected crash may come wrapped in the exception group of the task group it hit."""
    with pytest.raises(BaseException) as stop:
        await run
    assert any(isinstance(e, InjectedCrash) for e in leaves(stop.value)), stop.value


def _flag_updates(platform, thread):
    return [a for _, a in platform.writes("EmailThread.update") if a["id"] == thread]


async def test_a_crash_after_the_platform_applied_a_write_never_sends_it_again(settings, platform, mail):
    asks = mail.conversation("Samples", them("Could you send two samples?", "2026-10-04T09:00:00"))
    platform.crash_after.add("EmailThread.update")

    await _dies(agent.run(REPLY_CHECK, "suryodaya", settings, today=TODAY, llm=_triage()))
    run_dir = only_run(settings.runs_dir)
    assert writes(run_dir) == [], "the crash came before the write was recorded"

    out = await agent.resume(run_dir, settings, llm=_triage())

    assert final(out.run_dir).stopped == "done"
    assert len(_flag_updates(platform, asks)) == 1
    assert [w.row_id for w in writes(run_dir)] == [asks], "reconcile records the write that did happen, once"
    settled = [e for e in journal(run_dir) if e.kind == "write_reconciled"]
    assert [e.payload["happened"] for e in settled] == [True]


async def test_a_crash_before_the_write_was_sent_sends_it_exactly_once_on_resume(settings, platform, mail, monkeypatch):
    asks = mail.conversation("Samples", them("Could you send two samples?", "2026-10-04T09:00:00"))
    monkeypatch.setenv("EMAIL_AGENT_CRASH_AT", "write_started:1")

    await _dies(agent.run(REPLY_CHECK, "suryodaya", settings, today=TODAY, llm=_triage()))
    monkeypatch.delenv("EMAIL_AGENT_CRASH_AT")
    run_dir = only_run(settings.runs_dir)
    assert _flag_updates(platform, asks) == []

    out = await agent.resume(run_dir, settings, llm=_triage())

    assert final(out.run_dir).stopped == "done"
    assert len(_flag_updates(platform, asks)) == 1
    assert platform.row("EmailThread", asks)["flag_status"] == "flagged"
    assert len(writes(run_dir)) == 1


async def test_a_crash_while_judging_reuses_saved_replies_and_keeps_the_budget(settings, platform, mail, monkeypatch):
    for n in range(45):                                  # 3 shards of at most 20
        mail.conversation(f"Order {n}", them(f"Please confirm order {n}.", f"2026-10-04T09:{n:02d}:00"))
    monkeypatch.setenv("EMAIL_AGENT_CRASH_AT", "llm_call_finished:3")      # goals, planner, then the first shard
    model = _triage()

    await _dies(agent.run(REPLY_CHECK, "suryodaya", settings, today=TODAY, llm=model))
    monkeypatch.delenv("EMAIL_AGENT_CRASH_AT")
    run_dir = only_run(settings.runs_dir)

    out = await agent.resume(run_dir, settings, llm=model)

    assert final(out.run_dir).stopped == "done"
    reused = [e.node_id for e in journal(run_dir) if e.kind == "llm_call_finished" and e.payload.get("reused")]
    shard = next(r for r in reused if r and ".s0" in r)
    saved = set(nodes(run_dir)[shard].input["thread_ids"])
    asked = [req for req in model.calls("judge") if set(thread_ids(req)) == saved]
    assert len(asked) == 1, "the shard judged before the crash is not paid for again"
    assert budget_spent(run_dir)["llm_calls"] == kinds(run_dir).count("llm_call_started")
    assert len(platform.writes("EmailThread.update")) == 45


async def test_a_write_the_platform_applied_without_answering_is_settled_by_reading_the_row(settings, platform, mail):
    asks = mail.conversation("Samples", them("Could you send two samples?", "2026-10-04T09:00:00"))
    platform.no_answer["EmailThread.update"] = True

    out = await agent.run(REPLY_CHECK, "suryodaya", settings, today=TODAY, llm=_triage())

    assert final(out.run_dir).stopped == "done"
    assert len(_flag_updates(platform, asks)) == 1, "an unanswered write is read back, never sent blindly"
    assert len(writes(out.run_dir)) == 1


async def test_a_write_that_keeps_timing_out_waits_and_finishes_on_resume(settings, platform, mail):
    asks = mail.conversation("Samples", them("Could you send two samples?", "2026-10-04T09:00:00"))
    platform.no_answer["EmailThread.update"] = False

    out = await agent.run(REPLY_CHECK, "suryodaya", settings, today=TODAY, llm=_triage())

    stopped = final(out.run_dir)
    assert stopped.stopped == "waiting"
    assert "--resume" in (stopped.reason or "")
    assert platform.row("EmailThread", asks)["flag_status"] == "not_flagged"

    del platform.no_answer["EmailThread.update"]                 # the platform answers again
    out = await agent.resume(out.run_dir, settings, llm=_triage())

    assert final(out.run_dir).stopped == "done"
    assert platform.row("EmailThread", asks)["flag_status"] == "flagged"
    assert len(writes(out.run_dir)) == 1


async def test_ctrl_c_mid_run_leaves_the_files_and_resume_finishes_the_run(settings, platform, mail):
    asks = mail.conversation("Samples", them("Could you send two samples?", "2026-10-04T09:00:00"))
    model = _triage()
    run = asyncio.create_task(agent.run(REPLY_CHECK, "suryodaya", settings, today=TODAY, llm=model))

    def ctrl_c(_req):
        run.cancel()                                   # what Ctrl-C does to asyncio.run's main task
        return "Samples needs a reply."
    model.scripts["answer"] = ctrl_c

    with pytest.raises(asyncio.CancelledError):
        await run
    run_dir = only_run(settings.runs_dir)
    stopped = final(run_dir)
    assert stopped.stopped == "interrupted"
    assert "--resume" in (stopped.reason or "")
    assert (run_dir / "outcome.json").exists() and (run_dir / "report.md").exists()

    out = await agent.resume(run_dir, settings, llm=_triage())

    assert final(out.run_dir).stopped == "done"
    assert len(_flag_updates(platform, asks)) == 1
