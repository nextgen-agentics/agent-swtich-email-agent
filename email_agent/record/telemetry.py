"""OpenTelemetry export (Revision 12, Stage 10): a run's journal as a span tree. Adapted from S17 `telemetry/spans.py`.

    uv run python -m email_agent.record.telemetry runs/<run_id>               # (re)write runs/<run_id>/spans.jsonl
    uv run python -m email_agent.record.telemetry runs/<run_id> --console     # also print the spans through the SDK
    uv run python -m email_agent.record.telemetry runs/<run_id> --otlp [URL]  # also send them to an OTLP/HTTP collector

Nothing new is recorded (S17's point): the journal in run.sqlite already holds every step, so the trace is built from
it after the fact. Every journal event carries its real time, so every span has a true start and end.

    run                 run_started … the last event
    ├── planner round   its goals/planner model calls → graph_patched (a code-only patch is a round with no calls)
    │   └── llm call
    └── node            node_started → succeeded / failed / cancelled / waiting (one span per attempt)
        ├── llm call    a judge, validator, critic or answer call made by that node
        └── write       write_started → completed / failed / uncertain
Other journal events (fan-out, critic and validator verdicts, scores, cache hits, reconcile, approvals, resumes)
become span events on their node, or on the run. A span still open at the last event (a crash, a wait) ends there
with status error "unfinished".

Attributes follow the OpenTelemetry GenAI conventions where they exist (`gen_ai.operation.name`,
`gen_ai.provider.name`, `gen_ai.request.model`, `gen_ai.usage.input_tokens` / `output_tokens`); ours are
`email_agent.*`. No prompt or reply text goes into a span (they hold mail; they stay in steps.jsonl).

The span file needs no extra package. `--console` and `--otlp` need the optional group: `uv sync --group otel`.
At the end of every run agent.py writes spans.jsonl, and also exports to OTLP when OTEL_EXPORTER_OTLP_ENDPOINT is set.
"""

from __future__ import annotations

import argparse
import hashlib
import logging
import os
from collections import Counter
from datetime import datetime
from pathlib import Path
from typing import Any

from email_agent.contracts.graph import JournalEvent
from email_agent.contracts.telemetry import (
    AttrValue,
    SpanEvent,
    SpanRecord,
    TraceSummary,
)
from email_agent.graph.store import RUN_FILE, RunStore

SPANS_FILE = "spans.jsonl"
SERVICE_NAME = "team10-email-agent"
NODE_END = {"node_succeeded": "succeeded", "node_failed": "failed", "node_cancelled": "cancelled",
            "node_waiting": "waiting"}
WRITE_END = {"write_completed": "completed", "write_failed": "failed", "write_uncertain": "uncertain"}
NODE_LEVEL = {"llm_call_started", "llm_call_finished", "write_started", *NODE_END, *WRITE_END}

log = logging.getLogger(__name__)


def _attrs(values: dict[str, Any]) -> dict[str, AttrValue]:
    """Span attributes must be simple values: lists and dicts become short text, None is left out."""
    out: dict[str, AttrValue] = {}
    for k, v in values.items():
        if v is None:
            continue
        out[k] = v if isinstance(v, (str, int, float, bool)) else str(v)[:300]
    return out


class _Builder:
    def __init__(self, run_id: str):
        self.trace_id = hashlib.sha256(run_id.encode()).hexdigest()[:32]
        self.run_id = run_id
        self.spans: list[SpanRecord] = []
        self._n = 0

    def span(self, name: str, kind: str, parent: SpanRecord | None, start: datetime, **attributes: Any) -> SpanRecord:
        self._n += 1
        s = SpanRecord(trace_id=self.trace_id, span_id=hashlib.sha256(f"{self.run_id}:{self._n}".encode()).hexdigest()[:16],
                       parent_id=parent.span_id if parent else None, name=name, kind=kind, start=start, end=start,
                       attributes=_attrs({"email_agent.run.id": self.run_id, **attributes}))
        self.spans.append(s)
        return s


