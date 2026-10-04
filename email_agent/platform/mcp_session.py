"""MCP access to AgentSwitch through the official SDK (`mcp` 2.x).

    async with McpSession(settings, "suryodaya") as mcp:
        tools = await mcp.list_tools()
        outcome = await mcp.call("EmailThread.list", {"folder": "inbox", "limit": 20})
        if outcome.ok: rows = outcome.data()

How it connects (verified by scripts/platform/mcp_sdk_spike.py):
    REST login → bearer token → httpx2.AsyncClient(headers=Authorization)
    → mcp.client.streamable_http.streamable_http_client(url, http_client=…)
    → mcp.Client(transport, mode="legacy")   # initialize handshake, protocol 2025-11-25

`call()` never raises for a tool failure: the SDK raises MCPError for JSON-RPC
errors (unknown tool, invalid arguments) and sets is_error for tool errors —
both become ToolOutcome(ok=False, error=...), which the agent can read.

Every wait has a time limit (Stage 7, after a harness batch stalled for 8 minutes between two tasks): a call
(mcp_call_timeout_s), opening the session (mcp_open_timeout_s: the token check, run in a thread so it cannot block the
event loop, plus the SDK's handshake) and closing it (mcp_close_timeout_s: the SDK's session DELETE). Before this, the
open and close waited on httpx's 300 s read timeout and on the synchronous REST token check (up to 3 tries × 60 s),
which nothing could interrupt. A slow open raises McpCallError(kind="timeout"); a slow close drops the connection
with a warning and the caller goes on.
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from typing import Any, Callable

import httpx2
from mcp import Client
from mcp.client.streamable_http import streamable_http_client
from mcp.shared.exceptions import MCPError
from mcp.types import Tool
from pydantic import BaseModel

from email_agent.config import Settings
from email_agent.contracts.mcp import McpCallError, McpError, ToolOutcome
from email_agent.contracts.transport import HttpExchange
from email_agent.platform.rest import Recorder, RestClient, redact

logger = logging.getLogger(__name__)


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
        self._limit = asyncio.Semaphore(max(1, settings.mcp_concurrency))   # graph nodes share one session

    # ── connection ───────────────────────────────────────────────────────────
    async def __aenter__(self) -> "McpSession":
        limit = self.settings.mcp_open_timeout_s
        try:
            async with asyncio.timeout(limit):
                token = await asyncio.to_thread(self.rest.valid_token)   # sync REST: off the event loop
                hooks: dict[str, list[Callable[..., Any]]] = (
                    {"request": [self._on_request], "response": [self._on_response]} if self.recorder else {})
                self._http = httpx2.AsyncClient(
                    headers={"Authorization": f"Bearer {token}"},
                    timeout=httpx2.Timeout(self.settings.http_timeout_s, read=300),
                    event_hooks=hooks,
                )
                await self._http.__aenter__()
                self._client = Client(streamable_http_client(self.instance.mcp_url, http_client=self._http),
                                      mode="legacy")
                await self._client.__aenter__()
        except TimeoutError:
            self._client = None
            await self._drop_http()
            raise McpCallError(McpError(kind="timeout", message=(
                f"opening the MCP session to {self.instance.name} took more than {limit:.0f} s"))) from None
        return self

    async def __aexit__(self, *exc) -> None:
        limit = self.settings.mcp_close_timeout_s
        try:
            if self._client is not None:
                async with asyncio.timeout(limit):
                    await self._client.__aexit__(*exc)
        except TimeoutError:
            logger.warning("closing the MCP session to %s took more than %.0f s; the connection was dropped",
                           self.instance.name, limit)
        finally:
            self._client = None
            await self._drop_http()

    async def _drop_http(self) -> None:
        if self._http is not None:
            http, self._http = self._http, None
            try:
                async with asyncio.timeout(5):
                    await http.aclose()
            except Exception:  # noqa: BLE001 — closing a dead connection must never stop the caller
                logger.debug("closing the HTTP client failed", exc_info=True)

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
        if self.recorder is None:
            return
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
            try:
                async with asyncio.timeout(self.settings.mcp_call_timeout_s):
                    page = await self.client.list_tools(cursor=cursor)
            except TimeoutError:
                raise McpCallError(McpError(kind="timeout", message=(
                    f"tools/list got no answer within {self.settings.mcp_call_timeout_s:.0f} s"))) from None
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
        async with self._limit:
            try:
                async with asyncio.timeout(self.settings.mcp_call_timeout_s):
                    return await self._call(name, args)
            except TimeoutError:
                outcome = ToolOutcome(tool=name, arguments=args, ok=False, elapsed_ms=self.settings.mcp_call_timeout_s * 1000,
                                      error=McpError(kind="timeout", message=f"no answer within "
                                                                             f"{self.settings.mcp_call_timeout_s:.0f} s"))
                if self.on_call:
                    self.on_call(outcome)
                return outcome

    async def _call(self, name: str, args: dict[str, Any]) -> ToolOutcome:
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
