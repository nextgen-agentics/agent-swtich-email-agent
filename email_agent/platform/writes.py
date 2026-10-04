"""WritePath: the one way a platform write leaves the agent (Revision 12, Stage 3).

Every write — a model's single tool call, a batch tool's rows, a fan-out's write plan — goes through the same steps:
  1. lock the row (two goals writing the same conversation take turns, so each one's `before` is right for undo)
  2. guard (shared data: team 11 uses the same rows)
       create: allowed · delete: only rows this run created
       update: only rows in our working mailboxes or created by our login; `before` = the changed fields' values now.
       For a batch of more than BATCH_FROM_COPY rows the local copy is synced first (≈1 call per table when nothing
       changed) and read instead of one live `get` per row: Stage 0 measured ≈1 s per call, whatever its size.
  3. outbox (graph/outbox.py): recorded before it is sent, so a resumed run never sends it twice; a write left
     uncertain by a crash is not sent again — the caller parks its node until reconcile (Stage 6, reconcile.py).
     A dry run goes through the outbox too (its "send" changes nothing), so a resumed dry run never records a write twice
  4. dry run: recorded as a dry-run write, nothing sent, the local copy left alone
  5. send: MCP; or REST `PUT /api/<Entity>/<id>` when a field is set back to null (the MCP schema refuses null —
     checked live 2026-10-04)
  6. record: WriteRecord to writes.jsonl (what the harness and undo_run.py read); an accepted conversation update is
     copied into the local copy so later reads in the run see it; a sent write is also added to the book's record of
     our writes (agent.py → event_store), so the watcher never answers a change we made
A run scoped to some conversations (the watcher's, Stage 8) may not write any other conversation.
"""

from __future__ import annotations

import asyncio
import json
from contextvars import ContextVar
from typing import Any, Awaitable, Callable

from pydantic import BaseModel, ValidationError

from email_agent.contracts.agent import RunContext, WriteRecord
from email_agent.contracts.graph import Deferred, OutboxStatus
from email_agent.contracts.mcp import tool_entity, tool_operation
from email_agent.contracts.platform import ROW_MODELS, Row
from email_agent.contracts.tool_args import TOOL_ARGS
from email_agent.graph.outbox import UncertainWrite, WriteOutbox
from email_agent.mailbox.store import MailboxStore
from email_agent.platform.mcp_session import McpSession
from email_agent.platform.rest import RestClient

BATCH_FROM_COPY = 3                         # more rows than this: guard from a freshly synced local copy
CURRENT_NODE: ContextVar[str] = ContextVar("current_node", default="run")   # set by each graph worker


class WriteFailed(Exception):
    """The platform refused the write (its message is kept for the outbox and the model)."""


class WriteOutcome(BaseModel):
    tool: str
    row_id: str | None = None
    ok: bool
    record: WriteRecord | None = None
    error: str | None = None
    guard: bool = False                    # refused by our guard (never sent)
    uncertain: str | None = None           # outbox key of a write a crash left unsettled
    receipt: Any = None


async def _dry() -> dict[str, Any]:
    return {"dry_run": True}


def _payload_row(data: Any) -> Any:
    return data["data"] if isinstance(data, dict) and isinstance(data.get("data"), dict) else data


