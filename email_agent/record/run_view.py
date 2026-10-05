"""A web page for one finished run: runs/<run_id>/view.html (Revision 15).

Built only from the files the run already saved (run.sqlite, steps.jsonl, writes.jsonl, final.json, outcome.json,
spans.jsonl, undo.jsonl), so it works for any run: finished, crashed, interrupted, waiting, or one of the old loop.
The page is one file: the RunView as JSON plus the page's own style and script (view/); the graph libraries come from
a CDN, and every table still shows without them. agent.run writes it at the end of every run; for saved runs:

    uv run email-view runs/<run_id> [runs/<run_id> …] [--open]
    uv run email-view harness_runs/<batch>          # every run of a batch (harness-score also writes the batch page)
"""

from __future__ import annotations

import argparse
import html
import json
import logging
import re
import webbrowser
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from pydantic import BaseModel, ValidationError

from email_agent.contracts.agent import FinalAnswer, RunContext, RunRequest, WriteRecord
from email_agent.contracts.graph import NodeState
from email_agent.contracts.llm import LlmReply, Usage
from email_agent.contracts.runlog import LlmStep, NodeStep, PlanStep, StepLine, SyncStep
from email_agent.contracts.view import (
    RunView,
    UndoMark,
    ViewCall,
    ViewCritic,
    ViewHeader,
    ViewNode,
    ViewProblem,
    ViewRound,
    ViewScore,
    ViewSpan,
    ViewValidator,
    ViewWrite,
)
from email_agent.graph.outbox import WriteOutbox
from email_agent.graph.store import RUN_FILE, RunStore
from email_agent.record.run_report import build_summary
from email_agent.record.telemetry import spans_of

log = logging.getLogger(__name__)

PAGE = "view.html"
ASSETS = Path(__file__).resolve().parent / "view"


def _who(reply: LlmReply) -> str:
    return f"{reply.provider or '?'}/{reply.model}" + (f" key #{reply.key_slot}" if reply.key_slot else "")


def _seconds(start: datetime | None, end: datetime | None) -> float | None:
    return round((end - start).total_seconds(), 2) if start and end else None


def _where(run_dir: Path) -> str:
    try:
        return str(run_dir.resolve().relative_to(Path.cwd()))
    except ValueError:
        return str(run_dir)


def _older_lines(run_dir: Path) -> list[dict[str, Any]]:
    """steps.jsonl lines no current step model reads (the old Perception/Decision loop), shown as they are."""
    path = run_dir / "steps.jsonl"
    out: list[dict[str, Any]] = []
    for line in (path.read_text().splitlines() if path.exists() else []):
        if not line.strip():
            continue
        try:
            StepLine.model_validate({"step": json.loads(line)})
        except (ValidationError, json.JSONDecodeError):
            try:
                out.append(json.loads(line))
            except json.JSONDecodeError:
                out.append({"unreadable": line[:2000]})
    return out


def _undo_marks(run_dir: Path) -> dict[tuple[str, str | None], str]:
    path = run_dir / "undo.jsonl"
    marks: dict[tuple[str, str | None], str] = {}
    for line in (path.read_text().splitlines() if path.exists() else []):
        try:
            u = UndoMark.model_validate_json(line)
        except ValidationError:
            continue
        marks[(u.tool, u.row_id)] = u.action + (f": {u.detail}" if u.detail else "")
    return marks