def spans_of(run_dir: Path) -> list[SpanRecord]:
    """The span tree of one run, from its journal (run.sqlite)."""
    store = RunStore(run_dir / RUN_FILE)
    try:
        events = store.events()
    finally:
        store.close()
    if not events:
        return []
    run_id = next((e.payload.get("run_id") for e in events if e.kind == "run_started"), None) or run_dir.name
    b = _Builder(run_id)
    root = b.span(f"run {run_id}", "run", None, events[0].at, **{"gen_ai.operation.name": "invoke_agent",
                                                                 "gen_ai.agent.name": "team10-email-agent"})
    nodes: dict[str, SpanRecord] = {}            # open node attempt per node id
    parked: dict[str, SpanRecord] = {}           # node attempts that ended waiting (approval, reconcile)
    calls: dict[str, SpanRecord] = {}            # open LLM call per node id ("" = the planner)
    writes: dict[str, SpanRecord] = {}           # open write per outbox key
    round_calls: list[SpanRecord] = []           # planner calls waiting for their patch
    rounds = 0

    def event_on(e: JournalEvent) -> None:
        target = nodes.get(e.node_id or "", root)
        target.events.append(SpanEvent(name=e.kind, at=e.at, attributes=_attrs(
            {k: v for k, v in e.payload.items() if k not in ("goals", "add", "connect", "cancel")})))

    def close_open(end: datetime, why: str) -> None:
        """At a stop (seen as the next run_resumed) or at the end: whatever is still open ended there, unfinished."""
        nonlocal round_calls, rounds
        for s in [*nodes.values(), *calls.values(), *writes.values()]:
            s.end, s.status, s.status_message = max(end, s.start), "error", why
        nodes.clear()
        calls.clear()
        writes.clear()
        if round_calls:                          # planner calls with no patch after them (a stop in the planner)
            rounds += 1
            r = b.span(f"planner round {rounds}", "round", root, round_calls[0].start, **{"email_agent.round": rounds})
            r.end, r.status, r.status_message = max(end, r.start), "error", why
            for c in round_calls:
                c.parent_id = r.span_id
            round_calls = []

    previous = events[0].at
    for e in events:
        p = e.payload
        if e.kind == "run_resumed":              # the run stopped after `previous`; resume starts afresh
            close_open(previous, "unfinished: the run stopped here and was resumed later")
        previous = e.at
        if e.kind == "node_started":
            parked.pop(e.node_id or "", None)         # released (e.g. --approve): a new attempt starts
            nodes[e.node_id or ""] = b.span(f"node {e.node_id}", "node", root, e.at, **{
                "email_agent.node.id": e.node_id, "email_agent.capability": p.get("capability"),
                "email_agent.attempt": p.get("attempt")})
        elif e.kind in NODE_END:
            # a parked attempt answered from outside (e.g. --reject) ends without a new start: the same span goes on
            s = nodes.pop(e.node_id or "", None) or parked.pop(e.node_id or "", None)
            if s is None:
                s = b.span(f"node {e.node_id}", "node", root, e.at, **{"email_agent.node.id": e.node_id})
            if e.kind == "node_waiting":
                parked[e.node_id or ""] = s
            s.end, s.attributes["email_agent.node.state"] = e.at, NODE_END[e.kind]
            if e.kind == "node_failed":
                failure = p.get("failure") or {}
                s.status, s.status_message = "error", str(failure.get("message") or "failed")[:300]
                s.attributes["email_agent.failure.kind"] = str(failure.get("kind") or "error")
            elif e.kind == "node_waiting":
                s.status_message = f"waiting for {p.get('event_type') or p.get('handle') or 'an outside event'}"
            else:
                s.status = "ok"
        elif e.kind == "llm_call_started":
            parent = nodes.get(e.node_id or "") if e.node_id else None
            s = b.span(f"chat {p.get('purpose')}", "llm", parent or root, e.at, **{
                "gen_ai.operation.name": "chat", "email_agent.purpose": p.get("purpose")})
            calls[e.node_id or ""] = s
            if not e.node_id:
                round_calls.append(s)
        elif e.kind == "llm_call_finished":
            s = calls.pop(e.node_id or "", None)
            if s is None:                        # a reply reused on resume: no call was made
                parent = nodes.get(e.node_id or "") if e.node_id else None
                s = b.span(f"chat {p.get('purpose')}", "llm", parent or root, e.at, **{
                    "gen_ai.operation.name": "chat", "email_agent.purpose": p.get("purpose")})
                if not e.node_id:
                    round_calls.append(s)
            s.end, s.status = e.at, "ok"
            s.attributes.update(_attrs({"gen_ai.provider.name": p.get("provider"),
                                        "gen_ai.request.model": p.get("model"), "gen_ai.response.model": p.get("model"),
                                        "gen_ai.usage.input_tokens": p.get("input_tokens"),
                                        "gen_ai.usage.output_tokens": p.get("output_tokens"),
                                        "email_agent.reused": bool(p.get("reused"))}))
        elif e.kind == "graph_patched":
            rounds += 1
            start = min((c.start for c in round_calls), default=e.at)
            r = b.span(f"planner round {rounds}", "round", root, start, **{
                "email_agent.round": rounds, "email_agent.added": len(p.get("add") or []),
                "email_agent.cancelled": len(p.get("cancel") or []), "email_agent.finish": bool(p.get("finish")),
                "email_agent.llm_calls": len(round_calls), "email_agent.reason": str(p.get("reason") or "")[:300]})
            r.end, r.status = e.at, "ok"
            if p.get("planner_failed"):
                r.status, r.status_message = "error", str(p.get("reason") or "the planner failed")[:300]
            for c in round_calls:                # the planner's calls belong to their round
                c.parent_id = r.span_id
            round_calls = []
        elif e.kind == "write_started":
            parent = nodes.get(e.node_id or "", root)
            writes[p.get("key") or str(e.seq)] = b.span(f"write {p.get('tool')}", "write", parent, e.at, **{
                "gen_ai.operation.name": "execute_tool", "gen_ai.tool.name": p.get("tool"),
                "email_agent.row.id": p.get("row_id"), "email_agent.dry_run": p.get("dry_run")})
        elif e.kind in WRITE_END:
            w = writes.pop(p.get("key") or "", None)
            if w is None:
                continue
            s = w
            s.end, s.attributes["email_agent.write.state"] = e.at, WRITE_END[e.kind]
            if e.kind == "write_completed":
                s.status = "ok"
            else:
                s.status, s.status_message = "error", str(p.get("error") or WRITE_END[e.kind])[:300]
        else:
            event_on(e)

    root.end = events[-1].at
    close_open(root.end, "unfinished: the run stopped while this was open")
    root.status = "error" if any(s.status == "error" and s.kind == "node" for s in b.spans) else "ok"
    return b.spans


