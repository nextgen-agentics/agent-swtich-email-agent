"""What an operator reads after a run: the trace (spans.jsonl, built from the journal) and the run page (view.html).
Both must be right for runs that crashed, were resumed or are waiting, which are exactly the runs someone looks at."""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from email_agent import agent
from email_agent.common.errors import leaves
from email_agent.contracts.view import RunView
from email_agent.graph.store import InjectedCrash
from email_agent.record import run_view
from tests.conftest import TODAY
from tests.kit.mail import them
from tests.kit.model import ScriptedModel, goal
from tests.kit.runs import only_run, spans

REPLY_CHECK = "What needs my reply today?"
TRIAGE = goal("Find the conversations that need our reply today", "triage-replies")
HOSTILE = "Please reply </script><script>alert('x')</script><!-- by Friday"


def _model(**kw):
    return ScriptedModel(goals=[TRIAGE], verdict=lambda t: {"needs_reply": True, "why": "they ask"}, **kw)


def _page(run_dir) -> tuple[str, RunView]:
    html = (Path(run_dir) / "view.html").read_text()
    block = html.split('<script type="application/json" id="page-data">', 1)[1].split("</script>", 1)[0]
    return html, RunView.model_validate_json(block)


def _assert_one_tree(trace):
    by_id = {s["span_id"]: s for s in trace}
    assert len({s["trace_id"] for s in trace}) == 1
    assert [s["kind"] for s in trace if s["parent_id"] is None] == ["run"]
    for s in trace:
        if s["parent_id"] is not None:
            parent = by_id[s["parent_id"]]
            assert parent["start"] <= s["start"] and s["end"] <= parent["end"], (parent["name"], s["name"])


async def test_a_runs_spans_form_one_tree_with_every_parent_enclosing_its_children(settings, platform, mail):
    mail.conversation("Samples", them("Could you send two samples?", "2026-10-04T09:00:00"))

    out = await agent.run(REPLY_CHECK, "suryodaya", settings, today=TODAY, llm=_model())

    trace = spans(out.run_dir)
    _assert_one_tree(trace)
    kinds = {s["kind"] for s in trace}
    assert {"run", "round", "node", "llm", "write"} <= kinds
    assert all(s["status"] == "ok" for s in trace if s["kind"] in ("node", "write"))


async def test_a_resumed_run_shows_the_unfinished_attempt_and_the_retry_as_separate_spans(settings, platform, mail,
                                                                                            monkeypatch):
    mail.conversation("Samples", them("Could you send two samples?", "2026-10-04T09:00:00"))
    monkeypatch.setenv("EMAIL_AGENT_CRASH_AT", "llm_call_started:3")           # inside the shard's model call
    with pytest.raises(BaseException) as stop:
        await agent.run(REPLY_CHECK, "suryodaya", settings, today=TODAY, llm=_model())
    assert any(isinstance(e, InjectedCrash) for e in leaves(stop.value))
    monkeypatch.delenv("EMAIL_AGENT_CRASH_AT")

    out = await agent.resume(only_run(settings.runs_dir), settings, llm=_model())

    trace = spans(out.run_dir)
    _assert_one_tree(trace)
    first, retry = [s for s in trace if s["name"] == "node judge_g1.s001"]
    assert first["status"] == "error" and first["status_message"].startswith("unfinished")
    assert retry["status"] == "ok"
    assert first["end"] <= retry["start"]


async def test_the_run_page_is_safe_against_script_tags_in_mail_and_answers(settings, platform, mail):
    mail.conversation("Samples", them(HOSTILE, "2026-10-04T09:00:00"))

    out = await agent.run(REPLY_CHECK, "suryodaya", settings, today=TODAY, llm=_model(answer=HOSTILE))

    html, view = _page(out.run_dir)
    assert view.answer == HOSTILE, "the data block reads back whole"
    template = (Path(run_view.__file__).parent / "view" / "page.html").read_text()
    assert html.count("</script>") == template.count("</script>"), "the mail text adds no closing tag of its own"
    assert view.header.stopped == "done" and view.header.next_step is None


async def test_the_run_page_of_a_waiting_run_gives_the_approve_command(settings, platform, mail):
    mail.conversation("Samples", them("Could you send two samples?", "2026-10-04T09:00:00"))

    out = await agent.run(REPLY_CHECK, "suryodaya", settings, today=TODAY, llm=_model(), approve_writes=True)

    _, view = _page(out.run_dir)
    assert view.header.stopped == "waiting"
    assert "--resume" in view.header.next_step and "--approve" in view.header.next_step


async def test_the_run_page_of_an_interrupted_run_gives_the_resume_command(settings, platform, mail):
    mail.conversation("Samples", them("Could you send two samples?", "2026-10-04T09:00:00"))
    model = _model()
    run = asyncio.create_task(agent.run(REPLY_CHECK, "suryodaya", settings, today=TODAY, llm=model))
    model.scripts["answer"] = lambda req: run.cancel() and "never delivered"
    with pytest.raises(asyncio.CancelledError):
        await run

    _, view = _page(only_run(settings.runs_dir))

    assert view.header.stopped == "interrupted"
    assert "--resume" in view.header.next_step and "--approve" not in view.header.next_step
