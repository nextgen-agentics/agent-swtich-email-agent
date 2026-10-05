"""The agent (Revision 12): an S17-style live graph — planner first, parallel nodes, a journal you can resume.

    request.json ─► context ─► RunStore (runs/<run_id>/run.sqlite) ─► GraphExecutor:
        planner (LLM: goals, then the next tasks) ─► ready nodes run in parallel ─► outcome ─► planner … ─► finish
    ─► FinalAnswer ─► runs/<run_id>/

Whole-mailbox work is one `judge_threads` task that fans out into parallel judging shards, a join and a write step
(flows.py, workers.py); named records and the memory skill use tool capabilities through Action; every write goes
through WritePath (guard, dry run, outbox, record). The old Perception → Decision loop is gone; this graph is still our
own loop (graph/executor.py, adapted from S17).

Every exit writes final.json, outcome.json and report.md: a normal finish, a crash (stopped="crashed", the run returns
normally) and Ctrl-C (stopped="interrupted", the files are written, then it is re-raised).

Resume (Stage 6): `resume(run_id)` reopens runs/<run_id>/ and continues the same graph. First reconcile (reconcile.py)
settles every write left uncertain by reading the live rows; then your answer to a waiting approval is applied
(`approve` / `reject`); then the executor goes on: running nodes run again, planner events without a patch are planned,
and model replies saved before the stop are reused. A write that got no answer during a run is settled the same way
before the run ends.

Memory (Stage 7): the run reads long-term memory through `recall_memory` (memory.sqlite, the read copy of AgentMemory);
with `history`, the planner's first round sees the last runs on the same mailbox(es), fixed at the start so a resumed
run shows it the same; every exit saves this run's episode (memory_store.LongTermMemory).
"""

from __future__ import annotations

import asyncio
import logging
import secrets
from dataclasses import dataclass, field
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Literal

from email_agent.common.errors import leaves, run_error
from email_agent.common.skill_registry import SkillRegistry
from email_agent.config import Settings
from email_agent.contracts.agent import (
    FinalAnswer,
    Goal,
    RunContext,
    RunError,
    RunOutcome,
    RunRequest,
    Stopped,
    Where,
    WriteRecord,
)
from email_agent.contracts.events import OurWrite
from email_agent.contracts.graph import APPROVAL, ReconcileReport, RunReport
from email_agent.contracts.llm import LlmReply, Usage
from email_agent.contracts.memory import Episode, EpisodeGoal
from email_agent.contracts.runlog import ErrorStep, SyncStep
from email_agent.graph.capabilities import GRAPH_CAPABILITIES, Capabilities
from email_agent.graph.executor import NODE_BUDGET, GraphExecutor
from email_agent.graph.outbox import RECONCILE, WriteOutbox
from email_agent.graph.planner import RunPlanner
from email_agent.graph.reconcile import reconcile
from email_agent.graph.store import RUN_FILE, CrashPoint, RunStore
from email_agent.graph.workers import GraphRuntime, LimitedLlm
from email_agent.llm.route import Llm, RoutedLlm, make_llm, plan_route, route_text
from email_agent.mailbox.search import Embedder, MailSearch, MemorySearch
from email_agent.mailbox.store import MailboxStore
from email_agent.memory.store import LongTermMemory, VerdictCache
from email_agent.platform.action import LOCAL_TOOLS, Action
from email_agent.platform.context import build_context
from email_agent.platform.mcp_session import McpSession
from email_agent.platform.rest import RestClient
from email_agent.platform.writes import WritePath
from email_agent.record.artifacts import Artifacts
from email_agent.record.console import ConsoleView, TimedLlm
from email_agent.record.run_report import write_report
from email_agent.record.run_view import page_at_run_end
from email_agent.record.runlog import RunLog
from email_agent.record.telemetry import at_run_end
from email_agent.watch.store import EventStore


@dataclass
class _Progress:
    """How far the run got — what the crash handler and _finish need when the loop stops early."""

    goals: list[Goal] = field(default_factory=list)
    it: int = 0                                   # planner rounds
    nodes: int = 0                                # graph nodes executed
    where: Where = "connect"
    stopped: Stopped = "max_steps"
    reason: str | None = None
    error: RunError | None = None


def served_label(reply: LlmReply) -> str:
    """Who answered one LLM call: provider/model, plus the Gemini key slot (never the key)."""
    return f"{reply.provider or '?'}/{reply.model}" + (f" key #{reply.key_slot}" if reply.key_slot else "")


