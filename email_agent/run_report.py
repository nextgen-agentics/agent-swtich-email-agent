"""A readable report of one run: runs/<run_id>/report.md.

Built only from the files in the run folder (request.json, context.json, steps.jsonl,
writes.jsonl, final.json, outcome.json), so it works for any run — finished, crashed,
interrupted, or one killed so hard that no final.json was written. agent.run writes it at
the end of every run; for older runs:

    uv run python -m email_agent.run_report runs/<run_id> [runs/<run_id> …]
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any

from pydantic import BaseModel, ValidationError

from email_agent.contracts.agent import FinalAnswer, RunContext, RunOutcome, RunRequest, WriteRecord
from email_agent.contracts.runlog import (ActionStep, DecisionStep, ErrorStep, GoalsStep, LlmStep, MemoryStep,
                                          RunSummary, StepLine)

ANSWER_CHARS = 300


def _load(path: Path, model: type[BaseModel]) -> Any:
    if not path.exists():
        return None
    try:
        return model.model_validate_json(path.read_text())
    except ValidationError:
        return None


def build_summary(run_dir: Path) -> RunSummary:
    steps, bad = [], 0
    path = run_dir / "steps.jsonl"
    for line in (path.read_text().splitlines() if path.exists() else []):
        if not line.strip():
            continue
        try:
            steps.append(StepLine.model_validate({"step": json.loads(line)}).step)
        except (ValidationError, json.JSONDecodeError):
            bad += 1
    writes_path = run_dir / "writes.jsonl"
    writes = [WriteRecord.model_validate_json(x) for x in
              (writes_path.read_text().splitlines() if writes_path.exists() else []) if x.strip()]
    return RunSummary(run_id=run_dir.name, run_dir=str(run_dir), request=_load(run_dir / "request.json", RunRequest),
                      context=_load(run_dir / "context.json", RunContext), steps=steps, unreadable_lines=bad,
                      writes=writes, final=_load(run_dir / "final.json", FinalAnswer),
                      outcome=_load(run_dir / "outcome.json", RunOutcome))


# ── rendering ────────────────────────────────────────────────────────────────

def _cell(value: Any, limit: int = 160) -> str:
    text = value if isinstance(value, str) else json.dumps(value, ensure_ascii=False, default=str)
    text = " ".join(text.split())
    if len(text) > limit:
        text = text[:limit] + " …"
    return text.replace("|", "\\|") or "—"


def _k(n: int) -> str:
    return f"{n / 1000:.1f}k" if n >= 1000 else str(n)


def _step_row(step: Any) -> tuple[str, str, str] | None:
    """(layer, what happened, result) for one step; None for steps the table leaves out."""
    if isinstance(step, LlmStep):
        u = step.reply.usage if step.reply else None
        tokens = f"{_k(u.input_tokens)} in · {_k(u.output_tokens)} out · {_k(u.thinking_tokens)} thinking" if u else "no reply"
        if step.reply and step.reply.provider:
            tokens += f" · {step.reply.provider}/{step.reply.model}" + \
                      (f" key #{step.reply.key_slot}" if step.reply.key_slot else "")
            if step.reply.fallback_from:
                tokens += f" (after: {'; '.join(step.reply.fallback_from)})"
        secs = f"{step.reply.elapsed_ms / 1000:.1f}s" if step.reply else "—"
        if not step.valid:
            return f"{step.layer} (LLM)", f"reply rejected, {secs}", _cell(step.error or "", 200)
        return f"{step.layer} (LLM)", f"{secs} · {tokens}", _cell(step.error or "ok", 120)
    if isinstance(step, GoalsStep):
        goals = "; ".join(f"{g.id} → {g.skill or 'no skill'}{' ✓' if g.done else ''}" for g in step.goals)
        return "goals", f"{len(step.goals)} goal(s)", _cell(goals)
    if isinstance(step, DecisionStep):
        if step.output.answer is not None:
            return "decision", f"{step.goal_id}: answer", f"{len(step.output.answer)} characters"
        call = step.output.tool_call
        return "decision", f"{step.goal_id}: call `{call.name}`", _cell(call.arguments, 120)
    if isinstance(step, ActionStep):
        r = step.result
        if not r.ok:
            return "action", f"`{r.tool}`", f"✗ {r.kind}: {_cell(r.message, 200)}"
        if r.writes:
            sent = "dry run, not sent" if all(w.dry_run for w in r.writes) else "sent"
            return "action", f"`{r.tool}` ✎ {len(r.writes)} writes", f"{_cell(r.preview, 160)} ({sent})"
        if r.write:
            sent = "dry run, not sent" if r.write.dry_run else "sent"
            return "action", f"`{r.tool}` ✎ write", f"{_cell(r.write.fields, 120)} ({sent})"
        rows = f"{r.rows} rows · " if r.rows is not None else ""
        art = f" · saved as {r.artifact_id}" if r.artifact_id else ""
        return "action", f"`{r.tool}`", f"✓ {rows}{len(r.preview):,} characters{art}"
    if isinstance(step, ErrorStep):
        return "**error**", f"in {step.error.where}", f"✗ {step.error.type}: {_cell(step.error.message, 200)}"
    if isinstance(step, MemoryStep):
        return None
    return None


def render(s: RunSummary) -> str:
    req, ctx, final, out = s.request, s.context, s.final, s.outcome
    model = (f"{req.provider}/{req.model}" if req else
             next((f"{st.reply.model}" for st in s.steps if isinstance(st, LlmStep) and st.reply), "unknown"))
    lines = [f"# Run report — {s.run_id}", "",
             "| | |", "|---|---|",
             f"| Request | {_cell(req.request if req else (ctx.request if ctx else 'unknown'), 300)} |",
             f"| Instance | {(req.instance if req else ctx.instance if ctx else '—')} |",
             f"| Mailboxes | {', '.join(m.email for m in ctx.mailboxes) if ctx else '— (the run stopped before it read them)'} |",
             f"| Model | {model} |",
             f"| Route | {_cell(req.route or '—', 300) if req else '—'} |",
             f"| Served by | {', '.join(f'{k} ×{n}' for k, n in out.served_by.items()) if out and out.served_by else '—'} |",
             f"| Today | {ctx.today.isoformat() if ctx else (req.today.isoformat() if req and req.today else '—')} |",
             f"| Dry run | {('yes — writes were recorded, not sent' if req.dry_run else 'no') if req else 'unknown'} |",
             f"| Started → ended (UTC) | {req.started_at:%Y-%m-%d %H:%M:%S} → "
             f"{f'{out.ended_at:%H:%M:%S}' if out and out.ended_at else '—'} |" if req else "| Started | — |",
             ""]

    lines += ["## How it stopped", ""]
    if final is None:
        last = s.steps[-1] if s.steps else None
        where = f"step {last.iter}, a `{last.kind}` step at {last.at:%H:%M:%S} UTC" if last else "nothing was logged"
        lines += ["**Ended without a final answer.** The process stopped before it could write `final.json`: it was "
                  "killed hard (kill -9, a closed laptop), or the run is older than crash handling (2026-10-03).",
                  "", f"Last step logged: {where}.", ""]
    else:
        icon = {"done": "✅", "max_steps": "⚠️", "error": "⚠️", "crashed": "❌", "interrupted": "⏹"}[final.stopped]
        iters = f" after {out.iterations} iteration(s)" if out else ""
        lines += [f"{icon} **{final.stopped}**{iters}" + (f" — {final.reason}" if final.reason else ""), ""]
    if s.unreadable_lines:
        lines += [f"_{s.unreadable_lines} line(s) of steps.jsonl are in an older format and are left out below._", ""]

    goals = final.goals if final else next((st.goals for st in reversed(s.steps) if isinstance(st, GoalsStep)), [])
    lines += ["## Goals", ""]
    if goals:
        lines += ["| goal | skill | done | answer |", "|---|---|---|---|"]
        lines += [f"| {g.id}: {_cell(g.text, 120)} | {g.skill or 'none (refused)'} | {'✓' if g.done else '—'} | "
                  f"{_cell(g.answer or '', ANSWER_CHARS)} |" for g in goals]
    else:
        lines.append("No goals were set.")
    lines.append("")

    lines += ["## Steps", "", "| step | layer | what | result |", "|---|---|---|---|"]
    for st in s.steps:
        row = _step_row(st)
        if row:
            lines.append(f"| {st.iter} | {row[0]} | {row[1]} | {row[2]} |")
    lines.append("")

    lines += ["## Writes", ""]
    if s.writes:
        lines += ["| tool | row | fields set | before | sent? |", "|---|---|---|---|---|"]
        lines += [f"| `{w.tool}` | {w.row_id or '—'} | {_cell(w.fields)} | {_cell(w.before)} | "
                  f"{'no (dry run)' if w.dry_run else 'yes'} |" for w in s.writes]
    else:
        lines.append("None.")
    lines.append("")

    calls = sum(1 for st in s.steps if isinstance(st, LlmStep))
    if out:
        u = out.usage
        lines += ["## Tokens", "", f"{calls} LLM call(s) · {_k(u.input_tokens)} in · {_k(u.output_tokens)} out · "
                  f"{_k(u.thinking_tokens)} thinking", ""]

    if final and final.answer:
        lines += ["## Final answer", ""] + [f"> {x}" if x else ">" for x in final.answer.splitlines()] + [""]

    if final and final.error and final.error.traceback:
        lines += ["## Error details", "", "```", final.error.traceback, "```", ""]
    return "\n".join(lines)


def write_report(run_dir: Path) -> Path:
    path = run_dir / "report.md"
    path.write_text(render(build_summary(run_dir)))
    return path


def main() -> None:
    ap = argparse.ArgumentParser(description="Write report.md for saved runs (any age, finished or not)")
    ap.add_argument("run_dirs", nargs="+", type=Path)
    for run_dir in ap.parse_args().run_dirs:
        print(write_report(run_dir))


if __name__ == "__main__":
    main()
