"""What the agent is doing, printed live to the terminal (stderr; the answer stays alone on stdout).

The view is fed by RunLog's `on_step`, so it shows exactly the steps written to steps.jsonl.
TimedLlm adds a live "waiting for <model>… 14s" line while a model call is in flight (only on a
real terminal), and `setup_logging` routes the LLM clients' retry warnings ("rate limited,
waiting 31s") to the same console. Library use stays quiet: agent.run prints only when given a view.

Never prints a setting's value — only provider, model and mailbox addresses (the .env rule).
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from contextlib import asynccontextmanager
from pathlib import Path
from typing import TYPE_CHECKING, Any, AsyncIterator

from rich.console import Console
from rich.logging import RichHandler
from rich.markup import escape

from email_agent.contracts.agent import RunContext, RunOutcome, RunRequest
from email_agent.contracts.llm import LlmReply, LlmRequest
from email_agent.contracts.runlog import ActionStep, DecisionStep, ErrorStep, GoalsStep, LlmStep, MemoryStep

if TYPE_CHECKING:
    from email_agent.llm import Llm

LAYER_WIDTH = 11


def setup_logging(console: Console, verbose: bool = False, quiet: bool = False) -> None:
    """Warnings from our own modules (LLM retries, waits) go to the console; library chatter stays off."""
    handler = RichHandler(console=console, show_time=False, show_path=False, show_level=False, markup=False,
                          rich_tracebacks=False)
    logging.basicConfig(level=logging.WARNING, format="%(message)s", handlers=[handler], force=True)
    logging.getLogger("email_agent").setLevel(logging.ERROR if quiet else logging.INFO if verbose else logging.WARNING)


def _k(n: int) -> str:
    return f"{n / 1000:.1f}k" if n >= 1000 else str(n)


def _size(chars: int) -> str:
    return f"{chars / 1000:.1f} KB" if chars >= 1000 else f"{chars} chars"


def _short(value: Any, limit: int = 40) -> str:
    text = value if isinstance(value, str) else json.dumps(value, ensure_ascii=False, default=str)
    if len(text) == 36 and text.count("-") == 4:          # a uuid: the first 8 characters are enough to follow
        return text[:8] + "…"
    return text if len(text) <= limit else text[:limit] + "…"


def _args(arguments: dict[str, Any], verbose: bool) -> str:
    if verbose:
        return json.dumps(arguments, ensure_ascii=False, default=str)
    return ", ".join(f"{k}={_short(v)}" for k, v in arguments.items())


class ConsoleView:
    def __init__(self, console: Console | None = None, verbose: bool = False, prefix: str = ""):
        self.console = console or Console(stderr=True, highlight=False)
        self.verbose = verbose
        self.prefix = prefix                      # the harness puts the task id here
        self._llm: LlmStep | None = None          # the LLM call whose numbers go on the next decision/goals line
        self._mark = time.monotonic()             # when the last line was printed (action timing)

    # ── output ───────────────────────────────────────────────────────────────
    def _line(self, it: int | None, layer: str, text: str, style: str = "") -> None:
        step = f"[{it}]" if it is not None else "   "
        body = f"{escape(self.prefix)}[dim]{step:>4}[/dim] [bold]{layer:<{LAYER_WIDTH}}[/bold] {text}"
        self.console.print(f"[{style}]{body}[/{style}]" if style else body, soft_wrap=True)
        self._mark = time.monotonic()

    def _llm_numbers(self) -> str:
        s, self._llm = self._llm, None
        if not s or not s.reply:
            return ""
        u = s.reply.usage
        think = f" · {_k(u.thinking_tokens)} thinking" if u.thinking_tokens else ""
        size = f" · prompt {_size(sum(len(m.text) for m in s.request.messages))}" if self.verbose else ""
        via = ""
        if s.reply.fallback_from:                    # not the first option: say who answered
            via = f" · [yellow]via {s.reply.provider}/{escape(s.reply.model)}" + \
                  (f" key #{s.reply.key_slot}" if s.reply.key_slot else "") + "[/yellow]"
        return (f"   [dim]{s.reply.elapsed_ms / 1000:.1f}s · {_k(u.input_tokens)} in / {_k(u.output_tokens)} out"
                f"{think}{size}[/dim]{via}")

    # ── run start / end ──────────────────────────────────────────────────────
    def start(self, req: RunRequest) -> None:
        dry = " · [yellow]DRY RUN[/yellow]" if req.dry_run else " · [red]LIVE (writes are sent)[/red]"
        today = f" · today {req.today.isoformat()}" if req.today else ""
        self.console.print(f"{escape(self.prefix)}[bold cyan]▶ {req.run_id}[/bold cyan] · {req.instance}{dry}{today}",
                           soft_wrap=True)
        self.console.print(f"{escape(self.prefix)}     [dim]route: {escape(req.route or req.provider + '/' + req.model)}"
                           "[/dim]", soft_wrap=True)

    def context(self, ctx: RunContext, seat_tools: int, skills: list[str]) -> None:
        mailboxes = ", ".join(m.email for m in ctx.mailboxes)
        self._line(None, "context", f"me {escape(ctx.me.email or ctx.me.name or '?')} · "
                   f"{ctx.locale.country} / {ctx.locale.base_currency} · today {ctx.today.isoformat()} · "
                   f"{mailboxes} · {seat_tools} seat tools · skills: {', '.join(skills)}")

    def finish(self, outcome: RunOutcome, report: Path) -> None:
        f, u = outcome.final, outcome.usage
        style = {"done": "green", "crashed": "red", "interrupted": "yellow"}.get(f.stopped, "yellow")
        writes = len(outcome.writes)
        dry = " (dry run)" if outcome.dry_run and writes else ""
        self.console.print(f"{escape(self.prefix)}[bold {style}]■ {f.stopped}[/bold {style}] · {outcome.iterations} "
                           f"iteration(s) · {writes} write(s){dry} · {_k(u.input_tokens)} in / {_k(u.output_tokens)} "
                           f"out / {_k(u.thinking_tokens)} thinking · {escape(str(report))}", soft_wrap=True)
        if len(outcome.served_by) > 1 or any("key #" in k and not k.endswith("#1") for k in outcome.served_by):
            served = ", ".join(f"{escape(k)} ×{n}" for k, n in outcome.served_by.items())
            self.console.print(f"{escape(self.prefix)}     [dim]served by: {served}[/dim]", soft_wrap=True)

    # ── one step ─────────────────────────────────────────────────────────────
    def step(self, step: Any) -> None:
        if isinstance(step, LlmStep):
            if not step.valid:
                self._line(step.iter, step.layer, f"⚠ reply rejected: {escape(_short(step.error or '', 160))} — "
                           "asking again", "yellow")
                return
            self._llm = step
            if step.error:                                   # e.g. "model proposed 10 calls; the first is used"
                self._line(step.iter, step.layer, f"⚠ {escape(step.error)}", "yellow")
        elif isinstance(step, GoalsStep):
            self._line(step.iter, "perception", f"{len(step.goals)} goal(s){self._llm_numbers()}")
            for g in step.goals:
                skill = f"⇒ {g.skill}" if g.skill else "⇒ [yellow]no skill fits (will refuse)[/yellow]"
                done = " ✓" if g.done else ""
                self._line(None, "", f"{g.id} \"{escape(_short(g.text, 90))}\" {skill}{done}")
        elif isinstance(step, DecisionStep):
            out = step.output
            if out.answer is not None:
                what = f"{step.goal_id} → [green]ANSWER[/green] ({len(out.answer):,} characters)"
            else:
                what = f"{step.goal_id} → {out.tool_call.name}({escape(_args(out.tool_call.arguments, self.verbose))})"
            self._line(step.iter, "decision", what + self._llm_numbers())
        elif isinstance(step, ActionStep):
            self._action(step)
        elif isinstance(step, MemoryStep) and self.verbose:
            h = step.recorded
            self._line(step.iter, "memory", f"[dim]+ [{h.goal_id}] {h.kind}{f' {h.tool}' if h.tool else ''}: "
                       f"{escape(_short(h.text, 120))}[/dim]")
        elif isinstance(step, ErrorStep):
            e = step.error
            what = f": {escape(_short(e.message, 300))}" if e.message else ""
            self._line(step.iter, "error", f"✗ {e.type} in {e.where}{what}", "bold red")

    def _action(self, step: ActionStep) -> None:
        r, secs = step.result, f"   [dim]{time.monotonic() - self._mark:.1f}s[/dim]"
        if not r.ok:
            self._line(None, "action", f"✗ {r.kind}: {escape(_short(r.message, 200))} (not sent){secs}", "red")
            return
        if r.writes:                                         # a batch tool: one write per row
            dry = sum(1 for w in r.writes if w.dry_run)
            sent = f"[yellow]{dry} dry run, not sent[/yellow]" if dry else "[green]sent[/green]"
            failed = "" if r.preview.find('"failed": 0') >= 0 else " · [red]some rows failed (see result)[/red]"
            self._line(None, "action", f"✎ WRITE {r.tool}: {len(r.writes)} row(s) ({sent}){failed}{secs}")
            if self.verbose:
                for w in r.writes:
                    fields = ", ".join(f"{k}={_short(v)}" for k, v in w.fields.items())
                    self._line(None, "", f"[dim]{w.tool} {_short(w.row_id or 'new')} {escape(fields)}[/dim]")
            return
        if r.write:
            w = r.write
            fields = ", ".join(f"{k}={_short(v)}" for k, v in w.fields.items())
            sent = "[yellow]dry run, not sent[/yellow]" if w.dry_run else "[green]sent[/green]"
            self._line(None, "action", f"✎ WRITE {r.tool} {_short(w.row_id or '?')} {escape(fields)} ({sent}){secs}")
            return
        rows = f" → {r.rows} row(s)" if r.rows is not None else ""
        art = f" · saved as {r.artifact_id}" if r.artifact_id else ""
        self._line(None, "action", f"✓ {r.tool}{rows} · {_size(len(r.preview))}{art}{secs}")
        if self.verbose:
            self._line(None, "", f"[dim]{escape(_short(r.preview, 300))}[/dim]")

    # ── waiting for a model ──────────────────────────────────────────────────
    @asynccontextmanager
    async def waiting(self, label: str) -> AsyncIterator[None]:
        """A live line with the seconds so far, while a model call is in flight (terminal only)."""
        if not self.console.is_terminal:
            yield
            return
        started = time.monotonic()
        with self.console.status(f"{escape(self.prefix)}{label}… 0s") as status:
            async def tick() -> None:
                while True:
                    await asyncio.sleep(1)
                    status.update(f"{escape(self.prefix)}{label}… {time.monotonic() - started:.0f}s")
            ticker = asyncio.create_task(tick())
            try:
                yield
            finally:
                ticker.cancel()


class TimedLlm:
    """Wraps any Llm so the view shows a live waiting line during each call. Same interface."""

    def __init__(self, llm: Llm, view: ConsoleView):
        self.llm, self.view, self.model = llm, view, llm.model

    async def chat(self, request: LlmRequest) -> LlmReply:
        async with self.view.waiting(f"{request.purpose}: waiting for {escape(self.model)}"):
            return await self.llm.chat(request)
