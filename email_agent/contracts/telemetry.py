"""Trace contracts (Revision 12, Stage 10): one run's journal as spans (S17 `telemetry/spans.py` builds the same tree).

    SpanRecord   one span: run, planner round, node attempt, LLM call or write; with its true start and end, its
                 parent, attributes (GenAI semantic conventions where they exist, `email_agent.*` for ours) and events
    TraceSummary what was written: counts per kind, and where
"""

from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

SpanKind = Literal["run", "round", "node", "llm", "write"]
AttrValue = str | int | float | bool


class SpanEvent(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str                                   # the journal event kind (critic_reviewed, fanned_out, …)
    at: datetime
    attributes: dict[str, AttrValue] = Field(default_factory=dict)


class SpanRecord(BaseModel):
    model_config = ConfigDict(extra="forbid")

    trace_id: str                               # 32 hex characters, the same for every span of a run
    span_id: str                                # 16 hex characters
    parent_id: str | None = None
    name: str
    kind: SpanKind
    start: datetime
    end: datetime
    attributes: dict[str, AttrValue] = Field(default_factory=dict)
    events: list[SpanEvent] = Field(default_factory=list)
    status: Literal["ok", "error", "unset"] = "unset"
    status_message: str | None = None           # why a span is an error (failed, unfinished, uncertain)


class TraceSummary(BaseModel):
    model_config = ConfigDict(extra="forbid")

    run_id: str
    spans: int
    by_kind: dict[str, int]
    errors: int
    file: str | None = None
    exported_to: str | None = None              # "console", or the OTLP endpoint
