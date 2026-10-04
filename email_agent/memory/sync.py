"""MemorySync: the read copy of the platform's AgentMemory (Stage 7), kept the way the mailbox copy is
(email_agent/mailbox/sync.py): newest first by `updated_at`, down to the last watermark minus a 10-minute overlap, one
watermark per instance, retried calls.
"""

from __future__ import annotations

import asyncio
from datetime import datetime
from typing import Any

from tenacity import (
    AsyncRetrying,
    retry_if_exception_type,
    stop_after_attempt,
    wait_exponential,
)

from email_agent.contracts.mirror import EntitySync
from email_agent.contracts.platform import AgentMemory, ListPage
from email_agent.contracts.tool_args import TOOL_ARGS
from email_agent.mailbox.sync import OVERLAP, PAGE, SyncError, _CallFailed, _ts
from email_agent.memory.store import LongTermMemory
from email_agent.platform.mcp_session import McpSession


class MemorySync:
    """The read copy of the platform's AgentMemory (`memory_store.LongTermMemory`, Stage 7). Memories belong to the
    company, not to a mailbox, so there is one watermark per instance. `is_active` is left out of the call, so
    switched-off memories come back too (checked live 2026-10-04) and the copy sees the whole lifecycle."""

    def __init__(self, mcp: McpSession, store: LongTermMemory):
        self.mcp, self.store = mcp, store

    async def run(self) -> EntitySync:
        mark = self.store.sync_watermark()
        cutoff = None if mark is None else (_ts(mark) or datetime.min) - OVERLAP
        rows: list[AgentMemory] = []
        calls, offset = 0, 0
        while True:
            page = await self._page({"sort_by": "updated_at", "sort_order": "desc", "limit": PAGE, "offset": offset})
            calls += 1
            rows += page.data
            offset += len(page.data)
            if len(page.data) < PAGE or offset >= page.total:
                break
            oldest = min((t for r in page.data if (t := _ts(r.updated_at)) is not None), default=None)
            if cutoff is not None and oldest is not None and oldest < cutoff:
                break
        changed = await asyncio.to_thread(self.store.upsert_platform, rows)
        newest = max((r.updated_at for r in rows if r.updated_at), key=lambda v: _ts(v) or datetime.min, default=None)
        watermark = max((v for v in (newest, mark) if v), key=lambda v: _ts(v) or datetime.min, default=None)
        self.store.set_sync_watermark(watermark)
        return EntitySync(entity="AgentMemory", mailbox="(company)", full=mark is None, calls=calls, fetched=len(rows),
                          changed=changed, deleted=0, watermark_before=mark, watermark_after=watermark)

    async def party(self, party_id: str) -> list[AgentMemory]:
        """Every memory of one party, live (switched-off ones too); the copy takes them, and the party's rows the
        platform no longer has are marked gone."""
        rows: list[AgentMemory] = []
        offset = 0
        while True:
            page = await self._page({"party_id": party_id, "limit": PAGE, "offset": offset})
            rows += page.data
            offset += len(page.data)
            if len(page.data) < PAGE or offset >= page.total:
                break
        await asyncio.to_thread(self.store.upsert_platform, rows)
        self.store.mark_gone(party_id, {r.id for r in rows})
        return rows

    async def _page(self, args: dict[str, Any]) -> ListPage:
        tool = "AgentMemory.list"
        try:
            async for attempt in AsyncRetrying(stop=stop_after_attempt(3), wait=wait_exponential(multiplier=1, max=8),
                                               retry=retry_if_exception_type(_CallFailed), reraise=True):
                with attempt:
                    outcome = await self.mcp.call(tool, TOOL_ARGS[tool].model_validate(args))
                    if not outcome.ok:
                        raise _CallFailed(outcome)
        except _CallFailed as e:
            o = e.outcome
            raise SyncError(f"{tool} {args} failed after 3 tries: {o.error.message if o.error else o.text}") from None
        return ListPage[AgentMemory].model_validate(outcome.data())
