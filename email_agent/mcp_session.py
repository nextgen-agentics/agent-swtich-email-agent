"""MCP access to AgentSwitch through the official SDK (`mcp` 2.x).

    async with McpSession(settings, "suryodaya") as mcp:
        tools = await mcp.list_tools()
        outcome = await mcp.call("EmailThread.list", {"folder": "inbox", "limit": 20})
        if outcome.ok: rows = outcome.data()

How it connects (verified by scripts/mcp_sdk_spike.py):
    REST login → bearer token → httpx2.AsyncClient(headers=Authorization)
    → mcp.client.streamable_http.streamable_http_client(url, http_client=…)
    → mcp.Client(transport, mode="legacy")   # initialize handshake, protocol 2025-11-25

`call()` never raises for a tool failure: the SDK raises MCPError for JSON-RPC
errors (unknown tool, invalid arguments) and sets is_error for tool errors —
both become ToolOutcome(ok=False, error=...), which the agent can read.
"""

from __future__ import annotations

import json
import time
from typing import Any, Callable

import httpx2
from mcp import Client
from mcp.client.streamable_http import streamable_http_client
from mcp.shared.exceptions import MCPError
from mcp.types import Tool
from pydantic import BaseModel

from email_agent.config import Settings
from email_agent.contracts.mcp import McpError, ToolOutcome
from email_agent.contracts.transport import HttpExchange
from email_agent.rest import Recorder, RestClient, redact


def _json_or_text(raw: bytes) -> Any:
    try:
        return json.loads(raw)
    except ValueError:
        return raw.decode("utf-8", "replace")


class McpSession:
    """One MCP connection to one instance. Use as `async with`."""

    def __init__(self, settings: Settings, instance: str, *,
                 recorder: Recorder | None = None,
                 on_call: Callable[[ToolOutcome], None] | None = None):
        self.settings = settings
        self.rest = RestClient(settings, instance)
        self.instance = self.rest.instance
        self.recorder = recorder
        self.on_call = on_call
        self._http: httpx2.AsyncClient | None = None
        self._client: Client | None = None

    # ── connection ───────────────────────────────────────────────────────────
    async def __aenter__(self) -> "McpSession":
        token = self.rest.valid_token()
        hooks = {"request": [self._on_request], "response": [self._on_response]} if self.recorder else {}
        self._http = httpx2.AsyncClient(
            headers={"Authorization": f"Bearer {token}"},
            timeout=httpx2.Timeout(self.settings.http_timeout_s, read=300),
            event_hooks=hooks,
        )
        await self._http.__aenter__()
        self._client = Client(streamable_http_client(self.instance.mcp_url, http_client=self._http),
                              mode="legacy")
        await self._client.__aenter__()
        return self

    async def __aexit__(self, *exc) -> None:
        if self._client is not None:
            await self._client.__aexit__(*exc)
        if self._http is not None:
            await self._http.__aexit__(*exc)

    @property
    def client(self) -> Client:
        if self._client is None:
            raise RuntimeError("McpSession used outside `async with`")
        return self._client

    # ── evidence recording (httpx2 event hooks) ──────────────────────────────
    async def _on_request(self, request: httpx2.Request) -> None:
        request.extensions["t0"] = time.perf_counter()

    async def _on_response(self, response: httpx2.Response) -> None:
        await response.aread()
        req = response.request
        self.recorder(HttpExchange(
            method=req.method, url=str(req.url),
            request_body=redact(_json_or_text(req.content)) if req.content else None,
            status=response.status_code,
            response_headers={k: v for k, v in response.headers.items()
                              if k.lower() in {"content-type", "allow", "mcp-session-id"}},
            response_body=redact(_json_or_text(response.content)) if response.content else None,
            elapsed_ms=(time.perf_counter() - req.extensions.get("t0", time.perf_counter())) * 1000,
        ))

    # ── tools ────────────────────────────────────────────────────────────────
    async def list_tools(self) -> list[Tool]:
        tools: list[Tool] = []
        cursor: str | None = None
        while True:
            page = await self.client.list_tools(cursor=cursor)
            tools.extend(page.tools)
            cursor = page.next_cursor
            if not cursor:
                return tools

    async def call(self, name: str, arguments: BaseModel | dict[str, Any] | None = None) -> ToolOutcome:
        """Call one tool. Failures come back as ToolOutcome(ok=False), not exceptions."""
        if isinstance(arguments, BaseModel):
            # exclude_unset, NOT exclude_none: the generated *Update* models carry schema
            # defaults (is_read=False, message_count=0, …); sending them would silently
            # reset fields we never meant to touch.
            args = arguments.model_dump(exclude_unset=True, mode="json")
        else:
            args = dict(arguments or {})
        started = time.perf_counter()
        try:
            result = await self.client.call_tool(name, args)
        except MCPError as e:
            outcome = ToolOutcome(tool=name, arguments=args, ok=False,
                                  elapsed_ms=(time.perf_counter() - started) * 1000,
                                  error=McpError(kind="jsonrpc", code=e.code, message=e.message, data=e.data))
        else:
            elapsed = (time.perf_counter() - started) * 1000
            if result.is_error:
                text = "\n".join(getattr(c, "text", "") or "" for c in result.content)
                outcome = ToolOutcome(tool=name, arguments=args, ok=False, result=result, elapsed_ms=elapsed,
                                      error=McpError(kind="tool", message=text[:2000]))
            else:
                outcome = ToolOutcome(tool=name, arguments=args, ok=True, result=result, elapsed_ms=elapsed)
        if self.on_call:
            self.on_call(outcome)
        return outcome
