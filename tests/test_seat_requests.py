"""The seat's own requests, end to end: the real agent, graph, flows and write path, with only the model and the
platform faked. Each test reads the result where a user would see it: the book and the run's files."""

from __future__ import annotations

from email_agent import agent
from tests.conftest import TODAY
from tests.kit.mail import them, us
from tests.kit.model import ScriptedModel, goal, thread_ids
from tests.kit.runs import final, writes

REPLY_CHECK = "What needs my reply today?"


async def test_reply_triage_flags_only_the_conversations_waiting_on_us(settings, platform, mail):
    asks = mail.conversation("Price for 200 flanges", them("Can you quote 200 DN50 flanges by Friday?", "2026-10-03T09:00:00"))
    digest = mail.conversation("Steel prices this week", them("Our weekly digest of steel prices.", "2026-10-04T06:00:00"),
                               counterpart="news@steelweekly.in")
    answered = mail.conversation("Visit on Friday",
                                 them("Can we visit your plant on Friday?", "2026-10-02T10:00:00"),
                                 us("Yes, Friday 11:00 works for us.", "2026-10-02T15:00:00"))
    mirrored = mail.conversation("Delivery date",
                                 us("Please confirm the delivery date for PO 4471.", "2026-10-01T09:00:00"),
                                 them("Please confirm the delivery date for PO 4471.", "2026-10-02T09:00:00"))
    model = ScriptedModel(goals=[goal("Find the conversations that need our reply today", "triage-replies")],
                          verdict=lambda t: {"needs_reply": t == asks, "why": "they ask for a quote" if t == asks
                                             else "a newsletter asks nothing"})

    out = await agent.run(REPLY_CHECK, "suryodaya", settings, today=TODAY, llm=model)

    judged = {t for req in model.calls("judge") for t in thread_ids(req)}
    assert judged == {asks, digest}, "answered and mirror-copied conversations are not waiting on us"
    assert platform.row("EmailThread", asks)["flag_status"] == "flagged"
    assert platform.row("EmailThread", asks)["flag_due_date"] == TODAY.isoformat()
    for untouched in (digest, answered, mirrored):
        assert platform.row("EmailThread", untouched)["flag_status"] == "not_flagged"
    assert [(w.tool, w.row_id) for w in writes(out.run_dir)] == [("EmailThread.update", asks)]
    assert final(out.run_dir).stopped == "done"


def _price_mail(mail):
    deal = mail.conversation("Quote QTN-2026-00031",
                             them("Please quote 200 DN50 flanges.", "2026-09-28T09:00:00"),
                             us("Our quote QTN-2026-00031:\n200 x DN50 flange @ 450.00 = 90,000.00", "2026-09-29T11:00:00"),
                             them("We accept QTN-2026-00031, please go ahead.", "2026-10-01T10:00:00"),
                             party_id="party-acme")
    open_quote = mail.conversation("RFQ for gaskets",
                                   them("What is your price for 500 gaskets?", "2026-10-02T09:00:00"),
                                   us("500 x gasket @ 12.00 = 6,000.00", "2026-10-02T12:00:00"))
    return deal, open_quote


def _agreed(mail, deal, **figures):
    accept = mail.messages(deal)[-1]
    return {"status": "agreed", "why": "they accept our quote", "agreement_message_id": accept["id"],
            "agreed_on": "2026-10-01", "reference": "QTN-2026-00031", "item": "DN50 flange", "party_id": "party-acme",
            "quantity": 200, "unit_price": 450.0, "total": 90000.0, **figures}


async def test_an_agreed_price_is_recorded_with_its_figures_and_the_conversation_starred(settings, platform, mail):
    deal, open_quote = _price_mail(mail)
    model = ScriptedModel(goals=[goal("Find the mail where they agreed the price", "find-price-agreement")],
                          verdict=lambda t: _agreed(mail, deal) if t == deal
                          else {"status": "quote_only", "why": "no acceptance yet"})

    out = await agent.run("Find the mail where they agreed the price", "suryodaya", settings, today=TODAY, llm=model)

    [memory] = platform.rows["AgentMemory"].values()
    first_line = memory["content"].splitlines()[0]
    assert f"thread={deal}" in first_line
    assert "qty=200 | unit=450.00 | total=90000.00 | currency=INR" in first_line
    assert memory["party_id"] == "party-acme" and memory["is_active"] is True
    assert platform.row("EmailThread", deal)["is_starred"] is True
    assert platform.row("EmailThread", open_quote)["is_starred"] is False
    assert final(out.run_dir).stopped == "done"


