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
from rich.status import Status

from email_agent.contracts.agent import RunContext, RunOutcome, RunRequest
from email_agent.contracts.llm import LlmReply, LlmRequest
from email_agent.contracts.runlog import (
    ActionStep,
    ErrorStep,
    LlmStep,
    NodeStep,
    PlanStep,
    SyncStep,
)

if TYPE_CHECKING:
    from email_agent.llm.route import Llm

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


class ConsoleView:
    def __init__(self, console: Console | None = None, verbose: bool = False, prefix: str = ""):
        self.console = console or Console(stderr=True, highlight=False)
        self.verbose = verbose
        self.prefix = prefix                      # the harness puts the task id here
        self._llm: LlmStep | None = None          # the LLM call whose numbers go on the next planner line
        self._mark = time.monotonic()             # when the last line was printed (action timing)
        self._waits: dict[object, tuple[str, float]] = {}   # model calls in flight (label, start)
        self._status: Status | None = None
        self._ticker: asyncio.Task[None] | None = None

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

    def note(self, layer: str, text: str, style: str = "") -> None:
        """A line that is not a step (resume, reconcile, approvals)."""
        self._line(None, layer, escape(text), style)

    def finish(self, outcome: RunOutcome, report: Path) -> None:
        f, u = outcome.final, outcome.usage
        style = {"done": "green", "crashed": "red", "interrupted": "yellow", "waiting": "cyan"}.get(f.stopped, "yellow")
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
            if step.layer == "critic" and step.valid:        # the evidence critic and the verifier: a quiet line
                if step.error:
                    style = "yellow" if step.error.startswith("not ready") else "dim"
                    self._line(step.iter, "critic", f"{escape(step.node_id or '')}: {escape(step.error)}", style)
                return
            if step.layer in ("judge", "answer", "validator"):  # the node line carries the outcome
                if self.verbose and step.reply:
                    u = step.reply.usage
                    self._line(None, step.layer, f"[dim]{step.node_id}: {step.reply.elapsed_ms / 1000:.1f}s · "
                               f"{_k(u.input_tokens)} in / {_k(u.output_tokens)} out[/dim]")
                return
            self._llm = step
            if step.error:                                   # e.g. "model proposed 10 calls; the first is used"
                self._line(step.iter, step.layer, f"⚠ {escape(step.error)}", "yellow")
        elif isinstance(step, ActionStep):
            self._action(step)
        elif isinstance(step, PlanStep):
            for g in step.goals:
                skill = f"⇒ {g.skill}" if g.skill else f"⇒ [yellow]no skill ({g.refusal}): refused[/yellow]"
                self._line(None, "goal", f"{g.id} \"{escape(_short(g.text, 90))}\" {skill}")
            for r in step.rejected:
                self._line(step.iter, "planner", f"⚠ proposal rejected: {escape(_short(r, 200))} — asking again",
                           "yellow")
            if step.added or step.cancelled:
                what = "; ".join(escape(_short(a, 90)) for a in step.added)
                self._line(step.iter, "planner", f"+ {what}" + (f" · cancel {step.cancelled}" if step.cancelled else "")
                           + self._llm_numbers())
            elif not step.goals:
                self._line(step.iter, "planner", f"[dim]{escape(_short(step.reason or 'nothing new', 120))}[/dim]"
                           + self._llm_numbers())
        elif isinstance(step, NodeStep):
            style = {"failed": "red", "waiting": "yellow"}.get(step.state, "")
            mark = {"succeeded": "✓", "failed": "✗", "waiting": "⏸", "fanned_out": "⇉", "cancelled": "–"}[step.state]
            self._line(None, "node", f"{mark} {escape(step.node_id)} {escape('[' + step.capability + ']')} {escape(_short(step.summary, 140))}"
                       f"   [dim]{step.seconds:.1f}s[/dim]", style)
        elif isinstance(step, SyncStep):
            rep = step.report
            how = "full" if any(x.full for x in rep.tables) else "incremental"
            self._line(step.iter, "sync", f"local mailbox copy, {how}: {rep.calls} calls, {rep.seconds:.1f}s, "
                       f"{sum(x.changed for x in rep.tables)} rows changed, {rep.facts_recomputed} re-worked "
                       f"({rep.threads_total} conversations / {rep.messages_total} messages)"
                       + (f", {rep.embedded} message(s) embedded" if rep.embedded is not None else ""))
            if rep.search_note:
                self._line(step.iter, "sync", f"⚠ {escape(rep.search_note)}", "yellow")
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
        # Graph nodes call models in parallel, and the terminal can show only one live line: the first call opens
        # it, later ones join it ("3 calls: judge, judge, planner"), the last one to finish closes it.
        token = object()
        self._waits[token] = (label, time.monotonic())
        if len(self._waits) == 1:
            self._status = self.console.status(self._wait_text())
            self._status.__enter__()

            async def tick() -> None:
                while True:
                    await asyncio.sleep(1)
                    if self._status:
                        self._status.update(self._wait_text())
            self._ticker = asyncio.create_task(tick())
        try:
            yield
        finally:
            self._waits.pop(token, None)
            if not self._waits and self._status:
                if self._ticker is not None:
                    self._ticker.cancel()
                    self._ticker = None
                self._status.__exit__(None, None, None)
                self._status = None

    def _wait_text(self) -> str:
        items = list(self._waits.values())
        oldest = min(t for _, t in items)
        what = items[0][0] if len(items) == 1 else f"{len(items)} model calls ({', '.join(label.split(':')[0] for label, _ in items)})"
        return f"{escape(self.prefix)}{what}… {time.monotonic() - oldest:.0f}s"


class TimedLlm:
    """Wraps any Llm so the view shows a live waiting line during each call. Same interface."""

    def __init__(self, llm: Llm, view: ConsoleView):
        self.llm, self.view, self.model = llm, view, llm.model

    async def chat(self, request: LlmRequest) -> LlmReply:
        async with self.view.waiting(f"{request.purpose}: waiting for {escape(self.model)}"):
            return await self.llm.chat(request)
