"""Many things happen at once in a real run: graph nodes, model calls and MCP calls overlap, worker threads write the
local SQLite files while the event loop reads them, two runs share one state folder, and team 11 changes the
platform's rows while we page through them. Each test is a collision that can happen in production."""

from __future__ import annotations

import asyncio

import httpx2
from mcp.types import CallToolResult, TextContent
from pydantic import SecretStr

from email_agent import agent
from email_agent.contracts.events import Subscription
from email_agent.contracts.graph import GraphPatch, TaskSpec
from email_agent.contracts.platform import EmailMessage, EmailThread
from email_agent.graph.executor import GraphExecutor
from email_agent.graph.store import RunStore
from email_agent.mailbox.store import MailboxStore
from email_agent.mailbox.sync import MailboxSync
from email_agent.platform.action import Action
from email_agent.platform.mcp_session import McpSession
from email_agent.platform.rest import RestClient
from email_agent.watch.watcher import Watcher
from tests.conftest import TODAY
from tests.kit.mail import SALES, them, us
from tests.kit.model import ScriptedModel, goal, rounds, task
from tests.kit.platform import now
from tests.kit.runs import final, nodes, writes

REPLY_CHECK = "What needs my reply today?"
TRIAGE = goal("Find the conversations that need our reply today", "triage-replies")
OURS = {SALES["email"]}


def _needs_reply(_thread_id):
    return {"needs_reply": True, "why": "they ask us something"}


# ── MCP calls ────────────────────────────────────────────────────────────────

class _Server:
    """Stands in for the MCP client inside a real McpSession. One tool can be made to hang forever."""

    def __init__(self, hang: str | None = None):
        self.hang, self.in_flight, self.peak = hang, 0, 0

    async def call_tool(self, name, _args):
        self.in_flight += 1
        self.peak = max(self.peak, self.in_flight)
        try:
            await (asyncio.Event().wait() if name == self.hang else asyncio.sleep(0.02))
        finally:
            self.in_flight -= 1
        return CallToolResult(content=[TextContent(type="text", text="{}")], structured_content={}, is_error=False)


async def test_parallel_nodes_never_have_more_mcp_calls_in_flight_than_allowed(settings):
    session = McpSession(settings, "suryodaya")
    session._client = server = _Server()

    outcomes = await asyncio.gather(*(session.call("EmailThread.list", {}) for _ in range(8)))

    assert all(o.ok for o in outcomes)
    assert server.peak == settings.mcp_concurrency


async def test_a_hung_mcp_call_times_out_alone_while_the_others_finish(settings):
    settings.mcp_call_timeout_s = 0.2
    session = McpSession(settings, "suryodaya")
    session._client = _Server(hang="EmailThread.update")

    hung, *rest = await asyncio.wait_for(asyncio.gather(
        session.call("EmailThread.update", {"id": "t1"}), *(session.call("EmailThread.list", {}) for _ in range(4))),
        timeout=2)

    assert not hung.ok and hung.error.kind == "timeout"
    assert all(o.ok for o in rest)


# ── graph nodes and model calls ──────────────────────────────────────────────

async def test_never_more_nodes_than_workers_and_a_slow_node_does_not_hold_back_the_rest(tmp_path):
    fast_done = asyncio.Event()
    running = peak = finished = 0

    class Planner:
        async def plan(self, graph, event):
            if event.kind == "run_started":
                return GraphPatch(add=[TaskSpec(id="a_slow", capability="slow"),     # first in id order: starts first
                                       *(TaskSpec(id=f"fast{n:02d}", capability="fast") for n in range(10))],
                                  reason="one slow and ten fast")
            done = all(n.state == "succeeded" for n in graph.nodes.values())
            return GraphPatch(finish=done, reason="all done" if done else "wait")

    async def fast(_task):
        nonlocal running, peak, finished
        running += 1
        peak = max(peak, running)
        await asyncio.sleep(0.01)
        running, finished = running - 1, finished + 1
        if finished == 10:
            fast_done.set()
        return {}

    async def slow(_task):
        nonlocal running, peak
        running += 1
        peak = max(peak, running)
        await fast_done.wait()            # finishes only once every fast node has, so it holds a slot meanwhile
        running -= 1
        return {}

    store = RunStore(tmp_path / "run.sqlite")
    report = await asyncio.wait_for(GraphExecutor(store, Planner(), {"fast": fast, "slow": slow}, max_workers=3)
                                    .run(run_id="r"), timeout=5)
    store.close()

    assert report.finished
    assert peak == 3, "never more than the three workers, and the slow node's slot did not stop the others"


