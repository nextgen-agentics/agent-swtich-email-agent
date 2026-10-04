"""MailboxSync: bring the local mailbox copy (`mailbox_store.MailboxStore`) up to date with the platform (Revision 12).

Per working mailbox, conversations and messages are synced separately (Stage 0: a conversation's `updated_at` does not
always cover its newest message), each newest first by `updated_at`, in pages of 1000:
  - first time, or --full-sync: every page; rows the platform no longer has are removed;
  - afterwards: pages until the oldest row on a page is older than the last watermark minus OVERLAP (a safety margin
    for rows written while the last sync ran), so an unchanged mailbox costs one call per table.
Then the facts of every conversation that changed are worked out again (conversation.py), and nothing else.

Calls go one at a time: Stage 0 measured that the server answers our calls one at a time (about 1 s each, whatever their
size), so parallel pages would not be faster. A failed call is retried (tenacity) before the sync gives up.
"""

from __future__ import annotations

import asyncio
import time
from datetime import datetime, timedelta, timezone
from typing import Any

from tenacity import (
    AsyncRetrying,
    retry_if_exception_type,
    stop_after_attempt,
    wait_exponential,
)

from email_agent.contracts.agent import OurMailbox
from email_agent.contracts.mcp import ToolOutcome
from email_agent.contracts.mirror import EntitySync, SyncReport
from email_agent.contracts.platform import EmailMessage, EmailThread, ListPage, list_page
from email_agent.contracts.tool_args import TOOL_ARGS
from email_agent.mailbox.store import MailboxStore
from email_agent.platform.mcp_session import McpSession

PAGE = 1000                         # the platform's largest page
OVERLAP = timedelta(minutes=10)
TABLES: list[tuple[str, str, type]] = [("EmailThread", "EmailThread.list", EmailThread),
                                       ("EmailMessage", "EmailMessage.list", EmailMessage)]


class SyncError(RuntimeError):
    pass


class _CallFailed(Exception):
    def __init__(self, outcome: ToolOutcome):
        super().__init__(outcome.error.message if outcome.error else outcome.text)
        self.outcome = outcome


def _ts(value: str | None) -> datetime | None:
    """Platform times are ISO strings, sometimes with a zone; compare them as naive UTC."""
    if not value:
        return None
    try:
        t = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return t.astimezone(timezone.utc).replace(tzinfo=None) if t.tzinfo else t


class MailboxSync:
    def __init__(self, mcp: McpSession, store: MailboxStore, instance: str, mailboxes: list[OurMailbox],
                 our_emails: set[str]):
        self.mcp, self.store, self.instance = mcp, store, instance
        self.mailboxes, self.our_emails = mailboxes, our_emails

    async def run(self, *, full: bool = False) -> SyncReport:
        started = time.perf_counter()
        report = SyncReport(instance=self.instance)
        dirty: set[str] = set()
        for mb in self.mailboxes:
            for entity, tool, model in TABLES:
                table, touched = await self._sync_table(entity, tool, model, mb, full)
                report.tables.append(table)
                report.calls += table.calls
                dirty |= touched
        if self.store.facts_stale(self.our_emails):
            dirty = self.store.all_thread_ids()
        report.facts_recomputed = await asyncio.to_thread(self.store.rebuild_facts, dirty, self.our_emails)
        report.threads_total, report.messages_total = self.store.counts()
        report.seconds = round(time.perf_counter() - started, 2)
        return report

    async def _sync_table(self, entity: str, tool: str, model: type, mb: OurMailbox,
                          full: bool) -> tuple[EntitySync, set[str]]:
        state = self.store.sync_state(entity, mb.id)
        full_pass = full or state.watermark is None
        mark = _ts(state.watermark)
        cutoff = None if full_pass or mark is None else mark - OVERLAP
        rows: list[Any] = []
        calls, offset = 0, 0
        while True:
            page = await self._page(tool, model, mb.id, offset)
            calls += 1
            mine = [r for r in page.data if getattr(r, "mailbox_id", None) in (mb.id, None)]
            rows += mine
            offset += len(page.data)
            if len(page.data) < PAGE or offset >= page.total:
                break
            oldest = min((t for r in page.data if (t := _ts(r.updated_at)) is not None), default=None)
            if cutoff is not None and oldest is not None and oldest < cutoff:
                break
        if entity == "EmailThread":
            changed_ids = await asyncio.to_thread(self.store.upsert_threads, rows)
            changed, touched = len(changed_ids), set(changed_ids)
        else:
            changed, touched = await asyncio.to_thread(self.store.upsert_messages, rows)
        deleted = 0
        if full_pass:
            deleted, gone = self.store.delete_missing(entity, mb.id, {r.id for r in rows})
            touched |= gone
        newest = max((r.updated_at for r in rows if r.updated_at), key=lambda v: _ts(v) or datetime.min, default=None)
        watermark = max((v for v in (newest, state.watermark) if v), key=lambda v: _ts(v) or datetime.min, default=None)
        self.store.set_sync_state(entity, mb.id, watermark, full_pass)
        return EntitySync(entity=entity, mailbox=mb.email, full=full_pass, calls=calls, fetched=len(rows),
                          changed=changed, deleted=deleted, watermark_before=state.watermark,
                          watermark_after=watermark), touched

    async def _page(self, tool: str, model: type, mailbox_id: str, offset: int) -> ListPage:
        args = TOOL_ARGS[tool].model_validate({"mailbox_id": mailbox_id, "sort_by": "updated_at", "sort_order": "desc",
                                               "limit": PAGE, "offset": offset})
        try:
            async for attempt in AsyncRetrying(stop=stop_after_attempt(3), wait=wait_exponential(multiplier=1, max=8),
                                               retry=retry_if_exception_type(_CallFailed), reraise=True):
                with attempt:
                    outcome = await self.mcp.call(tool, args)
                    if not outcome.ok:
                        raise _CallFailed(outcome)
        except _CallFailed as e:
            o = e.outcome
            raise SyncError(f"{tool} (mailbox {mailbox_id}, offset {offset}) failed after 3 tries: "
                            f"{o.error.message if o.error else o.text}") from None
        return list_page(model).model_validate(outcome.data())