def build_view(run_dir: Path) -> RunView:
    s = build_summary(run_dir)
    req: RunRequest | None = s.request
    ctx: RunContext | None = s.context
    final: FinalAnswer | None = s.final
    has_graph = (run_dir / RUN_FILE).exists()

    snap, events, budgets, context, outbox, added_by = None, [], [], {}, [], {}
    if has_graph:
        store = RunStore(run_dir / RUN_FILE)
        try:
            snap, events, budgets = store.snapshot(), store.events(), store.budgets()
            context, outbox, added_by = store.context(), WriteOutbox(store).records(), store.added_by()
        finally:
            store.close()

    # ── model calls and planner rounds ───────────────────────────────────────
    calls: list[ViewCall] = []
    rounds: list[ViewRound] = []
    sync = None
    for st in s.steps:
        if isinstance(st, LlmStep):
            r = st.reply
            calls.append(ViewCall(
                n=len(calls) + 1, layer=st.layer, node_id=st.node_id, round=st.iter, at=st.at, valid=st.valid,
                error=st.error, answered_by=_who(r) if r else None, fallback_from=r.fallback_from if r else [],
                reused=r.reused if r else False, usage=r.usage if r else Usage(),
                seconds=round(r.elapsed_ms / 1000, 2) if r else None, system=st.request.system,
                messages=st.request.messages, response_schema=st.request.response_schema, reply=r.text if r else None))
        elif isinstance(st, PlanStep):
            rounds.append(ViewRound(n=st.iter, trigger=st.trigger, at=st.at, goals=st.goals, added=st.added,
                                    cancelled=st.cancelled, rejected=st.rejected, finished=st.finished,
                                    reason=st.reason))
        elif isinstance(st, SyncStep) and sync is None:
            sync = st.report
    for rd in rounds:
        rd.calls = [c.n for c in calls if c.node_id is None and c.round == rd.n]

    # ── writes: writes.jsonl, plus the outbox's status, plus undo ────────────
    by_key = {o.key: o for o in outbox}
    undo = _undo_marks(run_dir)
    writes: list[ViewWrite] = []
    seen: set[str] = set()
    for w in s.writes:
        o = by_key.get(w.key or "")
        seen.add(w.key or "")
        writes.append(_write(len(writes) + 1, w, o, undo))
    for o in outbox:                                 # sent but never completed: failed or uncertain writes
        if o.key not in seen:
            writes.append(ViewWrite(n=len(writes) + 1, tool=o.tool, row_id=o.row_id, fields=o.arguments,
                                    before=o.before or {}, node_id=o.node_id, status=str(o.status),
                                    receipt=o.receipt, error=o.error, at=o.updated_at,
                                    undo=undo.get((o.tool, o.row_id))))

    # ── tasks ────────────────────────────────────────────────────────────────
    nodes: list[ViewNode] = []
    if snap is not None:
        rounds_by_trigger = {e.payload.get("trigger_event"): e.payload.get("round") for e in events
                             if e.kind == "graph_patched"}
        judges = {n.id for n in snap.nodes.values() if n.capability == "judge_threads"}
        for n in sorted(snap.nodes.values(), key=lambda n: (n.started_at is None, str(n.started_at), n.id)):
            source = n.id.split(".", 1)[0] if "." in n.id and n.id.split(".", 1)[0] in judges else None
            seq = added_by.get(n.id)
            how = (f"fan-out of {source}" if source else
                   f"planner round {rounds_by_trigger[seq]}" if seq in rounds_by_trigger else
                   f"journal event {seq}" if seq is not None else None)
            nodes.append(ViewNode(
                id=n.id, capability=n.capability, goal_id=n.goal_id, state=str(n.state), attempt=n.attempt,
                input=n.input, result=n.result, error=f"{n.error.kind}: {n.error.message}" if n.error else None,
                waiting_on=n.wait.event_type if n.wait else None, started_at=n.started_at, ended_at=n.ended_at,
                seconds=_seconds(n.started_at, n.ended_at), added_by=how, fanned_from=source,
                calls=[c.n for c in calls if c.node_id == n.id],
                writes=[w.n for w in writes if w.node_id == n.id]))
    else:                                            # old loop: the node lines are all there is
        for st in s.steps:
            if isinstance(st, NodeStep):
                nodes.append(ViewNode(id=st.node_id, capability=st.capability, goal_id=st.goal_id, state=st.state,
                                      seconds=st.seconds, result={"summary": st.summary}))

    # ── checks ───────────────────────────────────────────────────────────────
    critics = [ViewCritic(node_id=e.node_id, goal_id=e.payload.get("goal_id"), ready=bool(e.payload.get("ready")),
                          overruled=bool(e.payload.get("overruled")), ending=e.payload.get("ending") or "",
                          reason=e.payload.get("reason") or "", missing=e.payload.get("missing") or [], at=e.at)
               for e in events if e.kind == "critic_reviewed"]
    validators = [ViewValidator(node_id=e.node_id, **{k: v for k, v in e.payload.items()
                                                      if k in ViewValidator.model_fields and k != "node_id"})
                  for e in events if e.kind == "validator_checked"]
    scores = [ViewScore(node_id=e.node_id, score=e.payload.get("score"), issues=e.payload.get("issues") or [])
              for e in events if e.kind == "answer_scored"]

    # ── header ───────────────────────────────────────────────────────────────
    out = s.outcome
    usage = out.usage if out else Usage(input_tokens=sum(c.usage.input_tokens for c in calls),
                                        output_tokens=sum(c.usage.output_tokens for c in calls),
                                        thinking_tokens=sum(c.usage.thinking_tokens for c in calls))
    served: dict[str, int] = dict(out.served_by) if out else {}
    if not served:
        for c in calls:
            if c.answered_by:
                served[c.answered_by] = served.get(c.answered_by, 0) + 1
    started = req.started_at if req else (ctx.started_at if ctx else None)
    ended = out.ended_at if out else (max((e.at for e in events), default=None) if events else None)
    header = ViewHeader(
        run_id=s.run_id, request=req.request if req else (ctx.request if ctx else "unknown"),
        instance=req.instance if req else (ctx.instance if ctx else "unknown"),
        mailboxes=[m.email for m in ctx.mailboxes] if ctx else (req.mailboxes or [] if req else []),
        today=ctx.today if ctx else (req.today if req else None), dry_run=req.dry_run if req else None,
        stopped=final.stopped if final else "unknown", reason=final.reason if final else None,
        started_at=started, ended_at=ended, seconds=_seconds(started, ended),
        model=f"{req.provider}/{req.model}" if req else None, route=req.route if req else None, served_by=served,
        usage=usage, calls=len(calls), budgets=budgets, error=final.error if final else None,
        next_step=_next_step(run_dir, snap), has_graph=has_graph)

    rules = ((context.get("context") or {}).get("house_rules") if context else (ctx.house_rules if ctx else None))
    view = RunView(generated_at=datetime.now(timezone.utc), header=header,
                   goals=final.goals if final else next((r.goals for r in reversed(rounds) if r.goals), []),
                   answer=final.answer if final else None, nodes=nodes, edges=list(snap.edges) if snap else [],
                   rounds=rounds, critics=critics, validators=validators, scores=scores, calls=calls, writes=writes,
                   spans=_spans(run_dir) if has_graph else [], house_rules=rules or None,
                   history=list(context.get("history") or []), sync=sync, older_lines=_older_lines(run_dir))
    view.problems = _problems(view, events)
    return view


