"""A real inbox is larger than one model call and changes between runs: the work is split into shards, the local copy
of the mailbox is kept up to date cheaply, and rows from mailboxes that are not ours never reach the model."""

from __future__ import annotations

import asyncio

from email_agent import agent
from email_agent.contracts.graph import FanOut, GraphPatch, TaskSpec
from email_agent.graph.executor import GraphExecutor
from email_agent.graph.store import RunStore
from email_agent.llm.route import Option, RoutedLlm
from email_agent.mailbox.store import MailboxStore
from tests.conftest import TODAY
from tests.kit.mail import SALES, TEAM11, them, us
from tests.kit.model import ScriptedModel, goal, judge_then_answer, task, thread_ids
from tests.kit.runs import final, journal, nodes, syncs

REPLY_CHECK = "What needs my reply today?"
TRIAGE = goal("Find the conversations that need our reply today", "triage-replies")


def _model(**kw):
    return ScriptedModel(goals=[TRIAGE], verdict=lambda t: {"needs_reply": True, "why": "they ask us"}, **kw)


async def test_a_large_inbox_is_judged_in_shards_and_the_answer_waits_for_all_of_them(settings, platform, mail):
    waiting = {mail.conversation(f"Order {n}", them(f"Please confirm order {n}.", f"2026-10-04T09:{n:02d}:00"))
               for n in range(45)}
    model = _model()

    out = await agent.run(REPLY_CHECK, "suryodaya", settings, today=TODAY, llm=model)

    shards = [n for n in nodes(out.run_dir).values() if n.capability == "judge_shard"]
    assert len(shards) == 3 and all(len(s.input["thread_ids"]) <= 20 for s in shards)
    assert {t for s in shards for t in s.input["thread_ids"]} == waiting
    order = [(e.kind, e.node_id) for e in journal(out.run_dir)]
    answer_starts = order.index(("node_started", "answer_g1"))
    assert all(order.index(("node_succeeded", s.id)) < answer_starts for s in shards)
    assert sum(platform.row("EmailThread", t)["flag_status"] == "flagged" for t in waiting) == 45


async def test_a_task_already_waiting_on_a_fan_out_waits_for_its_last_step(tmp_path):
    """Found live: an answer that waited on judge_threads ran as soon as it fanned out, before any shard, and ended
    the goal. Here the answer is wired to the fan-out node before the fan-out exists."""
    ran: list[str] = []

    class Planner:
        async def plan(self, graph, event):
            if event.kind == "run_started":
                return GraphPatch(add=[TaskSpec(id="judge", capability="fan"), TaskSpec(id="answer", capability="answer")],
                                  connect=[("judge", "answer")], reason="judge, then answer")
            return GraphPatch(finish=event.node_id == "answer", reason="answered" if event.node_id == "answer" else "wait")

    async def fan(_task):
        shards = [TaskSpec(id=f"shard{n}", capability="work", wakes_planner=False) for n in (1, 2, 3)]
        return FanOut(result={"shards": 3},
                      patch=GraphPatch(add=[*shards, TaskSpec(id="write", capability="work")],
                                       connect=[(s.id, "write") for s in shards], reason="3 shards",
                                       metadata={"final": "write"}))

    async def work(task):
        await asyncio.sleep(0.01)
        ran.append(task.id)
        return {}

    async def answer(_task):
        ran.append("answer")
        return {}

    store = RunStore(tmp_path / "run.sqlite")
    report = await GraphExecutor(store, Planner(), {"fan": fan, "work": work, "answer": answer}).run(run_id="r")
    store.close()

    assert report.finished
    assert ran[-2:] == ["write", "answer"]


async def test_more_candidates_than_the_budget_allows_are_judged_newest_first_and_the_rest_reported(settings,
                                                                                                    platform, mail):
    settings.max_nodes = 10                      # less the judge node itself, join, check, write, answer: 5 shards
    for n in range(200):
        mail.conversation(f"Order {n}", them(f"Please confirm order {n}.", f"2026-10-04T{n // 60:02d}:{n % 60:02d}:00"))
    model = _model()

    out = await agent.run(REPLY_CHECK, "suryodaya", settings, today=TODAY, llm=model)

    assert final(out.run_dir).stopped == "done"
    judge = nodes(out.run_dir)["judge_g1"].result
    assert judge["shards"] == 5 and judge["not_judged"] == 200 - 5 * 20
    judged = {t for req in model.calls("judge") for t in thread_ids(req)}
    newest = sorted(platform.rows["EmailThread"].values(), key=lambda r: r["updated_at"], reverse=True)[:100]
    assert judged == {r["id"] for r in newest}
    assert "not judged" in model.calls("answer")[0].messages[0].text, "the answer is told what was left out"