async def test_parallel_shards_never_have_more_model_calls_in_flight_than_allowed(settings, platform, mail):
    settings.llm_concurrency = 2
    for n in range(45):
        mail.conversation(f"Order {n}", them(f"Please confirm order {n}.", f"2026-10-04T09:{n:02d}:00"))
    model = ScriptedModel(goals=[TRIAGE], verdict=_needs_reply, delay=0.01)

    out = await agent.run(REPLY_CHECK, "suryodaya", settings, today=TODAY, llm=model)

    assert final(out.run_dir).stopped == "done"
    assert model.peak == 2


# ── the local SQLite files ───────────────────────────────────────────────────

def _memories(platform, party, n):
    for k in range(n):
        platform.add("AgentMemory", party_id=party, category="fact", is_active=True,
                     content=f"Fact {k} about this customer.", created_at="2026-09-01T10:00:00",
                     updated_at="2026-09-01T10:00:00")


def _two_parties(platform):
    acme = platform.add("Party", name="Acme Forgings", email="buyer@acme.in")["id"]
    bharat = platform.add("Party", name="Bharat Castings", email="ops@bharat.in")["id"]
    for party in (acme, bharat):                    # enough rows that the two copies overlap in time
        _memories(platform, party, 1500)
    return acme, bharat


async def test_two_memory_recalls_in_the_same_round_both_succeed(settings, platform, mail):
    acme, bharat = _two_parties(platform)
    model = ScriptedModel(goals=[goal("What do we remember about Acme and Bharat?", "remember-about-customer")],
                          plan=rounds([task("about_acme", "recall_memory", "g1", party_id=acme),
                                       task("about_bharat", "recall_memory", "g1", party_id=bharat)]))

    out = await agent.run("What do we remember about Acme and Bharat?", "suryodaya", settings, today=TODAY, llm=model)

    recalled = nodes(out.run_dir)
    assert recalled["about_acme"].state == "succeeded", recalled["about_acme"].error
    assert recalled["about_bharat"].state == "succeeded", recalled["about_bharat"].error


async def test_remembering_one_customer_while_recalling_another_both_succeed(settings, platform, mail):
    acme, bharat = _two_parties(platform)
    model = ScriptedModel(goals=[goal("Remember Acme's invoice format", "remember-about-customer")],
                          plan=rounds([task("remember", "remember_fact", "g1", party_id=acme, category="preference",
                                            content="Acme wants invoices as PDF."),
                                       task("about_bharat", "recall_memory", "g1", party_id=bharat)]))

    out = await agent.run("Remember that Acme wants PDF invoices", "suryodaya", settings, today=TODAY, llm=model)

    done = nodes(out.run_dir)
    assert done["remember"].state == "succeeded", done["remember"].error
    assert done["about_bharat"].state == "succeeded", done["about_bharat"].error


def _big_copy(tmp_path, n=2000):
    copy = MailboxStore(tmp_path / "mailbox.sqlite")
    copy.upsert_threads([EmailThread(id=f"t{i}", subject=f"Order {i}", mailbox_id=SALES["id"],
                                     updated_at="2026-10-01T00:00:00") for i in range(n)])
    copy.upsert_messages([EmailMessage(id=f"m{i}", thread_id=f"t{i}", mailbox_id=SALES["id"], from_email="b@acme.in",
                                       body_text="Please confirm.", received_at="2026-10-01T09:00:00",
                                       updated_at="2026-10-01T09:00:00") for i in range(n)])
    return copy