def _write(n: int, w: WriteRecord, o: Any, undo: dict[tuple[str, str | None], str]) -> ViewWrite:
    return ViewWrite(n=n, tool=w.tool, row_id=w.row_id, fields=w.fields, before=w.before, dry_run=w.dry_run,
                     node_id=o.node_id if o else None, status=str(o.status) if o else None,
                     receipt=o.receipt if o else None, error=o.error if o else None, at=w.at,
                     undo=undo.get((w.tool, w.row_id)))


def _next_step(run_dir: Path, snap: Any) -> str | None:
    if snap is None:
        return None
    where = _where(run_dir)
    if any(n.state == NodeState.WAITING and n.wait and n.wait.event_type == "approval.received"
           for n in snap.nodes.values()):
        return f"uv run email-agent --resume {where} --approve   (or --reject to write nothing)"
    if not snap.finished:
        return f"uv run email-agent --resume {where}"
    return None


def _spans(run_dir: Path) -> list[ViewSpan]:
    try:
        spans = spans_of(run_dir)
    except Exception:  # noqa: BLE001  (the timeline is a nice-to-have: a journal it cannot read leaves it empty)
        log.exception("could not read the timeline of %s", run_dir.name)
        return []
    if not spans:
        return []
    t0 = min(sp.start for sp in spans)
    parent = {sp.span_id: sp.parent_id for sp in spans}

    def depth(span_id: str) -> int:
        d, p = 0, parent.get(span_id)
        while p:
            d, p = d + 1, parent.get(p)
        return d

    def ms(t: datetime) -> float:
        return round((t - t0).total_seconds() * 1000, 1)

    return [ViewSpan(name=sp.name, kind=sp.kind, depth=depth(sp.span_id), start_ms=ms(sp.start), end_ms=ms(sp.end),
                     status=sp.status, message=sp.status_message) for sp in sorted(spans, key=lambda x: x.start)]


