"""LLM contracts: what goes to a model and what comes back. Provider-neutral."""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, Field


class ChatMessage(BaseModel):
    role: Literal["user", "model"]
    text: str


class LlmRequest(BaseModel):
    purpose: Literal["goals", "planner", "judge", "answer", "critic", "validator"]   # which graph role asks
    system: str
    messages: list[ChatMessage] = Field(min_length=1)
    response_schema: dict[str, Any] | None = None     # set → the model must answer with JSON of this shape
    temperature: float = 0.2


class ToolCall(BaseModel):
    """One tool call of the graph (a tool capability's node), as Action runs it."""

    name: str
    arguments: dict[str, Any] = Field(default_factory=dict)


class Usage(BaseModel):
    input_tokens: int = 0
    output_tokens: int = 0
    thinking_tokens: int = 0


class LlmReply(BaseModel):
    model: str
    text: str = ""
    finish_reason: str | None = None
    usage: Usage = Field(default_factory=Usage)
    elapsed_ms: float = 0.0
    provider: str | None = None                       # who answered (the route's option) …
    key_slot: int | None = None                       # … and which Gemini key SLOT (never the key)
    fallback_from: list[str] = Field(default_factory=list)   # options skipped or failed before it, in words
    reused: bool = False                              # replayed from run.sqlite on resume: no call, no tokens spent
