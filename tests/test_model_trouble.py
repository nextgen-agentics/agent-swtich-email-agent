"""What happens when the model misbehaves in production: rate limits, dead keys, plans that make no sense, verdicts
about the wrong conversations, two models that disagree, a budget that runs out, a critic that never says yes."""

from __future__ import annotations

from pathlib import Path

import httpx
import openai
import pytest
from google.genai import errors

from email_agent import agent
from email_agent.contracts.agent import RunOutcome
from email_agent.contracts.llm import ChatMessage, LlmRequest
from email_agent.llm.route import LlmError, Option, RoutedLlm
from tests.conftest import TODAY
from tests.kit.mail import them, us
from tests.kit.model import ScriptedModel, goal, judge_then_answer, task, thread_ids
from tests.kit.runs import budget_spent, final, journal, writes

REPLY_CHECK = "What needs my reply today?"
TRIAGE = goal("Find the conversations that need our reply today", "triage-replies")


def _needs_reply(_thread_id):
    return {"needs_reply": True, "why": "they ask us something"}


class Refusing:
    """A provider that answers every call with the same error."""

    def __init__(self, error: Exception):
        self.error, self.calls = error, 0

    async def chat(self, _req):
        self.calls += 1
        raise self.error


def _gemini_error(code: int, status: str, **extra) -> errors.APIError:
    return errors.APIError(code, {"error": {"code": code, "message": status, "status": status, **extra}})


RATE_LIMITED = _gemini_error(429, "RESOURCE_EXHAUSTED",
                             details=[{"@type": "type.googleapis.com/google.rpc.RetryInfo", "retryDelay": "30s"}])
BAD_KEY = _gemini_error(401, "UNAUTHENTICATED")


async def test_a_rate_limited_key_rests_and_the_next_key_answers_the_whole_run(settings, platform, mail):
    asks = mail.conversation("Samples", them("Could you send two samples?", "2026-10-04T09:00:00"))
    key1 = Refusing(RATE_LIMITED)
    key2 = ScriptedModel(goals=[TRIAGE], verdict=_needs_reply)
    route = RoutedLlm([Option("gemini", "fake-flash", 1, key1), Option("gemini", "fake-flash", 2, key2)])

    out = await agent.run(REPLY_CHECK, "suryodaya", settings, today=TODAY, llm=route)

    assert final(out.run_dir).stopped == "done"
    assert platform.row("EmailThread", asks)["flag_status"] == "flagged"
    assert key1.calls == 1, "a rate-limited key rests; it is not hammered on every call"
    saved = RunOutcome.model_validate_json((Path(out.run_dir) / "outcome.json").read_text())
    assert saved.served_by == {"gemini/fake-flash key #2": len(key2.requests)}


async def test_when_every_model_is_dead_the_run_stops_saying_why_and_keeps_its_files(settings, platform, mail):
    mail.conversation("Samples", them("Could you send two samples?", "2026-10-04T09:00:00"))
    route = RoutedLlm([Option("gemini", "fake-flash", 1, Refusing(BAD_KEY)),
                       Option("gemini", "fake-flash", 2, Refusing(BAD_KEY))])

    out = await agent.run(REPLY_CHECK, "suryodaya", settings, today=TODAY, llm=route)

    result = final(out.run_dir)
    assert result.stopped == "error"
    assert "no LLM option could answer" in (result.reason or "") and "key refused" in (result.reason or "")
    assert platform.writes() == []
    for name in ("outcome.json", "report.md", "view.html"):
        assert (Path(out.run_dir) / name).exists(), name


def _openai_429(body: dict) -> openai.RateLimitError:
    return openai.RateLimitError(f"Error code: 429 - {body}", body=body,
                                 response=httpx.Response(429, request=httpx.Request("POST", "http://model/v1")))


