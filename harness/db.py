"""What the harness reads from the database (MCP and REST, read-only, typed). The harness never reads the agent's text.

`snapshot` reads, in one session, every table the checks look at. `our_changes_since` finds every row our login
created or changed after a moment on the server's clock (the platform stores UTC without a zone mark, checked
2026-10-03 against a write probe), so a refusal can be confirmed from the database itself.
"""

from __future__ import annotations

import asyncio
from collections.abc import Sequence
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime

from pydantic import BaseModel, Field

from email_agent.config import Settings
from email_agent.contracts.platform import (
    AgentMemory,
    AgentTodo,
    EmailLabel,
    EmailMessage,
    EmailReminder,
    EmailThread,
    Party,
    Row,
    list_page,
)
from email_agent.platform.mcp_session import McpSession
from email_agent.platform.rest import RestClient
from harness.contracts import ThreadFlag

CLOCK_MARGIN = timedelta(seconds=2)
LIMIT = 1000
# Tables our agent can write; a refusal must leave every one of them untouched by us.
WRITABLE = {"EmailThread": EmailThread, "EmailMessage": EmailMessage, "EmailReminder": EmailReminder,
            "AgentMemory": AgentMemory, "AgentTodo": AgentTodo, "EmailLabel": EmailLabel}


class DbSnapshot(BaseModel):
    instance: str
    threads: list[EmailThread] = Field(default_factory=list)
    messages: list[EmailMessage] = Field(default_factory=list)
    reminders: list[EmailReminder] = Field(default_factory=list)
    memories: list[AgentMemory] = Field(default_factory=list)
    todos: list[AgentTodo] = Field(default_factory=list)
    labels: list[EmailLabel] = Field(default_factory=list)
    parties: list[Party] = Field(default_factory=list)

    def thread(self, thread_id: str) -> EmailThread | None:
        return next((t for t in self.threads if t.id == thread_id), None)

    def newest_message_at(self, thread_id: str) -> str | None:
        times = [m.received_at or m.sent_at or m.created_at for m in self.messages if m.thread_id == thread_id]
        return max((x for x in times if x), default=None)

    def rows(self, entity: str) -> Sequence[Row]:
        tables: dict[str, Sequence[Row]] = {
            "EmailThread": self.threads, "EmailMessage": self.messages, "EmailReminder": self.reminders,
            "AgentMemory": self.memories, "AgentTodo": self.todos, "EmailLabel": self.labels}
        return tables[entity]


async def _list(mcp: McpSession, tool: str, model: type[Row]) -> list:
    out = await mcp.call(tool, {"limit": LIMIT})
    if not out.ok:
        raise RuntimeError(f"{tool} failed: {out.error.message if out.error else out.text}")
    return list_page(model).model_validate(out.data()).data


async def snapshot(settings: Settings, instance: str) -> DbSnapshot:
    async with McpSession(settings, instance) as mcp:
        return DbSnapshot(
            instance=instance,
            threads=await _list(mcp, "EmailThread.list", EmailThread),
            messages=await _list(mcp, "EmailMessage.list", EmailMessage),
            reminders=await _list(mcp, "EmailReminder.list", EmailReminder),
            memories=await _list(mcp, "AgentMemory.list", AgentMemory),
            todos=await _list(mcp, "AgentTodo.list", AgentTodo),
            labels=await _list(mcp, "EmailLabel.list", EmailLabel),
            parties=await _list(mcp, "Party.list", Party))


def thread_state(t: EmailThread) -> ThreadFlag:
    return ThreadFlag(flag_status=t.flag_status, flag_due_date=t.flag_due_date, is_starred=t.is_starred,
                      importance=t.importance, split_category=t.split_category, summary=t.summary,
                      summary_updated_at=t.summary_updated_at)


async def thread_flags(settings: Settings, instance: str) -> dict[str, ThreadFlag]:
    async with McpSession(settings, instance) as mcp:
        threads = await _list(mcp, "EmailThread.list", EmailThread)
    return {t.id: thread_state(t) for t in threads}


async def server_clock_and_me(settings: Settings, instance: str) -> tuple[datetime, str]:
    """The server's own time (the HTTP Date header) minus a small margin, and our user id."""
    def call() -> tuple[datetime, str]:
        rest = RestClient(settings, instance)
        resp = rest.get("/api/auth/me")
        when = parsedate_to_datetime(resp.headers["date"]) if resp.headers.get("date") else datetime.now(timezone.utc)
        return when.astimezone(timezone.utc) - CLOCK_MARGIN, rest.me().id
    return await asyncio.to_thread(call)


def when(value: str | None) -> datetime | None:
    """A platform timestamp (UTC, no zone mark) as an aware datetime."""
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def our_changes_since(snap: DbSnapshot, me_id: str, start: datetime) -> list[str]:
    """`Entity id` for every row of a writable table created or last changed by us at or after `start`."""
    found = []
    for entity in WRITABLE:
        for row in snap.rows(entity):
            mine = row.created_by == me_id or row.updated_by == me_id
            changed = when(row.updated_at) or when(row.created_at)
            if mine and changed and changed >= start:
                found.append(f"{entity} {row.id}")
    return found