async def test_a_writes_local_update_while_a_sync_rebuilds_in_a_thread_does_not_collide(tmp_path):
    copy = _big_copy(tmp_path)
    rebuild = asyncio.create_task(asyncio.to_thread(copy.rebuild_facts, copy.all_thread_ids(), OURS))
    await asyncio.sleep(0.05)                        # the rebuild is inside its transaction now

    updated = copy.apply_thread_update("t7", {"flag_status": "flagged"}, OURS)
    await rebuild
    flag = copy.threads([SALES["id"]])["t7"].flag_status
    copy.close()

    assert updated and flag == "flagged"


async def test_a_node_that_times_out_during_the_sync_leaves_the_sync_usable_for_the_next(settings, platform, mail, ctx,
                                                                                        tmp_path):
    for n in range(2000):
        mail.conversation(f"Order {n}", them(f"Please confirm order {n}.", "2026-10-01T09:00:00"))
    copy = MailboxStore(tmp_path / "mailbox.sqlite")
    action = Action(platform, ctx, None, copy, writes=None)

    try:
        await asyncio.wait_for(action._ensure_synced(), timeout=0.3)    # the node's time limit, mid-rebuild
    except TimeoutError:
        pass
    await action._ensure_synced()
    overviews = copy.overviews([SALES["id"]])
    copy.close()

    assert len(overviews) == 2000
    assert [name for name, _ in platform.calls].count("EmailThread.list") == 2, "one sync (two pages), shared by both"


async def test_two_runs_at_once_on_the_same_state_folder_both_finish(settings, platform, mail):
    a = mail.conversation("Samples", them("Could you send two samples?", "2026-10-04T09:00:00"))
    b = mail.conversation("Invoice copy", them("Please resend the invoice.", "2026-10-04T10:00:00"))

    first, second = await asyncio.gather(
        agent.run(REPLY_CHECK, "suryodaya", settings, today=TODAY, llm=ScriptedModel(goals=[TRIAGE], verdict=_needs_reply),
                  threads=[a]),
        agent.run(REPLY_CHECK, "suryodaya", settings, today=TODAY, llm=ScriptedModel(goals=[TRIAGE], verdict=_needs_reply),
                  threads=[b]))

    assert final(first.run_dir).stopped == final(second.run_dir).stopped == "done"
    assert platform.row("EmailThread", a)["flag_status"] == platform.row("EmailThread", b)["flag_status"] == "flagged"


async def test_two_nodes_writing_the_same_conversation_both_land(settings, platform, mail):
    asks = mail.conversation("Samples", them("Could you send two samples?", "2026-10-04T09:00:00"))
    platform.delay = 0.01
    model = ScriptedModel(goals=[TRIAGE], plan=rounds([
        task("flag", "EmailThread.update", "g1", id=asks, flag_status="flagged"),
        task("star", "EmailThread.update", "g1", id=asks, is_starred=True)]))

    out = await agent.run(REPLY_CHECK, "suryodaya", settings, today=TODAY, llm=model)

    row = platform.row("EmailThread", asks)
    assert row["flag_status"] == "flagged" and row["is_starred"] is True
    assert sorted((w.fields for w in writes(out.run_dir)), key=str) == [{"flag_status": "flagged"}, {"is_starred": True}]


# ── the platform's rows change while we read them ────────────────────────────

async def test_a_row_changed_between_sync_pages_is_not_deleted_from_the_copy(platform, mail, tmp_path):
    thread = mail.conversation("Long thread", them("Opening question.", "2026-06-01T08:00:00"))
    for k in range(1499):
        mail.say(thread, us(f"Note {k}.", f"2026-06-{1 + k // 60:02d}T{8 + (k // 5) % 12:02d}:{k % 60:02d}:00"))
    copy = MailboxStore(tmp_path / "mailbox.sqlite")
    sync = MailboxSync(platform, copy, "suryodaya", [type("Mb", (), {"id": SALES["id"], "email": SALES["email"]})()],
                       OURS)
    await sync.run(full=True)
    older = sorted(platform.rows["EmailMessage"].values(), key=lambda m: m["updated_at"])[100]["id"]

    def team11_edits_during_the_pass(name, args):
        if name == "EmailMessage.list" and args.get("offset") == 1000 and not platform.rows["EmailMessage"][older].get("seen"):
            platform.rows["EmailMessage"][older].update(updated_at=now(), seen=True)
    platform.hooks.append(team11_edits_during_the_pass)
    await sync.run(full=True)
    kept = copy.message(older)
    copy.close()

    assert kept is not None, "the row moved to page 1 after page 1 was read; it still exists on the platform"