async def test_a_spent_token_budget_stops_every_model_on_that_key_at_once_but_a_rate_limit_only_rests():
    # The AgentSwitch runner's model answers 429 "budget_exhausted" when the run's token budget is spent (not 401).
    spent = Refusing(_openai_429({"error": {"code": "budget_exhausted"}}))
    other_model = ScriptedModel(goals=[TRIAGE])
    route = RoutedLlm([Option("openai", "model-a", None, spent), Option("openai", "model-b", None, other_model)])
    request = LlmRequest(purpose="goals", system="s", messages=[ChatMessage(role="user", text="hi")])

    with pytest.raises(LlmError, match="token budget spent"):
        await route.chat(request)

    assert spent.calls == 1 and other_model.requests == [], "nothing is retried or waited out"
    assert route.budget_spent()

    limited = Refusing(_openai_429({"error": {"code": "rate_limited"}}))
    route = RoutedLlm([Option("openai", "model-a", None, limited), Option("openai", "model-b", None, other_model)])
    reply = await route.chat(request)
    assert reply.fallback_from == ["openai/model-a: rate-limited (retry in 30s)"]
    assert not route.budget_spent() and route.options[0].dead_reason is None, "a plain 429 rests, it does not die"


class Counting(ScriptedModel):
    """A scripted model that bills a fixed number of tokens per call, like a real client's `tokens_used`."""

    def __init__(self, per_call: int, **kw):
        super().__init__(**kw)
        self.per_call, self.tokens_used = per_call, 0

    async def chat(self, req):
        self.tokens_used += self.per_call
        return await super().chat(req)


async def test_once_its_token_share_is_used_the_route_stops_like_a_spent_budget():
    # Revision 25: one token budget covers both books of an official run, so each book stops at its own share.
    model = Counting(per_call=100, goals=[TRIAGE])
    route = RoutedLlm([Option("openai", "model-a", None, model)], token_limit=150)
    request = LlmRequest(purpose="goals", system="s", messages=[ChatMessage(role="user", text="hi")])

    await route.chat(request)
    await route.chat(request)                     # 100 of 150 used: the call that crosses the share still runs
    with pytest.raises(LlmError, match="share of the token budget is used: 200 of 150"):
        await route.chat(request)

    assert len(model.requests) == 2 and route.tokens_used == 200
    assert route.budget_spent() and route.share_used()


async def test_with_the_quality_score_off_the_answer_is_the_same_and_no_verifier_is_asked(settings, platform, mail):
    mail.conversation("Samples", them("Could you send two samples?", "2026-10-04T09:00:00"))
    answers = []
    for verify in (True, False):
        model = ScriptedModel(goals=[TRIAGE], verdict=_needs_reply)
        out = await agent.run(REPLY_CHECK, "suryodaya", settings.model_copy(update={"verify_answers": verify}),
                              today=TODAY, llm=model)
        assert final(out.run_dir).stopped == "done"
        assert len(model.calls("verifier")) == (1 if verify else 0)
        answers.append(final(out.run_dir).answer)
    assert answers[0] == answers[1]


async def test_an_unknown_capability_from_the_planner_is_sent_back_and_repaired(settings, platform, mail):
    mail.conversation("Samples", them("Could you send two samples?", "2026-10-04T09:00:00"))

    def plan(body):
        if not body["graph"] and "your_previous_proposals_were_rejected" not in body:
            return {"tasks": [task("mail_them", "send_email", "g1", to="buyer@acme.in")], "reason": "just reply"}
        return judge_then_answer(body)
    model = ScriptedModel(goals=[TRIAGE], plan=plan, verdict=_needs_reply)

    out = await agent.run(REPLY_CHECK, "suryodaya", settings, today=TODAY, llm=model)

    assert final(out.run_dir).stopped == "done"
    repaired = model.calls("planner")[1].messages[-1].text
    assert "send_email" in repaired and "not offered" in repaired


async def test_a_planner_that_never_makes_sense_ends_the_run_cleanly(settings, platform, mail):
    mail.conversation("Samples", them("Could you send two samples?", "2026-10-04T09:00:00"))
    model = ScriptedModel(goals=[TRIAGE], plan=lambda body: {"tasks": [task("x", "send_email", "g1")], "reason": "?"})

    out = await agent.run(REPLY_CHECK, "suryodaya", settings, today=TODAY, llm=model)

    result = final(out.run_dir)
    assert result.stopped == "error"
    assert "rejected 4 times" in (result.reason or "")
    assert len(model.calls("planner")) == 4
    assert platform.writes() == []