def _final(goals: list[Goal], stopped: Stopped, reason: str | None = None, error: RunError | None = None) -> FinalAnswer:
    parts = [g.answer for g in goals if g.answer]
    refused = bool(goals) and all(g.refused for g in goals)
    return FinalAnswer(answer="\n\n".join(parts) or (reason or "No answer."), goals=goals, refused=refused,
                       reason=reason, stopped=stopped, error=error)


Decision = Literal["approve", "reject"]
RECONCILE_PASSES = 2             # in-run: settle an unanswered write and go on, at most this many times


async def run(request: str, instance: str, settings: Settings, *, today: date | None = None,
              llm: Llm | None = None, runs_dir: Path | None = None, dry_run: bool = False,
              mailboxes: list[str] | None = None, view: ConsoleView | None = None,
              full_sync: bool = False, cache: bool = False, approve_writes: bool = False,
              history: bool = False, threads: list[str] | None = None,
              search: Literal["fts", "hybrid"] | None = None) -> RunOutcome:
    """`mailboxes`: addresses to work in (None = the instance default in config.INSTANCES).
    `full_sync`: re-read the whole mailbox into the local copy instead of only what changed.
    `cache`: reuse saved verdicts for conversations that have not changed (off for the harness: scores must measure
    the model, not the cache).
    `approve_writes`: stop before any platform write and wait for `resume(..., decision="approve")`.
    `history`: the planner's first round sees the last runs on these mailboxes (off for the harness, like the cache).
    `threads`: only these conversations may be judged or written (the watcher's runs; enforced by code).
    `search`: "hybrid" adds meaning-based search (Stage 9) to judge_threads(search=…) and recall_memory(query=…);
    None = the SEARCH setting (default "fts", full text only).
    `view`: prints each step to the terminal as it happens (None = quiet)."""
    run_id = f"{instance}-{datetime.now(timezone.utc):%Y%m%dT%H%M%SZ}-{secrets.token_hex(2)}"
    log = RunLog((runs_dir or settings.runs_dir) / run_id, on_step=view.step if view else None)
    planned = plan_route(settings)
    req = RunRequest(run_id=run_id, request=request, instance=instance, provider=settings.provider,
                     model=llm.model if llm else (planned[0][1] if planned else "none"),
                     route=f"{llm.model} (given)" if llm else route_text(settings),
                     dry_run=dry_run, today=today, mailboxes=mailboxes, full_sync=full_sync, cache=cache,
                     approve_writes=approve_writes, history=history, threads=threads,
                     search=search or settings.search)
    log.begin(req)
    return await _drive(req, settings, llm, log, view, resumed=False, decision=None)


def run_dir_of(run: str | Path, settings: Settings, runs_dir: Path | None = None) -> Path:
    """A run id (looked up in runs/) or a run folder path (e.g. one inside harness_runs/<batch>/runs/)."""
    path = Path(run)
    if not (path / RUN_FILE).exists():
        path = (runs_dir or settings.runs_dir) / str(run)
    if not (path / RUN_FILE).exists():
        raise FileNotFoundError(f"no resumable run at {path} (runs from before Revision 12 have no {RUN_FILE})")
    return path


def resume_arg(run_dir: Path) -> str:
    """What to pass to --resume for this run folder: its path, relative when it is under the working directory."""
    try:
        return str(run_dir.resolve().relative_to(Path.cwd()))
    except ValueError:
        return str(run_dir)


async def resume(run: str | Path, settings: Settings, *, llm: Llm | None = None, runs_dir: Path | None = None,
                 view: ConsoleView | None = None, decision: Decision | None = None) -> RunOutcome:
    """Continue a stopped run (crashed, interrupted, waiting) in its own folder. `decision` answers the writes waiting
    for your approval: "approve" lets them go ahead, "reject" fails those nodes (nothing is written)."""
    run_dir = run_dir_of(run, settings, runs_dir)
    req = RunRequest.model_validate_json((run_dir / "request.json").read_text())
    log = RunLog.reopen(run_dir, on_step=view.step if view else None)
    return await _drive(req, settings, llm, log, view, resumed=True, decision=decision)


