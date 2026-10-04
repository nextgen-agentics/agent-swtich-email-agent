"""MCP contracts on top of the official SDK's own pydantic types.

The wire protocol (JSON-RPC 2.0 over Streamable HTTP) is handled entirely by
the `mcp` SDK; its types (`mcp.types.Tool`, `CallToolResult`, …) are already
pydantic models. This module only adds what our code needs on top:

  ToolOutcome      one tool call as data: ok or not, the SDK result, why it failed
  McpError         failure classification (the SDK raises; we record)

The saved tools/list snapshot (ToolCatalogFile) is a scripts-only shape: scripts/contracts/catalog.py.
"""

from __future__ import annotations

import json
from typing import Any, Literal

from mcp.types import CallToolResult
from pydantic import BaseModel, Field

PROTOCOL_VERSION = "2025-11-25"  # what AgentSwitch speaks (verified by scripts/platform/mcp_sdk_spike.py)


def tool_entity(name: str) -> str:
    """`EmailThread.list` → `EmailThread`; `endpoint.email.x` → `endpoint`."""
    return name.split(".", 1)[0]


def tool_operation(name: str) -> str:
    return name.split(".", 1)[1] if "." in name else ""


McpErrorKind = Literal["jsonrpc", "tool", "http", "auth", "transport", "validation", "timeout"]


class McpError(BaseModel):
    """Why a call failed. Kept as data so the agent (and reports) can read it.

    jsonrpc     server answered with a JSON-RPC error (SDK raised MCPError), e.g. unknown tool,
                "Invalid tool arguments."
    tool        the tool ran and reported failure (CallToolResult.is_error)
    validation  our own pydantic args model rejected the call before sending
    timeout     no answer within mcp_call_timeout_s: for a write, it may or may not have happened
    auth/http/transport  connection-level problems
    """

    kind: McpErrorKind
    message: str
    code: int | None = None
    http_status: int | None = None
    data: Any = None


class ToolOutcome(BaseModel):
    """Result of one tools/call, success or failure."""

    tool: str
    arguments: dict[str, Any] = Field(default_factory=dict)
    ok: bool
    result: CallToolResult | None = None
    error: McpError | None = None
    elapsed_ms: float = 0.0

    @property
    def text(self) -> str:
        if self.result is None:
            return self.error.message if self.error else ""
        return "\n".join(getattr(c, "text", "") or "" for c in self.result.content)

    def data(self) -> Any:
        """The JSON payload: structured_content if sent, else the text parsed as JSON."""
        if self.result is None:
            return None
        if self.result.structured_content is not None:
            return self.result.structured_content
        try:
            return json.loads(self.text)
        except (json.JSONDecodeError, TypeError):
            return None


class McpCallError(Exception):
    """Raised only for failures a caller cannot treat as data (auth, transport)."""

    def __init__(self, error: McpError):
        super().__init__(f"{error.kind}: {error.message}")
        self.error = error
