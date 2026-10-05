"""A fake AgentSwitch book: rows in memory, every call logged, and the failures we have seen live (a write that never
gets an answer, a crash right after the platform applied a write). It stands in for both McpSession and RestClient,
and it outlives a run the way the real server does, so a resumed run sees what the first pass wrote."""

from __future__ import annotations

import asyncio
import json
import uuid
from collections import defaultdict
from collections.abc import Callable
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from typing import Any

from mcp.types import CallToolResult, TextContent, Tool
from pydantic import BaseModel

from email_agent.contracts.mcp import McpError, ToolOutcome, tool_entity, tool_operation
from email_agent.contracts.platform import Me, RegimeLocale
from email_agent.graph.store import InjectedCrash

ROOT = Path(__file__).resolve().parents[2]
SCHEMAS = ROOT / "data" / "schemas" / "suryodaya"
PAGING = {"limit", "offset", "sort_by", "sort_order", "search"}
CACHE_FROM = 10_000                        # load-test tables only; smaller tests edit rows in place


def now(**later: float) -> str:
    """The platform's clock, written as it writes times: naive UTC with microseconds."""
    t = datetime.now(timezone.utc).replace(tzinfo=None) + timedelta(**later)
    return t.isoformat(timespec="microseconds")


class FakePlatform:
    def __init__(self) -> None:
        self.user = Me.model_validate_json((SCHEMAS / "me.json").read_text())
        self.locale = RegimeLocale.model_validate_json((SCHEMAS / "locale.json").read_text())
        self.rows: dict[str, dict[str, dict[str, Any]]] = defaultdict(dict)
        self.calls: list[tuple[str, dict[str, Any]]] = []
        self.no_answer: dict[str, bool] = {}     # tool → applied first? The caller gets a timeout either way.
        self.crash_after: set[str] = set()       # tool → apply it, then the agent process "dies"
        self.meanwhile: dict[str, Callable[[], None]] = {}   # tool → what someone else does right after that call
        self.leaks_other_mailboxes = False       # BUG-019/026: a list ignores its mailbox_id filter
        self.hooks: list[Callable[[str, dict[str, Any]], None]] = []   # run before every call (name, arguments)
        self.delay = 0.0                         # seconds each call takes, so calls overlap as they do live
        self.in_flight = self.peak = 0
        self._listings: dict[tuple, list[dict[str, Any]]] = {}   # sorted listings, until their table changes
        self._tools = [Tool(name=t["name"], description=t.get("description") or "", input_schema=t["inputSchema"])
                       for t in json.loads((SCHEMAS / "mcp-tools.json").read_text())["tools"]]

    # ── rows ─────────────────────────────────────────────────────────────────
    def add(self, entity: str, **row: Any) -> dict[str, Any]:
        row.setdefault("id", str(uuid.uuid4()))
        row.setdefault("company_id", self.user.company_id)
        row.setdefault("created_at", now())
        row.setdefault("updated_at", row["created_at"])
        self.rows[entity][row["id"]] = row
        self._changed(entity)
        return row

    def _changed(self, entity: str) -> None:
        self._listings = {k: v for k, v in self._listings.items() if k[0] != entity}

    def row(self, entity: str, row_id: str) -> dict[str, Any]:
        return self.rows[entity][row_id]

    def writes(self, tool: str | None = None) -> list[tuple[str, dict[str, Any]]]:
        """Every call that changed (or tried to change) platform data."""
        return [(name, args) for name, args in self.calls
                if tool_operation(name) not in ("list", "get") and (tool is None or name == tool)]

    # ── McpSession ───────────────────────────────────────────────────────────
    def session(self, *_args: Any, **_kwargs: Any) -> "FakePlatform":
        return self

    async def __aenter__(self) -> "FakePlatform":
        return self

    async def __aexit__(self, *exc: Any) -> None:
        return None

    async def list_tools(self) -> list[Tool]:
        return self._tools

    async def call(self, name: str, arguments: BaseModel | dict[str, Any] | None = None) -> ToolOutcome:
        args = arguments.model_dump(exclude_unset=True, mode="json") if isinstance(arguments, BaseModel) \
            else dict(arguments or {})
        self.calls.append((name, args))
        for hook in self.hooks:
            hook(name, args)
        if self.delay:
            self.in_flight += 1
            self.peak = max(self.peak, self.in_flight)
            try:
                await asyncio.sleep(self.delay)
            finally:
                self.in_flight -= 1
        if name in self.meanwhile:
            self.meanwhile.pop(name)()
        if name in self.no_answer:
            if self.no_answer[name]:
                self._apply(name, args)
            return ToolOutcome(tool=name, arguments=args, ok=False,
                               error=McpError(kind="timeout", message="no answer within 90 s"))
        try:
            data = self._apply(name, args)
        except LookupError as e:
            return self._failed(name, args, str(e))
        if name in self.crash_after:
            self.crash_after.discard(name)
            raise InjectedCrash(f"the agent died after the platform applied {name}")
        return ToolOutcome(tool=name, arguments=args, ok=True,
                           result=CallToolResult(content=[TextContent(type="text", text=json.dumps(data))],
                                                 structured_content=data, is_error=False))

    def _apply(self, name: str, args: dict[str, Any]) -> Any:
        entity, op = tool_entity(name), tool_operation(name)
        table = self.rows[entity]
        if op == "list":
            skip = PAGING | ({"mailbox_id"} if self.leaks_other_mailboxes else set())
            key = (entity, json.dumps({k: v for k, v in args.items() if k not in {"limit", "offset"}}, sort_keys=True),
                   self.leaks_other_mailboxes)
            rows = self._listings.get(key) if len(table) >= CACHE_FROM else None
            if rows is None:                       # a 50,000-row table is filtered and sorted once, not per page
                rows = [r for r in table.values() if all(_same(r.get(k), v) for k, v in args.items() if k not in skip)]
                if args.get("search"):
                    rows = [r for r in rows if args["search"].lower() in json.dumps(r).lower()]
                rows.sort(key=lambda r: r.get(args.get("sort_by") or "updated_at") or "",
                          reverse=args.get("sort_order", "desc") == "desc")
                if len(table) >= CACHE_FROM:
                    self._listings[key] = rows
            offset, limit = args.get("offset", 0), args.get("limit", 20)
            return {"data": rows[offset:offset + limit], "total": len(rows), "limit": limit, "offset": offset}
        if op == "get":
            if args["id"] not in table:
                raise LookupError(f"{entity} {args['id']} not found")
            return table[args["id"]]
        if op == "update":
            if args["id"] not in table:
                raise LookupError(f"{entity} {args['id']} not found")
            table[args["id"]].update({k: v for k, v in args.items() if k != "id"}, updated_at=now())
            self._changed(entity)
            return table[args["id"]]
        if op == "create":
            return self.add(entity, **args, created_by=self.user.id)
        if op == "delete":
            self._changed(entity)
            return table.pop(args["id"])
        raise LookupError(f"the fake platform has no {name}")

    @staticmethod
    def _failed(name: str, args: dict[str, Any], message: str) -> ToolOutcome:
        return ToolOutcome(tool=name, arguments=args, ok=False,
                           result=CallToolResult(content=[TextContent(type="text", text=message)], is_error=True),
                           error=McpError(kind="tool", message=message))

    # ── RestClient ───────────────────────────────────────────────────────────
    def rest(self, *_args: Any, **_kwargs: Any) -> "FakePlatform":
        return self

    def me(self) -> Me:
        return self.user

    def display_locale(self) -> RegimeLocale:
        return self.locale

    def request(self, method: str, path: str, *, json_body: Any = None, **_: Any) -> SimpleNamespace:
        """Only what WritePath sends over REST: a PUT that clears a field (MCP cannot send a null)."""
        assert method == "PUT", f"unexpected REST call {method} {path}"
        _, _, entity, row_id = path.split("/")
        self.calls.append((f"{entity}.update", {"id": row_id, **json_body}))
        row = self._apply(f"{entity}.update", {"id": row_id, **json_body})
        return SimpleNamespace(status_code=200, json=lambda: row, text=json.dumps(row))


def _same(have: Any, want: Any) -> bool:
    if isinstance(want, bool):
        return bool(have) == want
    return have == want