async def test_a_verdict_about_a_conversation_the_judge_was_not_given_is_never_written(settings, platform, mail):
    asks = mail.conversation("Samples", them("Could you send two samples?", "2026-10-04T09:00:00"))
    answered = mail.conversation("Visit", them("Can we visit Friday?", "2026-10-02T10:00:00"),
                                 us("Yes, Friday works.", "2026-10-02T15:00:00"))
    model = ScriptedModel(goals=[TRIAGE], verdict=_needs_reply)
    honest = model.scripts["judge"]
    replies = iter([
        lambda req: {"verdicts": [{"thread_id": t, **_needs_reply(t)} for t in [*thread_ids(req), answered]]},
        honest,
    ])
    model.scripts["judge"] = lambda req: next(replies)(req)

    out = await agent.run(REPLY_CHECK, "suryodaya", settings, today=TODAY, llm=model)

    assert len(model.calls("judge")) == 2, "one repair"
    assert "unknown" in model.calls("judge")[1].messages[-1].text
    assert platform.row("EmailThread", answered)["flag_status"] == "not_flagged"
    assert [w.row_id for w in writes(out.run_dir)] == [asks]


async def test_a_conversation_the_second_model_disputes_is_held_not_written(settings, platform, mail):
    settings.validate_verdicts = True
    agreed = mail.conversation("Samples", them("Could you send two samples?", "2026-10-04T09:00:00"))
    disputed = mail.conversation("FYI", them("Just so you know, the plant is closed Monday.", "2026-10-04T10:00:00"))
    judge = ScriptedModel(goals=[TRIAGE], verdict=_needs_reply, model="judge-model")
    second = ScriptedModel(goals=[TRIAGE], verdict=lambda t: {"needs_reply": t == agreed, "why": "only a notice"},
                           model="validator-model")
    route = RoutedLlm([Option("gemini", "judge-model", 1, judge), Option("openai", "validator-model", None, second)])

    out = await agent.run(REPLY_CHECK, "suryodaya", settings, today=TODAY, llm=route)

    assert platform.row("EmailThread", agreed)["flag_status"] == "flagged"
    assert platform.row("EmailThread", disputed)["flag_status"] == "not_flagged"
    [checked] = [e for e in journal(out.run_dir) if e.kind == "validator_checked"]
    assert checked.payload["held"] == [disputed]
    assert checked.payload["validator_model"] == "validator-model"


async def test_the_model_call_budget_is_never_overspent(settings, platform, mail):
    settings.max_llm_calls = 3                         # goals, the first plan, one shard; then nothing more
    for n in range(45):
        mail.conversation(f"Order {n}", them(f"Please confirm order {n}.", f"2026-10-04T09:{n:02d}:00"))
    model = ScriptedModel(goals=[TRIAGE], verdict=_needs_reply)

    out = await agent.run(REPLY_CHECK, "suryodaya", settings, today=TODAY, llm=model)

    assert len(model.requests) == 3
    assert budget_spent(out.run_dir)["llm_calls"] == 3
    assert final(out.run_dir).stopped == "max_steps", "a limit reached, not a crash"
    assert "llm_calls" in (final(out.run_dir).reason or "")
    assert platform.writes() == [], "a mailbox only partly judged is not written"


async def test_a_critic_that_keeps_rejecting_is_overruled_after_two_tries(settings, platform, mail):
    mail.conversation("Samples", them("Could you send two samples?", "2026-10-04T09:00:00"))

    def never_ready(req):
        if (req.response_schema or {}).get("title") == "VerifierScore":
            return {"score": 60, "critique": "thin", "issues": []}
        return {"ready": False, "missing": ["the customer's phone number"], "reason": "not in the evidence"}
    model = ScriptedModel(goals=[TRIAGE], verdict=_needs_reply, critic=never_ready)

    out = await agent.run(REPLY_CHECK, "suryodaya", settings, today=TODAY, llm=model)

    reviews = [e.payload for e in journal(out.run_dir) if e.kind == "critic_reviewed"]
    assert [r["overruled"] for r in reviews] == [False, False, True]
    assert final(out.run_dir).stopped == "done"
    assert final(out.run_dir).goals[0].done
