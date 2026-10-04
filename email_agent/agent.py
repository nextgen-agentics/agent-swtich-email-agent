"""The agent loop, S7 layout:

    request.json ─► context.build ─► each iteration (≤ max_steps):
        memory.read ─► perception.observe ─► decision.next_step ─► action.execute ─► memory.record
    (Perception runs at the start and after each answered goal — the only times the goal list can change.)
    ─► FinalAnswer ─► runs/<run_id>/

Perception names a skill per goal; Decision sees only that skill's instructions and tools;
Action validates, guards and records. Every hand-off is a pydantic model (contracts/agent.py).

Every exit writes final.json, outcome.json and report.md: a normal finish, a crash (stopped="crashed",
the run returns normally) and Ctrl-C (stopped="interrupted", the files are written, then it is re-raised).
"""

from __future__ import annotations

import asyncio
import secrets
from dataclasses import dataclass, field
from datetime import date, datetime, timezone
from pathlib import Path

from email_agent import decision, perception
from email_agent.action import ALWAYS_OFFERED, LOCAL_TOOLS, Action, tool_specs
from email_agent.artifacts import Artifacts
from email_agent.config import Settings
from email_agent.console import ConsoleView, TimedLlm
from email_agent.context import build_context
from email_agent.contracts.agent import (FinalAnswer, Goal, Observation, RefuseInput, RunError, RunOutcome, RunRequest,
                                         Stopped, Where)
from email_agent.contracts.llm import LlmReply, Usage
from email_agent.contracts.runlog import ActionStep, ErrorStep, GoalsStep, MemoryStep
from email_agent.errors import leaves, run_error
from email_agent.llm import Llm, make_llm, plan_route, route_text
from email_agent.mcp_session import McpSession
from email_agent.memory import RunMemory
from email_agent.run_report import write_report
from email_agent.runlog import RunLog
from email_agent.skill_registry import SkillRegistry

NO_SKILL = ("I can't do this one: none of my skills covers it, or it needs data this mailbox is not allowed to "
            "see. It would need another team's agent, an administrator, or a person.")


@dataclass
class _Progress:
    """How far the run got — what the crash handler and _finish need when the loop stops early."""

    goals: list[Goal] = field(default_factory=list)
    it: int = 0
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


async def run(request: str, instance: str, settings: Settings, *, today: date | None = None,
              llm: Llm | None = None, runs_dir: Path | None = None, dry_run: bool = False,
              mailboxes: list[str] | None = None, view: ConsoleView | None = None) -> RunOutcome:
    """`mailboxes`: addresses to work in (None = the instance default in config.INSTANCES).
    `view`: prints each step to the terminal as it happens (None = quiet)."""
    run_id = f"{instance}-{datetime.now(timezone.utc):%Y%m%dT%H%M%SZ}-{secrets.token_hex(2)}"
    log = RunLog((runs_dir or settings.runs_dir) / run_id, on_step=view.step if view else None)
    planned = plan_route(settings)
    req = RunRequest(run_id=run_id, request=request, instance=instance, provider=settings.provider,
                     model=llm.model if llm else (planned[0][1] if planned else "none"),
                     route=f"{llm.model} (given)" if llm else route_text(settings),
                     dry_run=dry_run, today=today, mailboxes=mailboxes)
    log.begin(req)
    if view:
        view.start(req)
    p = _Progress()
    try:
        await _loop(req, settings, llm, log, p, view)
    except Exception as e:
        _stopped_early(log, p, "crashed", e)
    except BaseException as e:                       # Ctrl-C, or the task was cancelled
        _stopped_early(log, p, "interrupted", e)
        outcome = _finish(log, req, p, view)
        # The MCP client wraps it in an exception group; raise the interrupt itself, so callers can catch it.
        stop = next((x for x in leaves(e) if isinstance(x, (KeyboardInterrupt, asyncio.CancelledError))), e)
        stop.add_note(f"The run's files were written before stopping: {outcome.run_dir}/report.md")
        if stop is e:
            raise
        raise stop from e
    return _finish(log, req, p, view)


