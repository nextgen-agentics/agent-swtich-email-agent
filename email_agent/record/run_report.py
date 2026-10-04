"""A readable report of one run: runs/<run_id>/report.md.

Built only from the files in the run folder (request.json, context.json, steps.jsonl,
writes.jsonl, final.json, outcome.json), so it works for any run — finished, crashed,
interrupted, or one killed so hard that no final.json was written. agent.run writes it at
the end of every run; for older runs:

    uv run python -m email_agent.record.run_report runs/<run_id> [runs/<run_id> …]
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any

from pydantic import BaseModel, ValidationError

from email_agent.contracts.agent import (
    FinalAnswer,
    RunContext,
    RunOutcome,
    RunRequest,
    WriteRecord,
)
from email_agent.contracts.runlog import (
    ActionStep,
    ErrorStep,
    LlmStep,
    NodeStep,
    PlanStep,
    RunSummary,
    StepLine,
    SyncStep,
)
from email_agent.graph.store import RUN_FILE, RunStore

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
        layer = f"{step.layer} (LLM)" + (f" `{step.node_id}`" if step.node_id else "")
        return layer, f"{secs} · {tokens}", _cell(step.error or "ok", 120)
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
    if isinstance(step, PlanStep):
        what = f"round {step.iter} ← {step.trigger}"
        if step.goals:
            goals = "; ".join(f"{g.id} → {g.skill or f'no skill ({g.refusal})'}" for g in step.goals)
            return "planner", what, _cell(f"goals: {goals}; added: {', '.join(step.added) or '—'}", 300)
        rejected = f" · {len(step.rejected)} rejected: {_cell(step.rejected[-1], 120)}" if step.rejected else ""
        return "planner", what, _cell(f"added: {', '.join(step.added) or '—'}{rejected}", 300)
    if isinstance(step, NodeStep):
        mark = {"succeeded": "✓", "failed": "✗", "waiting": "⏸", "fanned_out": "⇉", "cancelled": "–"}[step.state]
        return "node", f"`{step.node_id}` [{step.capability}]", f"{mark} {_cell(step.summary, 160)} · {step.seconds:.1f}s"
    if isinstance(step, SyncStep):
        rep = step.report
        changed = sum(t.changed for t in rep.tables)
        how = "full" if any(t.full for t in rep.tables) else "incremental"
        return "sync", f"local mailbox copy ({how})", (f"{rep.calls} calls · {rep.seconds:.1f}s · {changed} rows changed · "
                                                      f"{rep.facts_recomputed} conversations re-worked · "
                                                      f"{rep.threads_total} conversations / {rep.messages_total} messages")
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
        icon = {"done": "✅", "max_steps": "⚠️", "error": "⚠️", "crashed": "❌", "interrupted": "⏹",
                "waiting": "⏸"}.get(final.stopped, "⚠️")
        iters = f" after {out.iterations} iteration(s)" if out else ""
        lines += [f"{icon} **{final.stopped}**{iters}" + (f" — {final.reason}" if final.reason else ""), ""]
    if s.unreadable_lines:
        lines += [f"_{s.unreadable_lines} line(s) of steps.jsonl are in an older format and are left out below._", ""]

    goals = final.goals if final else next((st.goals for st in reversed(s.steps)
                                            if isinstance(st, PlanStep) and st.goals), [])
    lines += ["## Goals", ""]
    if goals:
        lines += ["| goal | skill | done | answer |", "|---|---|---|---|"]
        lines += [f"| {g.id}: {_cell(g.text, 120)} | {g.skill or 'none (refused)'} | {'✓' if g.done else '—'} | "
                  f"{_cell(g.answer or '', ANSWER_CHARS)} |" for g in goals]
    else:
        lines.append("No goals were set.")
    lines.append("")

    lines += _graph_section(Path(s.run_dir) if s.run_dir else None)

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


def _graph_section(run_dir: Path | None) -> list[str]:
    """The run's graph as a checklist (the running-task todo list, Revision 12), read from run.sqlite."""
    if run_dir is None or not (run_dir / RUN_FILE).exists():
        return []
    path = run_dir / RUN_FILE
    store = RunStore(path)
    try:
        snap = store.snapshot()
        budgets = store.budgets()
        events = store.events()
        context = store.context()
    finally:
        store.close()
    mark = {"succeeded": "[x]", "failed": "[!]", "cancelled": "[-]", "waiting": "[~]", "running": "[>]", "pending": "[ ]"}
    lines = ["## Graph (the run's todo list)", ""]
    for n in sorted(snap.nodes.values(), key=lambda n: (n.started_at is None, str(n.started_at), n.id)):
        if n.capability == "judge_shard":
            continue
        shards = [x for x in snap.nodes.values() if x.capability == "judge_shard" and x.id.startswith(n.id + ".")]
        extra = f" · {sum(x.state == 'succeeded' for x in shards)}/{len(shards)} shards" if shards else ""
        err = f" — {_cell(n.error.message, 120)}" if n.error else ""
        lines.append(f"- {mark.get(n.state, '[?]')} `{n.id}` {n.capability}"
                     f"{f' ({n.goal_id})' if n.goal_id else ''}{extra}{err}")
    if budgets:
        lines += ["", "Budgets: " + ", ".join(f"{b.name} {b.spent:g}/{b.limit:g}" for b in budgets)]
    lines += _memory_lines(context)
    spans = run_dir / "spans.jsonl"
    if spans.exists():
        count = sum(1 for line in spans.read_text().splitlines() if line.strip())
        try:
            where = run_dir.resolve().relative_to(Path.cwd())
        except ValueError:
            where = run_dir
        lines += ["", f"Trace: {count} spans in `spans.jsonl` (to send them to OpenTelemetry: "
                      f"`uv run python -m email_agent.record.telemetry {where} --otlp`)"]
    lines += _resume_lines(run_dir, snap, events)
    return lines + [""]