async def test_an_existing_follow_up_reminder_past_the_first_thousand_is_not_created_again(settings, platform, mail):
    waiting = mail.conversation("Quote sent", them("Please quote 20 valves.", "2026-09-25T09:00:00"),
                                us("Our quote is attached.", "2026-09-26T09:00:00"))
    ours = mail.messages(waiting)[-1]["id"]
    platform.add("EmailReminder", thread_id=waiting, message_id=ours, type="follow_up", is_fired=False,
                 remind_at="2026-10-08", created_at="2026-09-26T10:00:00", updated_at="2026-09-26T10:00:00")
    for n in range(1000):                            # newer reminders on other conversations come first in the list
        platform.add("EmailReminder", thread_id=f"elsewhere-{n}", message_id=f"m-{n}", type="follow_up",
                     is_fired=False, remind_at="2026-10-09", updated_at=f"2026-10-0{1 + n % 4}T10:00:00")
    model = ScriptedModel(goals=[goal("Remind me where I am waiting on a reply", "follow-up-reminders")],
                          verdict=lambda t: {"waiting_on_them": True, "remind_at": None, "note": "waiting for the PO"})

    await agent.run("Remind me where I am waiting on a reply", "suryodaya", settings, today=TODAY, llm=model)

    assert platform.writes("EmailReminder.create") == []


# ── the watcher ──────────────────────────────────────────────────────────────

async def test_a_burst_of_events_never_spends_more_than_the_daily_model_call_ceiling(settings, platform, mail):
    model = ScriptedModel(goals=[TRIAGE], verdict=_needs_reply)
    sub = Subscription(id="reply-check", instances=["suryodaya"], event_types=["new_inbound"],
                       request=REPLY_CHECK, max_llm_calls_per_day=10)
    w = Watcher(settings, "suryodaya", run=lambda *a, **kw: agent.run(*a, llm=model, today=TODAY, **kw),
                subscriptions=[sub])
    mail.conversation("Old", them("Old mail.", "2026-10-03T09:00:00"))
    await w.poll()
    for n in range(3):
        mail.conversation(f"Question {n}", them(f"Question {n}?", now(seconds=n)))

    report = await w.poll()
    w.close()

    assert report.runs, "the first event still gets a run"
    assert len(model.requests) <= 10
    assert sum(r.llm_calls for r in report.runs) <= 10


async def test_a_poll_that_fails_is_reported_and_the_next_poll_works(settings, platform, mail):
    mail.conversation("Old", them("Old mail.", "2026-10-03T09:00:00"))
    w = Watcher(settings, "suryodaya", subscriptions=[])
    platform.no_answer["Mailbox.list"] = False

    failed = await w.poll_safely()
    del platform.no_answer["Mailbox.list"]
    ok = await w.poll_safely()
    w.close()

    assert failed.error and "Mailbox.list" in failed.error
    assert ok.error is None and ok.baseline


# ── REST ─────────────────────────────────────────────────────────────────────

def test_a_rest_write_after_the_token_expired_logs_in_again_and_succeeds(settings):
    settings.as_suryodaya_password = SecretStr("not-a-real-password")
    logins = []

    def server(request: httpx2.Request) -> httpx2.Response:
        if request.url.path == "/api/auth/login":
            logins.append(1)
            return httpx2.Response(200, json={"token": "fresh"})
        if request.headers.get("Authorization") != "Bearer fresh":
            return httpx2.Response(401, json={"detail": "token expired"})
        return httpx2.Response(200, json={"id": "t1", "flag_due_date": None})

    rest = RestClient(settings, "suryodaya")
    rest._http = httpx2.Client(base_url=rest.instance.base_url, transport=httpx2.MockTransport(server))
    rest._token = "expired"

    resp = rest.request("PUT", "/api/EmailThread/t1", json_body={"flag_due_date": None})

    assert resp.status_code == 200
    assert len(logins) == 1