async def _drive(req: RunRequest, settings: Settings, llm: Llm | None, log: RunLog, view: ConsoleView | None, *,
                 resumed: bool, decision: Decision | None) -> RunOutcome:
    if view:
        view.start(req)
    p = _Progress()
    try:
        await _graph(req, settings, llm, log, p, view, resumed, decision)
    except Exception as e:
        _stopped_early(log, p, "crashed", e)
    except BaseException as e:                       # Ctrl-C, or the task was cancelled
        _stopped_early(log, p, "interrupted", e)
        outcome = _finish(log, req, p, view, settings)
        # The MCP client wraps it in an exception group; raise the interrupt itself, so callers can catch it.
        stop = next((x for x in leaves(e) if isinstance(x, (KeyboardInterrupt, asyncio.CancelledError))), e)
        stop.add_note(f"The run's files were written before stopping: {outcome.run_dir}/report.md")
        if stop is e:
            raise
        raise stop from e
    return _finish(log, req, p, view, settings)


async def _graph(req: RunRequest, settings: Settings, llm: Llm | None, log: RunLog, p: _Progress,
                 view: ConsoleView | None, resumed: bool, decision: Decision | None) -> None:
    skills = SkillRegistry()
    llm = llm or make_llm(settings)
    route = llm if isinstance(llm, RoutedLlm) else None      # the validator picks another model from it
    if view:
        llm = TimedLlm(llm, view)
    llm = LimitedLlm(llm, settings.llm_concurrency)
    async with McpSession(settings, req.instance) as mcp:
        seat_tools = {t.name: t for t in await mcp.list_tools()}
        skills.validate_tools(set(seat_tools), set(LOCAL_TOOLS) | GRAPH_CAPABILITIES)
        p.where = "context"
        if resumed:                                   # the same facts as the first pass (today, mailboxes, who)
            ctx = RunContext.model_validate_json((log.dir / "context.json").read_text())
        else:
            ctx = await build_context(settings, mcp, req.instance, req.request, req.run_id, req.today, req.mailboxes,
                                      req.threads)
            log.start(ctx)
        if view:
            view.context(ctx, len(seat_tools), skills.names())
        p.where = "graph"
        store = RunStore(log.dir / RUN_FILE, crash_at=CrashPoint.from_env())
        mirror = MailboxStore.for_instance(settings.state_dir, req.instance)
        verdicts = VerdictCache.for_instance(settings.state_dir, req.instance) if req.cache else None
        memory = LongTermMemory.for_instance(settings.state_dir, req.instance)
        ours = EventStore.for_instance(settings.state_dir, req.instance)

        def on_write(rec: WriteRecord) -> None:
            log.record_write(rec)
            if not rec.dry_run and rec.row_id:            # the watcher must never answer a change we made (Stage 8)
                try:
                    ours.add_our_writes([OurWrite(row_id=rec.row_id, tool=rec.tool, at=rec.at, via="agent",
                                                  run_id=req.run_id)])
                except Exception:  # noqa: BLE001 — the write happened; its writes.jsonl line matters more
                    logging.getLogger(__name__).exception("could not add a write to the record of our writes")
        try:
            mail_search = memory_search = None
            if (req.search or "fts") == "hybrid":         # Stage 9: vectors are updated after each sync
                embedder = Embedder(settings)
                mail_search = MailSearch(mirror, embedder, settings.state_dir, req.instance)
                memory_search = MemorySearch(memory, embedder, settings.state_dir, req.instance)
            action = Action(mcp, ctx, Artifacts(log.dir), mirror, writes=None, full_sync=req.full_sync, memory=memory,
                            mail_search=mail_search, memory_search=memory_search)
            action.memory_vector_floor = settings.memory_vector_floor
            writes = WritePath(mcp, RestClient(settings, req.instance), ctx, mirror, on_write=on_write,
                               dry_run=req.dry_run, outbox=WriteOutbox(store), refresh=action.refresh)
            writes.created = {w.row_id for w in log.writes if w.row_id and w.tool.endswith(".create") and not w.dry_run}
            action.writes = writes
            caps = Capabilities(skills, seat_tools)
            planner: RunPlanner | None = None
            rt = GraphRuntime(ctx=ctx, store=store, mirror=mirror, action=action, writes=writes, llm=llm, log=log,
                              skills=skills, caps=caps, cache=verdicts, goals=lambda: planner.goals if planner else {},
                              route=route, validate=settings.validate_verdicts, approve_writes=req.approve_writes)
            # the history is fixed when the run starts (kept in run.sqlite), so a resumed run's first planner round is
            # the same request and its saved reply is reused
            history = store.context().get("history", []) if resumed else (
                [e.line() for e in memory.recent_episodes([m.id for m in ctx.mailboxes], req.run_id)]
                if req.history else [])
            rt.vector_candidates, rt.vector_margin = settings.search_vector_candidates, settings.search_vector_margin
            planner = RunPlanner(rt, caps, skills, ctx, history=history)
            if view and history and not resumed:
                view.note("memory", f"the planner sees the last {len(history)} run(s) on these mailboxes")
            action.on_sync = lambda report: log.step(SyncStep(iter=rt.round, report=report))
            for name, limit in (("planner_rounds", settings.max_planner_rounds), ("llm_calls", settings.max_llm_calls),
                                (NODE_BUDGET, settings.max_nodes)):
                store.set_budget(name, limit)          # on resume an existing budget keeps what it has spent
            executor = GraphExecutor(store, planner, rt.workers(), max_workers=settings.max_workers)
            try:
                if resumed and store.is_finished():
                    report = RunReport(run_id=req.run_id, finished=True, executed=[], waiting=[])
                    if view:
                        view.note("resume", "this run had already finished: nothing to do")
                elif resumed:
                    planner.restore()
                    p.where = "reconcile"
                    settled = await reconcile(store=store, mcp=mcp, ctx=ctx, log=log, dry_run=req.dry_run)
                    answered = _answer_approvals(store, decision)
                    if view:
                        view.note("resume", _resume_line(store, settled, answered, decision))
                    p.where = "graph"
                    report = await executor.run(resume=True)
                else:
                    report = await executor.run(run_id=req.run_id, context={"request": req.model_dump(mode="json"),
                                                                            "context": ctx.model_dump(mode="json"),
                                                                            "history": history})
                for _ in range(RECONCILE_PASSES):      # a write got no answer during this pass: settle it, go on
                    if not any(n.wait and n.wait.event_type == RECONCILE for n in store.waiting()):
                        break
                    p.where = "reconcile"
                    settled = await reconcile(store=store, mcp=mcp, ctx=ctx, log=log, dry_run=req.dry_run)
                    if view:
                        view.note("reconcile", _settled_text(settled))
                    if not settled.released:
                        break
                    p.where = "graph"
                    report = await executor.run(resume=True)
            finally:
                snap = store.snapshot()
                p.it, p.goals = rt.round, planner.final_goals(snap)
                p.nodes = sum(1 for n in snap.nodes.values() if n.state in ("succeeded", "failed"))
            ended = [g for g in p.goals if g.done]
            waiting = store.waiting()
            approvals = [n for n in waiting if n.wait and n.wait.event_type == APPROVAL]
            if report.finished and p.goals and len(ended) == len(p.goals):
                p.stopped = "done"
            elif planner.budget_hit:
                p.stopped, p.reason = "max_steps", f"Stopped: {planner.budget_hit}"
            elif approvals:
                n = sum(len(a.wait.metadata.get("writes") or []) for a in approvals if a.wait)
                p.stopped, p.reason = "waiting", (
                    f"Waiting for your approval of {n} write(s) ({', '.join(a.id for a in approvals)}); the list is in "
                    f"report.md. To send them: uv run python -m email_agent --resume {resume_arg(log.dir)} --approve "
                    f"(or --reject to write nothing).")
            elif waiting:
                p.stopped, p.reason = "waiting", (
                    f"Waiting on {', '.join(n.id for n in waiting)}: a write could not be checked against the live "
                    f"platform (see report.md). Check those rows, then: uv run python -m email_agent --resume {resume_arg(log.dir)}")
            else:
                last = next((e for e in reversed(store.events()) if e.kind == "graph_patched"), None)
                why = (last.payload.get("reason") if last else None) or "the planner stopped without finishing"
                p.stopped, p.reason = "error", f"Stopped before every goal ended: {why}"
        finally:
            if "action" in locals():
                await action.settle()
            store.close()
            mirror.close()
            memory.close()
            ours.close()
            if verdicts:
                verdicts.close()
        p.where = "finish"                                # closing the MCP session comes next