async def _loop(req: RunRequest, settings: Settings, llm: Llm | None, log: RunLog, p: _Progress,
                view: ConsoleView | None) -> None:
    registry = SkillRegistry()
    llm = llm or make_llm(settings)
    if view:
        llm = TimedLlm(llm, view)
    async with McpSession(settings, req.instance) as mcp:
        seat_tools = {t.name: t for t in await mcp.list_tools()}
        registry.validate_tools(set(seat_tools), set(LOCAL_TOOLS))
        p.where = "context"
        ctx = await build_context(settings, mcp, req.instance, req.request, req.run_id, req.today, req.mailboxes)
        log.start(ctx)
        if view:
            view.context(ctx, len(seat_tools), registry.names())
        memory = RunMemory()
        action = Action(mcp, ctx, Artifacts(log.dir), on_write=log.record_write, dry_run=req.dry_run)

        replan = True                                     # the goal list can only change at the start and
        for p.it in range(1, settings.max_steps + 1):     # after a goal is answered, so Perception runs then
            if replan:
                p.where = "perception"
                obs = await perception.observe(llm, ctx, registry, p.goals, memory.read(), log, p.it)
                if obs is None:
                    p.stopped, p.reason = "error", "Perception did not return a valid goal list."
                    break
                p.goals, replan = obs.goals, False
                log.step(GoalsStep(iter=p.it, goals=p.goals))
            goal = Observation(goals=p.goals).next_unfinished()
            if goal is None:
                p.stopped = "done"
                break
            if goal.skill is None:                         # no skill fits: say so, change nothing
                goal.done, goal.answer, replan = True, NO_SKILL, True
                goal.refused, goal.refusal = True, goal.refusal or "out_of_seat"
                log.step(MemoryStep(iter=p.it, recorded=memory.record_answer(p.it, goal, NO_SKILL)))
                continue
            skill = registry.get(goal.skill)
            offered = skill.tools + [t for t in ALWAYS_OFFERED if t not in skill.tools]
            p.where = "decision"
            out = await decision.next_step(llm, ctx, goal, skill, tool_specs(offered, seat_tools),
                                           memory.read(goal), log, p.it)
            if out is None:
                p.stopped, p.reason = "error", f"Decision did not return a valid step for goal {goal.id}."
                break
            if out.answer is not None:
                goal.done, goal.answer, replan = True, out.answer, True
                log.step(MemoryStep(iter=p.it, recorded=memory.record_answer(p.it, goal, out.answer)))
                continue
            p.where = "action"
            result = await action.execute(out.tool_call, offered)
            log.step(ActionStep(iter=p.it, goal_id=goal.id, result=result))
            if result.ok and result.tool == "refuse":       # the skill declined: the goal ends here, as data
                r = RefuseInput.model_validate(result.arguments)
                goal.done, goal.refused, goal.refusal, goal.answer, replan = True, True, r.reason, r.explanation, True
                log.step(MemoryStep(iter=p.it, recorded=memory.record_answer(p.it, goal, r.explanation)))
                continue
            log.step(MemoryStep(iter=p.it, recorded=memory.record_action(p.it, goal, result)))
        else:
            p.reason = f"Stopped after {settings.max_steps} steps."
        if p.goals and all(g.done for g in p.goals):
            p.stopped = "done"
        p.where = "finish"                                # closing the MCP session comes next


def _stopped_early(log: RunLog, p: _Progress, stopped: Stopped, e: BaseException) -> None:
    p.error = run_error(e, p.where, p.it)
    log.step(ErrorStep(iter=p.it, error=p.error))
    what = f"{p.error.type}: {p.error.message}" if p.error.message else p.error.type
    if p.where == "finish" and p.stopped == "done":   # every goal was answered; only closing the session failed
        p.reason = f"Finished, but closing the MCP session failed: {what}"
        return
    p.stopped = stopped
    p.reason = f"{'Crashed' if stopped == 'crashed' else 'Interrupted'} in {p.where} at step {p.it}: {what}"


def _finish(log: RunLog, req: RunRequest, p: _Progress, view: ConsoleView | None) -> RunOutcome:
    usage, served = Usage(), {}
    for step in RunLog.llm_steps(log.dir):
        if step.reply:
            usage.input_tokens += step.reply.usage.input_tokens
            usage.output_tokens += step.reply.usage.output_tokens
            usage.thinking_tokens += step.reply.usage.thinking_tokens
            who = served_label(step.reply)
            served[who] = served.get(who, 0) + 1
    final = _final(p.goals, p.stopped, p.reason, p.error)
    log.write("final.json", final)
    outcome = RunOutcome(run_id=req.run_id, run_dir=str(log.dir), instance=req.instance, final=final,
                         writes=log.writes, usage=usage, iterations=p.it, provider=req.provider, model=req.model,
                         route=req.route, served_by=served,
                         dry_run=req.dry_run, started_at=req.started_at, ended_at=datetime.now(timezone.utc))
    log.write("outcome.json", outcome)
    report = write_report(log.dir)
    if view:
        view.finish(outcome, report)
    return outcome