class WritePath:
    def __init__(self, mcp: McpSession, rest: RestClient, ctx: RunContext, mirror: MailboxStore, *,
                 on_write: Callable[[WriteRecord], None], dry_run: bool, outbox: WriteOutbox | None = None,
                 refresh: Callable[[], Awaitable[None]] | None = None):
        self.mcp, self.rest, self.ctx, self.mirror = mcp, rest, ctx, mirror
        self.on_write, self.dry_run, self.outbox, self.refresh = on_write, dry_run, outbox, refresh
        self.created: set[str] = set()
        self._locks: dict[str, asyncio.Lock] = {}

    def _ours(self) -> set[str]:
        return set(self.ctx.our_addresses) or {m.email for m in self.ctx.mailboxes}

    # ── batches ──────────────────────────────────────────────────────────────
    async def write_many(self, items: list[tuple[str, dict[str, Any]]]) -> list[WriteOutcome]:
        """Several writes from one node. Conversation updates are guarded from the freshly synced local copy."""
        updates = [f.get("id") for t, f in items if t == "EmailThread.update" and f.get("id")]
        rows: dict[str, Any] | None = None
        if len(updates) > BATCH_FROM_COPY:
            if self.refresh:
                await self.refresh()
            rows = {k: v.model_dump(mode="json") for k, v in self.mirror.threads([m.id for m in self.ctx.mailboxes]).items()}
        # A TaskGroup, not gather: if the node dies (a crash, a cancel), every sibling write stops with it instead of
        # running on in the background against a closed run file.
        async with asyncio.TaskGroup() as tg:
            tasks = [tg.create_task(self.write(t, f, rows=rows)) for t, f in items]
        return [t.result() for t in tasks]

    # ── one write ────────────────────────────────────────────────────────────
    async def write(self, tool: str, fields: dict[str, Any], *, rows: dict[str, Any] | None = None) -> WriteOutcome:
        try:
            TOOL_ARGS[tool].model_validate(fields)
        except (KeyError, ValidationError) as e:
            return WriteOutcome(tool=tool, ok=False, error=f"invalid arguments: {str(e)[:400]}")
        row_id = fields.get("id")
        lock = self._locks.setdefault(row_id or f"new:{id(fields)}", asyncio.Lock())
        async with lock:
            refusal, before = await self._guard(tool, fields, rows)
            if refusal:
                return WriteOutcome(tool=tool, row_id=row_id, ok=False, guard=True, error=refusal)
            changed = {k: v for k, v in fields.items() if k != "id"}
            node = CURRENT_NODE.get()
            key = WriteOutbox.key(self.mcp_run_id(), node, tool, fields) if self.outbox is not None else None
            earlier = self.outbox.get(key) if self.outbox is not None and key is not None else None
            if earlier is not None and earlier.status == OutboxStatus.COMPLETED:
                # sent and recorded before (e.g. the node ran again after a resume): never a second writes.jsonl line
                return WriteOutcome(tool=tool, row_id=earlier.row_id or row_id, ok=True, receipt=earlier.receipt)
            send = _dry if self.dry_run else (lambda: self._send(tool, fields))
            try:
                if self.outbox is not None:
                    receipt = await self.outbox.execute(node_id=node, tool=tool, arguments=fields, row_id=row_id,
                                                        before=before, send=send)
                else:
                    receipt = await send()
            except Exception as e:  # noqa: BLE001 — the platform said no; the model reads why
                return WriteOutcome(tool=tool, row_id=row_id, ok=False, error=str(e)[:600])
            if isinstance(receipt, Deferred):
                return WriteOutcome(tool=tool, row_id=row_id, ok=False, uncertain=receipt.handle,
                                    error="an earlier attempt of this write may or may not have happened")
            if self.dry_run:
                rec = WriteRecord(tool=tool, entity=tool_entity(tool), row_id=row_id, fields=changed, before=before,
                                  dry_run=True, key=key)
                self.on_write(rec)
                return WriteOutcome(tool=tool, row_id=row_id, ok=True, record=rec)
            if tool_operation(tool) == "create":
                row_id = (_payload_row(receipt) or {}).get("id") if isinstance(receipt, dict) else None
                if row_id:
                    self.created.add(row_id)
            rec = WriteRecord(tool=tool, entity=tool_entity(tool), row_id=row_id, fields=changed, before=before, key=key)
            self.on_write(rec)
            if tool == "EmailThread.update" and row_id:
                self.mirror.apply_thread_update(row_id, changed, self._ours())
            return WriteOutcome(tool=tool, row_id=row_id, ok=True, record=rec, receipt=receipt)

    def mcp_run_id(self) -> str:
        return self.outbox.store.run_id if self.outbox is not None else self.ctx.run_id

    async def _guard(self, tool: str, fields: dict[str, Any], rows: dict[str, Any] | None) -> tuple[str | None, dict]:
        op, entity = tool_operation(tool), tool_entity(tool)
        scope = self.ctx.only_threads
        thread = fields.get("id") if entity == "EmailThread" else fields.get("thread_id")
        if scope is not None and thread and thread not in scope:     # a watcher run (Stage 8)
            return f"this run may change only conversation(s) {scope}; {thread} is outside it", {}
        if op == "create":
            return None, {}
        row_id = fields.get("id")
        if op == "delete":
            return (None if row_id in self.created else "only rows created by this run may be deleted"), {}
        if not row_id:
            return f"{tool} needs the row's id so it can be checked before changing it", {}
        if row_id in self.created:
            return None, {}
        row = rows.get(row_id) if rows is not None and entity == "EmailThread" else None
        if row is None:
            got = await self.mcp.call(f"{entity}.get", TOOL_ARGS[f"{entity}.get"].model_validate({"id": row_id}))
            if not got.ok:
                return f"could not read {entity} {row_id} to check it is ours: {got.error.message if got.error else ''}", {}
            row = ROW_MODELS.get(entity, Row).model_validate(_payload_row(got.data())).model_dump(mode="json")
        mailbox = row.get("mailbox_id")
        if mailbox not in self.ctx.mailbox_ids and row.get("created_by") != self.ctx.me.id:
            return f"{entity} {row_id} is not in our mailboxes and was not created by us, so it is not changed", {}
        return None, {k: row.get(k) for k in fields if k != "id"}

    async def _send(self, tool: str, fields: dict[str, Any]) -> Any:
        if tool_operation(tool) == "update" and any(v is None for k, v in fields.items() if k != "id"):
            body = {k: v for k, v in fields.items() if k != "id"}
            r = await asyncio.to_thread(self.rest.request, "PUT", f"/api/{tool_entity(tool)}/{fields['id']}", json_body=body)
            if r.status_code >= 300:
                raise WriteFailed(f"HTTP {r.status_code}: {r.text[:400]}")
            return r.json()
        out = await self.mcp.call(tool, TOOL_ARGS[tool].model_validate(fields))
        if not out.ok and out.error and out.error.kind == "timeout":
            raise UncertainWrite(out.error.message)
        if not out.ok:
            err = out.error
            detail = f" {json.dumps(err.data)[:400]}" if err and err.data else ""
            raise WriteFailed((err.message if err else out.text)[:600] + detail)
        return out.data()