def _answer_approvals(store: RunStore, decision: Decision | None) -> list[str]:
    """Your answer to the writes waiting for approval. Each handle is single use (S17 `complete_waiting`)."""
    answered = []
    for node in store.waiting():
        if node.wait is None or node.wait.event_type != APPROVAL or decision is None:
            continue
        if decision == "approve":
            store.release_waiting(node.wait.handle, APPROVAL, note="approved by you (--approve)")
        else:
            store.complete_waiting(node.wait.handle, APPROVAL, {"error": "you declined these writes (--reject); "
                                                                         "nothing was written"}, success=False)
        answered.append(node.id)
    return answered


def _settled_text(r: ReconcileReport) -> str:
    counts: dict[str, int] = {}
    for item in r.items:
        counts[item.verdict] = counts.get(item.verdict, 0) + 1
    parts = [f"{v} {k}" for k, v in counts.items()] or ["no uncertain write"]
    if r.backfilled:
        parts.append(f"{r.backfilled} completed write(s) added to writes.jsonl")
    if r.released:
        parts.append(f"{len(r.released)} node(s) run again")
    return "reconcile: " + ", ".join(parts)


def _resume_line(store: RunStore, settled: ReconcileReport, answered: list[str], decision: Decision | None) -> str:
    running = [n.id for n in store.snapshot().nodes.values() if n.state == "running"]
    text = f"resume #{store.resumes() + 1}: {len(running)} node(s) that were running run again · {_settled_text(settled)}"
    if answered:
        text += f" · {'approved' if decision == 'approve' else 'declined'}: {', '.join(answered)}"
    return text


