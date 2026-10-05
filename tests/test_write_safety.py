"""The book is shared with team 11, so what the agent may change is decided by code, not by the model: a dry run
sends nothing, other mailboxes are off limits, a row someone else changed is left alone, and approval means approval."""

from __future__ import annotations

from email_agent import agent
from tests.conftest import TODAY
from tests.kit.mail import TEAM11, them
from tests.kit.model import ScriptedModel, goal, task
from tests.kit.runs import final, journal, nodes, writes

REPLY_CHECK = "What needs my reply today?"
TRIAGE = goal("Find the conversations that need our reply today", "triage-replies")


def _needs_reply(_thread_id):
    return {"needs_reply": True, "why": "they ask us something"}


async def test_a_dry_run_sends_nothing_but_records_every_write_it_would_make(settings, platform, mail):
    asks = mail.conversation("Samples", them("Could you send two samples?", "2026-10-04T09:00:00"))
    model = ScriptedModel(goals=[TRIAGE], verdict=_needs_reply)

    out = await agent.run(REPLY_CHECK, "suryodaya", settings, today=TODAY, llm=model, dry_run=True)

    assert platform.writes() == []
    assert platform.row("EmailThread", asks)["flag_status"] == "not_flagged"
    [would] = writes(out.run_dir)
    assert (would.row_id, would.dry_run) == (asks, True)
    assert would.fields == {"flag_status": "flagged", "flag_due_date": TODAY.isoformat()}
    assert would.before == {"flag_status": "not_flagged", "flag_due_date": None}


async def test_a_write_the_model_proposes_on_another_teams_mailbox_is_refused_and_never_sent(settings, platform, mail):
    theirs = mail.conversation("Team 11 order", them("Ship it today please.", "2026-10-04T09:00:00"), mailbox=TEAM11)

    def plan(body):
        if not body["graph"]:
            return {"tasks": [task("flag_it", "EmailThread.update", "g1", id=theirs, flag_status="flagged")],
                    "reason": "flag the order"}
        return {"tasks": [task("answer_g1", "answer", "g1")], "reason": "report what happened"}
    model = ScriptedModel(goals=[TRIAGE], plan=plan)

    out = await agent.run(REPLY_CHECK, "suryodaya", settings, today=TODAY, llm=model)

    assert platform.writes() == []
    assert platform.row("EmailThread", theirs)["flag_status"] == "not_flagged"
    flag = nodes(out.run_dir)["flag_it"]
    assert flag.state == "failed" and "not in our mailboxes" in flag.error.message
    assert final(out.run_dir).stopped == "done"


async def test_a_row_team11_changed_while_our_write_was_lost_is_not_overwritten(settings, platform, mail):
    asks = mail.conversation("Samples", them("Could you send two samples?", "2026-10-04T09:00:00"))
    platform.no_answer["EmailThread.update"] = False
    platform.meanwhile["EmailThread.update"] = lambda: platform.row("EmailThread", asks).update(flag_status="completed")
    model = ScriptedModel(goals=[TRIAGE], verdict=_needs_reply)

    out = await agent.run(REPLY_CHECK, "suryodaya", settings, today=TODAY, llm=model)

    assert platform.row("EmailThread", asks)["flag_status"] == "completed", "team 11's change stands"
    assert len(platform.writes("EmailThread.update")) == 1, "the lost write is never sent again"
    [settled] = [e for e in journal(out.run_dir) if e.kind == "write_reconciled"]
    assert settled.payload["happened"] is None and "changed by someone else" in settled.payload["note"]
    assert writes(out.run_dir) == []


async def test_with_approval_on_nothing_is_written_until_you_approve(settings, platform, mail):
    asks = mail.conversation("Samples", them("Could you send two samples?", "2026-10-04T09:00:00"))
    model = ScriptedModel(goals=[TRIAGE], verdict=_needs_reply)

    first = await agent.run(REPLY_CHECK, "suryodaya", settings, today=TODAY, llm=model, approve_writes=True)

    assert final(first.run_dir).stopped == "waiting"
    assert "--approve" in (final(first.run_dir).reason or "")
    assert platform.writes() == []

    out = await agent.resume(first.run_dir, settings, llm=model, decision="approve")

    assert final(out.run_dir).stopped == "done"
    assert platform.row("EmailThread", asks)["flag_status"] == "flagged"
    assert len(platform.writes()) == 1


async def test_rejecting_the_approval_writes_nothing_and_still_answers(settings, platform, mail):
    mail.conversation("Samples", them("Could you send two samples?", "2026-10-04T09:00:00"))
    model = ScriptedModel(goals=[TRIAGE], verdict=_needs_reply)
    first = await agent.run(REPLY_CHECK, "suryodaya", settings, today=TODAY, llm=model, approve_writes=True)

    out = await agent.resume(first.run_dir, settings, llm=model, decision="reject")

    assert platform.writes() == [] and writes(out.run_dir) == []
    result = final(out.run_dir)
    assert result.stopped == "done", "a declined write must not leave the answer waiting forever"
    assert result.goals[0].done


async def test_a_watcher_run_cannot_write_outside_its_own_conversation(settings, platform, mail):
    event = mail.conversation("Samples", them("Could you send two samples?", "2026-10-04T09:00:00"))
    other = mail.conversation("Invoice copy", them("Please resend the invoice.", "2026-10-04T10:00:00"))

    def plan(body):
        if not body["graph"]:
            return {"tasks": [task("judge_g1", "judge_threads", "g1", skill="triage-replies"),
                              task("flag_other", "EmailThread.update", "g1", id=other, flag_status="flagged")],
                    "reason": "judge, and flag the invoice too"}
        if any(n["state"] in ("pending", "running", "waiting") for n in body["graph"]):
            return {"tasks": [], "reason": "wait"}
        return {"tasks": [task("answer_g1", "answer", "g1")], "reason": "done"}
    model = ScriptedModel(goals=[TRIAGE], plan=plan, verdict=_needs_reply)

    out = await agent.run(REPLY_CHECK, "suryodaya", settings, today=TODAY, llm=model, threads=[event])

    assert platform.row("EmailThread", event)["flag_status"] == "flagged"
    assert platform.row("EmailThread", other)["flag_status"] == "not_flagged"
    assert [a["id"] for _, a in platform.writes()] == [event]
    assert "may change only" in nodes(out.run_dir)["flag_other"].error.message
