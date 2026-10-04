"""LLM contracts: what goes to a model and what comes back. Provider-neutral."""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, Field


class ChatMessage(BaseModel):
    role: Literal["user", "model"]
    text: str


class ToolSpec(BaseModel):
    """One tool offered to the model: name, what it does, and its JSON-schema arguments."""

    name: str
    description: str = ""
    parameters: dict[str, Any] = Field(default_factory=lambda: {"type": "object", "properties": {}})


class LlmRequest(BaseModel):
    purpose: Literal["perception", "decision"]
    system: str
    messages: list[ChatMessage] = Field(min_length=1)
    tools: list[ToolSpec] = Field(default_factory=list)
    response_schema: dict[str, Any] | None = None     # set → the model must answer with JSON of this shape
    temperature: float = 0.2


class ToolCall(BaseModel):
    name: str
    arguments: dict[str, Any] = Field(default_factory=dict)


class Usage(BaseModel):
    input_tokens: int = 0
    output_tokens: int = 0
    thinking_tokens: int = 0


class LlmReply(BaseModel):
    model: str
    text: str = ""
    tool_calls: list[ToolCall] = Field(default_factory=list)
    finish_reason: str | None = None
    usage: Usage = Field(default_factory=Usage)
    elapsed_ms: float = 0.0
    provider: str | None = None                       # who answered (the route's option) …
    key_slot: int | None = None                       # … and which Gemini key SLOT (never the key)
    fallback_from: list[str] = Field(default_factory=list)   # options skipped or failed before it, in words