def _stopped_early(log: RunLog, p: _Progress, stopped: Stopped, e: BaseException) -> None:
    p.error = run_error(e, p.where, p.it)
    log.step(ErrorStep(iter=p.it, error=p.error))
    what = f"{p.error.type}: {p.error.message}" if p.error.message else p.error.type
    if p.where == "finish" and p.stopped == "done":   # every goal was answered; only closing the session failed
        p.reason = f"Finished, but closing the MCP session failed: {what}"
        return
    p.stopped = stopped
    p.reason = f"{'Crashed' if stopped == 'crashed' else 'Interrupted'} in {p.where} at step {p.it}: {what}"
    if (log.dir / RUN_FILE).exists():
        p.reason += f" To continue it: uv run python -m email_agent --resume {resume_arg(log.dir)}"


def _save_episode(log: RunLog, req: RunRequest, p: _Progress, settings: Settings) -> None:
    """This run in a few words, for the next runs' planner (memory layer 7). Replaced when the run is resumed. A
    failure here is logged, never raised: the run's own files matter more."""
    try:
        path = log.dir / "context.json"
        ctx = RunContext.model_validate_json(path.read_text()) if path.exists() else None
        writes: dict[str, int] = {}
        for w in log.writes:
            writes[w.tool] = writes.get(w.tool, 0) + 1
        ep = Episode(run_id=req.run_id, day=(ctx.today if ctx else (req.today or req.started_at.date())).isoformat(),
                     request=req.request, stopped=p.stopped, dry_run=req.dry_run,
                     mailboxes=[m.email for m in ctx.mailboxes] if ctx else [],
                     goals=[EpisodeGoal(text=g.text, skill=g.skill,
                                        outcome="refused" if g.refused else "answered" if g.done else "open",
                                        reason=g.refusal if g.refused else None) for g in p.goals],
                     writes=writes)
        memory = LongTermMemory.for_instance(settings.state_dir, req.instance)
        try:
            memory.save_episode(ep, [m.id for m in ctx.mailboxes] if ctx else [], ctx.me.email if ctx else "agent")
        finally:
            memory.close()
    except Exception:  # noqa: BLE001
        logging.getLogger(__name__).exception("could not save the episode of %s", req.run_id)


def _finish(log: RunLog, req: RunRequest, p: _Progress, view: ConsoleView | None, settings: Settings) -> RunOutcome:
    usage, served = Usage(), dict[str, int]()
    for step in RunLog.llm_steps(log.dir):
        if step.reply and not step.reply.reused:      # a reply reused on resume cost nothing the second time
            usage.input_tokens += step.reply.usage.input_tokens
            usage.output_tokens += step.reply.usage.output_tokens
            usage.thinking_tokens += step.reply.usage.thinking_tokens
            who = served_label(step.reply)
            served[who] = served.get(who, 0) + 1
    final = _final(p.goals, p.stopped, p.reason, p.error)
    log.write("final.json", final)
    outcome = RunOutcome(run_id=req.run_id, run_dir=str(log.dir), instance=req.instance, final=final,
                         writes=log.writes, usage=usage, iterations=p.nodes, provider=req.provider, model=req.model,
                         route=req.route, served_by=served,
                         dry_run=req.dry_run, started_at=req.started_at, ended_at=datetime.now(timezone.utc))
    log.write("outcome.json", outcome)
    _save_episode(log, req, p, settings)
    if (log.dir / RUN_FILE).exists():
        at_run_end(log.dir)                       # spans.jsonl (+ OTLP when configured): Stage 10, never raises
    report = write_report(log.dir)
    page = page_at_run_end(log.dir)               # view.html, the run's web page (Revision 15), never raises
    if view:
        view.finish(outcome, report, page)
    return outcome
