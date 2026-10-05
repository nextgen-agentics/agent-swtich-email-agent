"""The inbox watcher left running for a day: new mail starts a small dry run, our own writes never wake it, a flood
and the daily ceilings are refused (and recorded), and a restart does not process anything twice."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from functools import partial

from email_agent import agent
from email_agent.contracts.agent import RunRequest
from email_agent.contracts.events import MailboxEvent, Subscription
from email_agent.watch.governor import Governor
from email_agent.watch.store import EventStore
from email_agent.watch.watcher import Watcher
from tests.conftest import TODAY
from tests.kit.mail import them
from tests.kit.model import ScriptedModel, goal
from tests.kit.platform import now
from tests.kit.runs import writes

TRIAGE = goal("Find the conversations that need our reply today", "triage-replies")


def _reply_check(**ceilings) -> Subscription:
    return Subscription(id="reply-check", instances=["suryodaya"], event_types=["new_inbound"],
                        request="What needs my reply today?", **ceilings)


def _watcher(settings, model=None, sub=None) -> Watcher:
    model = model or ScriptedModel(goals=[TRIAGE], verdict=lambda t: {"needs_reply": True, "why": "they ask"})
    return Watcher(settings, "suryodaya", run=partial(agent.run, llm=model, today=TODAY),
                   subscriptions=[sub or _reply_check()])


async def test_a_new_inbound_mail_starts_one_dry_run_scoped_to_its_conversation(settings, platform, mail):
    older = mail.conversation("Invoice copy", them("Please resend the invoice.", "2026-10-03T09:00:00"))
    w = _watcher(settings)
    assert (await w.poll()).baseline, "starting the watcher never raises the mailbox's history"

    fresh = mail.conversation("Samples", them("Could you send two samples?", now()))
    report = await w.poll()
    w.close()

    [run] = report.runs
    assert run.dry_run is True and run.stopped == "done"
    request = RunRequest.model_validate_json((settings.runs_dir / run.run_id / "request.json").read_text())
    assert request.threads == [fresh]
    assert [x.row_id for x in writes(run.run_dir)] == [fresh], "the older conversation is outside this run"
    assert platform.writes() == []
    assert platform.row("EmailThread", older)["flag_status"] == "not_flagged"


async def test_our_own_write_does_not_wake_the_watcher_but_a_later_real_change_does(settings, platform, mail):
    asks = mail.conversation("Samples", them("Could you send two samples?", "2026-10-03T09:00:00"))
    w = _watcher(settings)
    await w.poll()
    model = ScriptedModel(goals=[TRIAGE], verdict=lambda t: {"needs_reply": True, "why": "they ask"})
    await agent.run("What needs my reply today?", "suryodaya", settings, today=TODAY, llm=model)   # flags `asks`

    ours = await w.poll()
    platform.row("EmailThread", asks).update(flag_status="completed", updated_at=now(minutes=10))  # team 11, later
    theirs = await w.poll()
    w.close()

    assert ours.refused == {"self_trigger": 1} and ours.runs == []
    assert theirs.refused == {} and theirs.unmatched == 1, "admitted (no subscription wants thread changes)"


async def test_the_history_read_on_the_first_poll_does_not_make_the_first_new_mail_look_like_a_flood(settings,
                                                                                                    platform, mail):
    busy = mail.conversation("Order status", them("Status?", now(minutes=-9)))
    for n in range(40):                          # the last ten minutes: the first poll reads them again as history
        mail.say(busy, them(f"Status update {n}?", now(minutes=-8, seconds=n)))
    w = _watcher(settings)
    await w.poll()
    fresh = mail.conversation("Samples", them("Could you send two samples?", now()))

    report = await w.poll()
    w.close()

    assert report.refused == {}
    assert [r.stopped for r in report.runs] == ["done"]
    assert fresh


def test_a_flood_from_one_mailbox_is_refused_after_thirty_a_minute_without_holding_back_another(tmp_path):
    store = EventStore(tmp_path / "events.sqlite")
    governor = Governor(store)
    at = datetime(2026, 10, 5, 9, 0, tzinfo=timezone.utc)

    def arrive(n, source):
        event = MailboxEvent(key=f"{source}:{n}", type="new_inbound", source=source, thread_id=f"t{n}", observed_at=at)
        store.ingest(event)
        return governor.admit_event(event, now=at + timedelta(seconds=30))

    flood = [arrive(n, "mailbox:sales@suryodaya.in") for n in range(31)]
    other = arrive(0, "mailbox:orders@suryodaya.in")
    store.close()

    assert [v.admitted for v in flood] == [True] * 30 + [False]
    assert flood[-1].control == "source_rate_limit"
    assert other.admitted


async def test_the_daily_run_ceiling_holds_when_events_arrive_together(settings, platform, mail):
    w = _watcher(settings, sub=_reply_check(max_runs_per_day=1))
    mail.conversation("Old", them("Old mail.", "2026-10-03T09:00:00"))
    await w.poll()
    for n in range(3):
        mail.conversation(f"Question {n}", them(f"Question {n}?", now(seconds=n)))

    report = await w.poll()
    w.close()

    assert len(report.runs) == 1
    assert report.refused == {"max_runs_per_day": 2}


async def test_the_daily_model_call_ceiling_caps_a_run_and_refuses_the_next_before_a_run_slot_is_taken(settings,
                                                                                                         platform, mail):
    w = _watcher(settings, sub=_reply_check(max_llm_calls_per_day=5))
    mail.conversation("Old", them("Old mail.", "2026-10-03T09:00:00"))
    await w.poll()
    mail.conversation("First", them("First question?", now()))
    first = await w.poll()                     # a full triage needs more than five model calls
    mail.conversation("Second", them("Second question?", now(seconds=1)))

    second = await w.poll()
    snapshot = w.governor.snapshot(w.subs[0])
    w.close()

    assert len(first.runs) == 1 and first.runs[0].llm_calls == 5, "the run gets what is left today, no more"
    assert second.runs == [] and second.refused == {"max_llm_calls_per_day": 1}
    assert snapshot["runs"] == "1/20", "the refused event did not use up a run slot"
    assert snapshot["llm_calls"] == "5/5"


async def test_after_a_restart_no_event_is_processed_twice(settings, platform, mail):
    mail.conversation("Old", them("Old mail.", "2026-10-03T09:00:00"))
    w = _watcher(settings)
    await w.poll()
    mail.conversation("Samples", them("Could you send two samples?", now()))
    assert len((await w.poll()).runs) == 1
    w.close()

    restarted = _watcher(settings)
    again = await restarted.poll()
    restarted.close()

    assert again.runs == [] and again.events == 0
    assert again.duplicates >= 1, "the overlap re-reads the message; its key was already seen"