def write_spans(run_dir: Path) -> TraceSummary:
    spans = spans_of(run_dir)
    path = run_dir / SPANS_FILE
    path.write_text("".join(s.model_dump_json() + "\n" for s in spans))
    return TraceSummary(run_id=spans[0].attributes.get("email_agent.run.id", run_dir.name) if spans else run_dir.name,
                        spans=len(spans), by_kind=dict(Counter(s.kind for s in spans)),
                        errors=sum(s.status == "error" for s in spans), file=str(path))


def export(spans: list[SpanRecord], exporter: Any) -> None:
    """Replay span records through the OpenTelemetry SDK (their own times, parents and attributes) to `exporter`."""
    from opentelemetry import trace
    from opentelemetry.sdk.resources import Resource
    from opentelemetry.sdk.trace import TracerProvider
    from opentelemetry.sdk.trace.export import SimpleSpanProcessor
    from opentelemetry.trace import Status, StatusCode

    provider = TracerProvider(resource=Resource.create({"service.name": SERVICE_NAME}))
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    tracer = provider.get_tracer("email_agent.record.telemetry")
    by_id = {s.span_id: s for s in spans}

    def depth(s: SpanRecord) -> int:
        d = 0
        while s.parent_id and s.parent_id in by_id:
            s, d = by_id[s.parent_id], d + 1
        return d

    live: dict[str, Any] = {}
    for s in sorted(spans, key=depth):          # parents first, so each child gets its parent's context
        parent = live.get(s.parent_id or "")
        ctx = trace.set_span_in_context(parent) if parent is not None else None
        span = tracer.start_span(s.name, context=ctx, start_time=int(s.start.timestamp() * 1e9),
                                 attributes={**s.attributes, "email_agent.span.kind": s.kind})
        for ev in s.events:
            span.add_event(ev.name, attributes=ev.attributes, timestamp=int(ev.at.timestamp() * 1e9))
        if s.status != "unset":
            span.set_status(Status(StatusCode.OK if s.status == "ok" else StatusCode.ERROR, s.status_message))
        live[s.span_id] = span
    for s in sorted(spans, key=depth, reverse=True):
        live[s.span_id].end(end_time=int(s.end.timestamp() * 1e9))
    provider.shutdown()


def otlp_exporter(endpoint: str | None = None) -> Any:
    from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
    url = endpoint or os.environ.get("OTEL_EXPORTER_OTLP_TRACES_ENDPOINT")
    if url is None and os.environ.get("OTEL_EXPORTER_OTLP_ENDPOINT"):
        url = os.environ["OTEL_EXPORTER_OTLP_ENDPOINT"].rstrip("/") + "/v1/traces"
    return OTLPSpanExporter(endpoint=url) if url else OTLPSpanExporter()


def at_run_end(run_dir: Path) -> TraceSummary | None:
    """agent.py, at every exit: the spans file, and an OTLP export when OTEL_EXPORTER_OTLP_ENDPOINT is set. A failure is
    logged, never raised: the run's own files matter more."""
    try:
        summary = write_spans(run_dir)
        if os.environ.get("OTEL_EXPORTER_OTLP_ENDPOINT") or os.environ.get("OTEL_EXPORTER_OTLP_TRACES_ENDPOINT"):
            export(spans_of(run_dir), otlp_exporter())
            summary.exported_to = "otlp"
        return summary
    except Exception:  # noqa: BLE001
        log.exception("could not write the trace of %s", run_dir.name)
        return None


def main() -> None:
    ap = argparse.ArgumentParser(description="A run's journal as OpenTelemetry spans")
    ap.add_argument("run_dir", type=Path)
    shown = ap.add_mutually_exclusive_group()
    shown.add_argument("--console", action="store_true", help="also print the spans through the SDK's console exporter")
    shown.add_argument("--otlp", nargs="?", const="", metavar="URL",
                       help="also send them to an OTLP/HTTP collector (default: OTEL_EXPORTER_OTLP_ENDPOINT)")
    args = ap.parse_args()
    summary = write_spans(args.run_dir)
    if args.console or args.otlp is not None:
        spans = spans_of(args.run_dir)
        if args.console:
            from opentelemetry.sdk.trace.export import ConsoleSpanExporter
            export(spans, ConsoleSpanExporter())
            summary.exported_to = "console"
        else:
            export(spans, otlp_exporter(args.otlp or None))
            summary.exported_to = args.otlp or "OTLP endpoint from the environment"
    print(summary.model_dump_json(indent=1))


if __name__ == "__main__":
    main()