def _problems(v: RunView, events: list[Any]) -> list[ViewProblem]:
    """Everything that went wrong, in one list, each pointing at where it happened."""
    p: list[ViewProblem] = []
    h = v.header
    if h.stopped == "unknown":
        p.append(ViewProblem(where="run", title="Ended without a final answer",
                             detail="The process stopped before it could write final.json (killed hard, or a run "
                                    "older than crash handling)."))
    elif h.stopped not in ("done",):
        p.append(ViewProblem(where="run", title=f"Stopped: {h.stopped}", detail=h.reason or ""))
    if h.error:
        p.append(ViewProblem(where="run", title=f"{h.error.type} in {h.error.where} (round {h.error.iter})",
                             detail=(h.error.message + "\n\n" + h.error.traceback).strip()))
    for e in events:
        if e.kind == "budget_exhausted":
            p.append(ViewProblem(where="run", title="A budget ran out", detail=json.dumps(e.payload)))
        elif e.kind == "search_fallback":
            p.append(ViewProblem(where="task", ref=e.node_id, title="Search fell back to full text",
                                 detail=json.dumps(e.payload)))
    for r in v.rounds:
        for why in r.rejected:
            p.append(ViewProblem(where="planner", ref=f"round {r.n}", title="Planner's plan rejected, asked again",
                                 detail=why))
    for n in v.nodes:
        if n.state in ("failed", "cancelled"):
            p.append(ViewProblem(where="task", ref=n.id, title=f"Task {n.state}: {n.id} ({n.capability})",
                                 detail=n.error or ""))
    failovers: dict[tuple[str, ...], list[ViewCall]] = {}
    for c in v.calls:
        if not c.valid:
            p.append(ViewProblem(where="call", ref=f"call {c.n}", title=f"Model reply rejected ({c.layer})",
                                 detail=c.error or ""))
        if c.fallback_from:                          # one entry per reason, not one per call
            reasons = tuple(re.sub(r"resting \d+s", "resting", x) for x in c.fallback_from)
            failovers.setdefault(reasons, []).append(c)
    for reasons, group in failovers.items():
        who = sorted({c.answered_by or "?" for c in group})
        p.append(ViewProblem(where="call", ref=f"call {group[0].n}",
                             title=f"{len(group)} model call(s) answered by a fallback ({', '.join(who)})",
                             detail="Tried first, could not answer:\n" + "\n".join(reasons) +
                                    "\n\nCalls: " + ", ".join(str(c.n) for c in group)))
    for cr in v.critics:
        if not cr.ready:
            p.append(ViewProblem(where="check", ref=cr.node_id, title=f"Evidence check: not ready ({cr.goal_id})",
                                 detail=cr.reason + ("\nmissing: " + "; ".join(cr.missing) if cr.missing else "")))
        elif cr.overruled:
            p.append(ViewProblem(where="check", ref=cr.node_id, title=f"Evidence check overruled ({cr.goal_id})",
                                 detail=cr.reason))
    for va in v.validators:
        if va.held or va.possible_misses:
            p.append(ViewProblem(where="check", ref=va.node_id,
                                 title=f"Second model disagreed: {len(va.held)} held, {len(va.possible_misses)} "
                                       "possible misses", detail=f"{va.agreed}/{va.checked} agreed ({va.skill})"))
    for w in v.writes:
        if w.error or (w.status and w.status not in ("completed",)):
            p.append(ViewProblem(where="write", ref=f"write {w.n}", title=f"Write {w.status or 'failed'}: {w.tool}",
                                 detail=w.error or ""))
    return p


# ── the page ─────────────────────────────────────────────────────────────────

def render_page(data: BaseModel, title: str) -> str:
    """One self-contained page: the template, the shared style and script, and the data as JSON."""
    page = (ASSETS / "page.html").read_text()
    payload = data.model_dump_json().replace("<", "\\u003c")   # no "</script>" or "<!--" from a reply can end the block
    return (page.replace("__TITLE__", html.escape(title))
                .replace("/*__CSS__*/", (ASSETS / "view.css").read_text())
                .replace("/*__JS__*/", (ASSETS / "view.js").read_text())
                .replace("__DATA__", payload))


def write_view(run_dir: Path) -> Path:
    view = build_view(run_dir)
    path = run_dir / PAGE
    path.write_text(render_page(view, f"Run {view.header.run_id}"))
    return path


def page_at_run_end(run_dir: Path) -> Path | None:
    """agent.py, at every exit: the run page. A failure is logged, never raised: the run's own files matter more."""
    try:
        return write_view(run_dir)
    except Exception:  # noqa: BLE001
        log.exception("could not write the run page of %s", run_dir.name)
        return None


def main() -> None:
    ap = argparse.ArgumentParser(description="Write view.html (a web page of the run) for saved runs")
    ap.add_argument("run_dirs", nargs="+", type=Path, help="run folders, or a harness batch folder (all its runs)")
    ap.add_argument("--open", action="store_true", help="open the page(s) in the browser")
    args = ap.parse_args()
    for d in args.run_dirs:
        targets = sorted(x for x in (d / "runs").iterdir() if x.is_dir()) if (d / "runs").is_dir() else [d]
        for run_dir in targets:
            path = write_view(run_dir)
            print(path.resolve().as_uri())
            if args.open:
                webbrowser.open(path.resolve().as_uri())


if __name__ == "__main__":
    main()