async def test_with_the_second_model_on_every_conversation_to_be_written_is_checked_first(settings, platform, mail):
    settings.validate_verdicts = True
    asks = {mail.conversation(f"Order {n}", them(f"Please confirm order {n}?", f"2026-10-04T09:{n:02d}:00"))
            for n in range(60)}                 # more than 40 plus the 5 sampled from the rest
    judge = _model(model="judge-model")
    second = _model(model="validator-model")
    route = RoutedLlm([Option("gemini", "judge-model", 1, judge), Option("openai", "validator-model", None, second)])

    await agent.run(REPLY_CHECK, "suryodaya", settings, today=TODAY, llm=route)

    checked = {t for req in second.calls("validator") for t in thread_ids(req)}
    assert checked == asks, "all 60, not the first 40"
    assert sum(platform.row("EmailThread", t)["flag_status"] == "flagged" for t in asks) == 60


async def test_a_second_sync_of_an_unchanged_mailbox_reads_only_the_newest_page(settings, platform, mail):
    for n in range(10):                                   # 1,200 messages: two pages of 1,000 on a full read
        t = mail.conversation(f"Long thread {n}", them("Opening question.", "2026-06-01T08:00:00"))
        for k in range(119):
            mail.say(t, us(f"Note {k} on thread {n}.", f"2026-06-{1 + k // 5:02d}T{9 + k % 5:02d}:{n:02d}:00"))
    await agent.run(REPLY_CHECK, "suryodaya", settings, today=TODAY, llm=_model(), dry_run=True)
    assert [name for name, _ in platform.calls].count("EmailMessage.list") == 2
    platform.calls.clear()

    out = await agent.run(REPLY_CHECK, "suryodaya", settings, today=TODAY, llm=_model(), dry_run=True)

    assert [name for name, _ in platform.calls].count("EmailMessage.list") == 1
    [report] = syncs(out.run_dir)
    assert all(table.changed == 0 for table in report.tables)
    assert report.facts_recomputed == 0


async def test_a_new_message_between_runs_is_picked_up_by_the_next_run(settings, platform, mail):
    visit = mail.conversation("Visit", them("Can we visit Friday?", "2026-10-02T10:00:00"),
                              us("Yes, Friday works.", "2026-10-02T15:00:00"))
    first = _model()
    await agent.run(REPLY_CHECK, "suryodaya", settings, today=TODAY, llm=first, dry_run=True)
    assert first.calls("judge") == [], "nothing was waiting on us"

    mail.say(visit, them("Sorry, can we make it Monday instead?", "2026-10-04T08:00:00"))
    second = _model()
    await agent.run(REPLY_CHECK, "suryodaya", settings, today=TODAY, llm=second, dry_run=True)

    assert [t for req in second.calls("judge") for t in thread_ids(req)] == [visit]


async def test_a_conversation_deleted_on_the_platform_leaves_the_local_copy_on_a_full_sync(settings, platform, mail):
    gone = mail.conversation("Spam", them("Win a prize!", "2026-10-03T10:00:00"))
    kept = mail.conversation("Samples", them("Could you send two samples?", "2026-10-04T09:00:00"))
    await agent.run(REPLY_CHECK, "suryodaya", settings, today=TODAY, llm=_model(), dry_run=True)

    del platform.rows["EmailThread"][gone]
    for m in [m for m in platform.rows["EmailMessage"].values() if m["thread_id"] == gone]:
        del platform.rows["EmailMessage"][m["id"]]
    model = _model()
    await agent.run(REPLY_CHECK, "suryodaya", settings, today=TODAY, llm=model, dry_run=True, full_sync=True)

    copy = MailboxStore.for_instance(settings.state_dir, "suryodaya")
    try:
        assert set(copy.threads([SALES["id"]])) == {kept}
    finally:
        copy.close()
    assert [t for req in model.calls("judge") for t in thread_ids(req)] == [kept]


async def test_rows_from_another_mailbox_in_a_listing_never_reach_the_model(settings, platform, mail):
    platform.leaks_other_mailboxes = True
    ours = mail.conversation("Samples", them("Could you send two samples?", "2026-10-04T09:00:00"))
    theirs = mail.conversation("Team 11 secret price", them("Our cost is 310 per unit.", "2026-10-04T10:00:00"),
                               mailbox=TEAM11)

    def plan(body):
        if not body["graph"]:
            return {"tasks": [task("judge_g1", "judge_threads", "g1", skill="triage-replies"),
                              task("read_mail", "EmailMessage.list", "g1", limit=50)], "reason": "judge and read"}
        return judge_then_answer(body)
    model = _model(plan=plan)

    out = await agent.run(REPLY_CHECK, "suryodaya", settings, today=TODAY, llm=model)

    seen = "\n".join(m.text for req in model.requests for m in req.messages)
    assert theirs not in seen and "310 per unit" not in seen
    assert ours in seen
    assert "left_out" in nodes(out.run_dir)["read_mail"].result["result"]
    assert platform.row("EmailThread", theirs)["flag_status"] == "not_flagged"
