"""Memory that outlives a run: what we were told about a customer comes back later for that customer only, a memory
switched off on the platform stops counting, and the planner sees earlier runs on its own mailbox only."""

from __future__ import annotations

from email_agent import agent
from email_agent.contracts.memory import Episode
from email_agent.memory.store import LongTermMemory
from tests.conftest import TODAY
from tests.kit.mail import them
from tests.kit.model import ScriptedModel, goal, planner_body, rounds, task
from tests.kit.runs import nodes

REMEMBER = goal("Remember Acme's invoice preference", "remember-about-customer")


def _parties(platform):
    acme = platform.add("Party", name="Acme Forgings", email="buyer@acme.in", type="customer")["id"]
    bharat = platform.add("Party", name="Bharat Castings", email="ops@bharat.in", type="customer")["id"]
    return acme, bharat


def _recalled(run_dir, node_id):
    return nodes(run_dir)[node_id].result["result"]


async def test_a_fact_remembered_in_one_run_comes_back_later_for_that_customer_only(settings, platform, mail):
    acme, bharat = _parties(platform)
    told = ScriptedModel(goals=[REMEMBER], plan=rounds(
        [task("recall", "recall_memory", "g1", party_id=acme)],
        [task("remember", "remember_fact", "g1", party_id=acme, category="preference",
              content="Acme wants every invoice as a PDF, never as a spreadsheet.")]))
    await agent.run("Remember that Acme wants invoices as PDF", "suryodaya", settings, today=TODAY, llm=told)
    assert [m["party_id"] for m in platform.rows["AgentMemory"].values()] == [acme]

    later = ScriptedModel(goals=[goal("Say what we remember about Acme and Bharat", "remember-about-customer")],
                          plan=rounds([task("about_acme", "recall_memory", "g1", party_id=acme)],
                                      [task("about_bharat", "recall_memory", "g1", party_id=bharat)]))
    out = await agent.run("What do we remember about Acme and Bharat?", "suryodaya", settings, today=TODAY, llm=later)

    assert "invoice as a PDF" in _recalled(out.run_dir, "about_acme")
    assert "PDF" not in _recalled(out.run_dir, "about_bharat")


async def test_a_memory_switched_off_on_the_platform_is_no_longer_recalled(settings, platform, mail):
    acme, _ = _parties(platform)
    memory = platform.add("AgentMemory", party_id=acme, category="instruction", is_active=True,
                          content="Always copy accounts@acme.in on invoices.", created_at="2026-09-20T10:00:00",
                          updated_at="2026-09-20T10:00:00")

    def recall_acme():
        return ScriptedModel(goals=[REMEMBER], plan=rounds([task("recall", "recall_memory", "g1", party_id=acme)]))
    first = await agent.run("What do we remember about Acme?", "suryodaya", settings, today=TODAY, llm=recall_acme())
    assert "accounts@acme.in" in _recalled(first.run_dir, "recall")

    memory.update(is_active=False, updated_at="2026-10-04T10:00:00")              # someone switched it off
    second = await agent.run("What do we remember about Acme?", "suryodaya", settings, today=TODAY, llm=recall_acme())

    assert "accounts@acme.in" not in _recalled(second.run_dir, "recall")


async def test_the_planner_sees_earlier_runs_on_this_mailbox_but_not_other_mailboxes_or_this_run(settings, platform, mail):
    mail.conversation("Samples", them("Could you send two samples?", "2026-10-04T09:00:00"))
    triage = goal("Find the conversations that need our reply today", "triage-replies")
    await agent.run("Which mail needs a reply?", "suryodaya", settings, today=TODAY, llm=ScriptedModel(goals=[triage]),
                    dry_run=True)
    store = LongTermMemory.for_instance(settings.state_dir, "suryodaya")
    try:
        store.save_episode(Episode(run_id="elsewhere-1", day="2026-10-04", request="Sort the orders inbox",
                                   stopped="done", dry_run=False, goals=[],
                                   mailboxes=["orders@elsewhere.in"]), ["mb-elsewhere"], "someone")
    finally:
        store.close()
    model = ScriptedModel(goals=[triage])

    await agent.run("What needs my reply today?", "suryodaya", settings, today=TODAY, llm=model, history=True)

    runs = planner_body(model.calls("planner")[0])["recent_runs"]["runs"]
    assert len(runs) == 1
    assert "Which mail needs a reply?" in runs[0] and "dry run" in runs[0]
    assert not any("Sort the orders inbox" in r or "What needs my reply today?" in r for r in runs)