async def test_a_price_whose_figures_do_not_add_up_is_never_recorded(settings, platform, mail):
    deal, _ = _price_mail(mail)
    model = ScriptedModel(goals=[goal("Find the mail where they agreed the price", "find-price-agreement")],
                          verdict=lambda t: _agreed(mail, deal, total=9000.0) if t == deal
                          else {"status": "quote_only", "why": "no acceptance yet"})

    await agent.run("Find the mail where they agreed the price", "suryodaya", settings, today=TODAY, llm=model)

    assert platform.rows["AgentMemory"] == {}
    assert platform.writes() == []


async def test_the_full_seat_request_ends_each_goal_exactly_once(settings, platform, mail):
    deal, _ = _price_mail(mail)
    asks = mail.conversation("Samples", them("Could you send two samples next week?", "2026-10-04T09:00:00"))
    model = ScriptedModel(
        goals=[goal("Find the conversations that need our reply today", "triage-replies"),
               goal("Find the mail where they agreed the price", "find-price-agreement")],
        verdict={"triage-replies": lambda t: {"needs_reply": t == asks, "why": "a question for us"},
                 "find-price-agreement": lambda t: _agreed(mail, deal) if t == deal
                 else {"status": "quote_only", "why": "no acceptance yet"}})

    out = await agent.run("What needs my reply today, and find the mail where they agreed the price.", "suryodaya",
                          settings, today=TODAY, llm=model)

    result = final(out.run_dir)
    assert result.stopped == "done"
    assert [(g.done, g.refused) for g in result.goals] == [(True, False), (True, False)]
    assert len(model.calls("answer")) == 2, "one answer per goal, never a second one"
    assert platform.row("EmailThread", asks)["flag_status"] == "flagged"
    assert len(platform.rows["AgentMemory"]) == 1


async def test_a_salary_request_is_refused_as_out_of_seat_without_planning_or_writing(settings, platform, mail):
    mail.conversation("Payroll query", them("What is Ravi's salary this month?", "2026-10-04T09:00:00"))
    model = ScriptedModel(goals=[goal("Tell the salary of Ravi", None, "out_of_seat")])

    out = await agent.run("What is Ravi's salary?", "suryodaya", settings, today=TODAY, llm=model)

    result = final(out.run_dir)
    assert result.refused and result.goals[0].refusal == "out_of_seat"
    assert model.calls("planner") == []
    assert platform.writes() == [] and writes(out.run_dir) == []


async def test_a_request_about_another_teams_mailbox_is_refused_as_not_ours(settings, platform, mail):
    model = ScriptedModel(goals=[goal("Flag everything in orders@team11.example", None, "not_our_mailbox")])

    out = await agent.run("Flag every mail in orders@team11.example", "suryodaya", settings, today=TODAY, llm=model)

    result = final(out.run_dir)
    assert result.refused and result.goals[0].refusal == "not_our_mailbox"
    assert platform.writes() == []


async def test_a_mixed_request_answers_one_goal_and_refuses_the_other_in_the_same_run(settings, platform, mail):
    asks = mail.conversation("Samples", them("Could you send two samples next week?", "2026-10-04T09:00:00"))
    model = ScriptedModel(goals=[goal("Find the conversations that need our reply today", "triage-replies"),
                                 goal("Tell the salary of Ravi", None, "out_of_seat")],
                          verdict=lambda t: {"needs_reply": True, "why": "a question for us"})

    out = await agent.run("What needs my reply today, and what is Ravi's salary?", "suryodaya", settings,
                          today=TODAY, llm=model)

    reply, salary = final(out.run_dir).goals
    assert reply.done and not reply.refused
    assert salary.refused and salary.refusal == "out_of_seat"
    assert not final(out.run_dir).refused, "one refused goal does not make the whole run a refusal"
    assert platform.row("EmailThread", asks)["flag_status"] == "flagged"
