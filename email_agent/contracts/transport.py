"""Evidence of one HTTP request/response (secrets masked by the caller, bodies capped).

rest.py and mcp_session.py take an optional recorder that receives an HttpExchange per request; scripts use it
to keep what the server actually said next to each check. The agent itself runs without a recorder.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Any

from pydantic import BaseModel, Field, field_validator


def _now() -> datetime:
    return datetime.now(timezone.utc)


EVIDENCE_BODY_LIMIT = 4000  # characters of JSON kept per request/response body


def _cap(value: Any) -> Any:
    """Keep evidence files small: bodies over the limit become a preview string."""
    if value is None:
        return None
    text = value if isinstance(value, str) else json.dumps(value, ensure_ascii=False, default=str)
    if len(text) <= EVIDENCE_BODY_LIMIT:
        return value
    return f"{text[:EVIDENCE_BODY_LIMIT]}… [truncated, {len(text)} chars total]"


class HttpExchange(BaseModel):
    method: str
    url: str
    request_body: Any = None
    status: int | None = None
    response_headers: dict[str, str] = Field(default_factory=dict)
    response_body: Any = None
    elapsed_ms: float = 0.0
    at: datetime = Field(default_factory=_now)

    @field_validator("request_body", "response_body", mode="before")
    @classmethod
    def _cap_body(cls, value: Any) -> Any:
        return _cap(value)