def _memory_lines(context: dict[str, Any]) -> list[str]:
    """What the run's planner saw from memory (Stage 7): your house rules and the last runs on these mailboxes."""
    rules = ((context.get("context") or {}).get("house_rules") or "").strip()
    history = context.get("history") or []
    if not rules and not history:
        return []
    lines = ["", "### Memory the planner saw", ""]
    if rules:
        lines.append(f"- house rules ({len(rules)} characters): {_cell(rules, 200)}")
    lines += [f"- earlier run: {_cell(h, 260)}" for h in history]
    return lines


def _resume_lines(run_dir: Path, snap: Any, events: list[Any]) -> list[str]:
    """Resumes, settled writes, reused replies, and what a waiting run needs from you (Stage 6)."""
    lines = []
    resumes = sum(e.kind == "run_resumed" for e in events)
    reused = sum(e.kind == "llm_call_finished" and bool(e.payload.get("reused")) for e in events)
    settled = [e.payload for e in events if e.kind == "write_reconciled"]
    if resumes or reused or settled:
        verdict = {True: "happened", False: "not sent (sent again)", None: "changed by someone else (not sent)"}
        lines += ["", f"Resumed {resumes} time(s) · {reused} model reply(ies) reused instead of called again"]
        lines += [f"- reconcile: {s.get('tool')} {s.get('row_id') or ''} → {verdict.get(s.get('happened'), '?')}"
                  f"{' — ' + _cell(s.get('note'), 120) if s.get('note') else ''}" for s in settled]
    for n in snap.nodes.values():
        if n.state != "waiting" or n.wait is None:
            continue
        if n.wait.event_type == "approval.received":
            writes = n.wait.metadata.get("writes") or []
            lines += ["", f"### Waiting for your approval: `{n.id}` ({len(writes)} write(s))", ""]
            lines += [f"- {_cell(w, 220)}" for w in writes[:60]]
            if len(writes) > 60:
                lines.append(f"- … and {len(writes) - 60} more")
            lines += ["", f"Send them: `uv run python -m email_agent --resume {run_dir} --approve` · "
                          f"write nothing: `… --reject`"]
        else:
            lines += ["", f"Waiting: `{n.id}` on {n.wait.event_type} — a write could not be settled against the live "
                          f"platform; check the row, then `uv run python -m email_agent --resume {run_dir}`"]
    if not snap.finished and not any(n.state == "waiting" for n in snap.nodes.values()):
        lines += ["", f"Not finished. To continue: `uv run python -m email_agent --resume {run_dir}`"]
    return lines


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
