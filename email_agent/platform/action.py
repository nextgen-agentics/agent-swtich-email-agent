"""Action (no LLM): run one *tool* capability of the graph (one node = one call) — validated, guarded, recorded.
Every write goes through `writes.WritePath` (guard, dry run, outbox, send, record); Action keeps the reads and the
argument checks.

Order for every call:
  1. the tool must belong to the current goal's skill (or be offered to every skill) → else kind="not_in_skill"
  2. arguments must pass the tool's pydantic model (generated, or a local tool's)  → else kind="contract" (never sent)
  3. the safety guard (shared data: team 11 uses the same rows)   → else kind="guard"
       reads: allowed · create: allowed · delete: only rows this run created
       any other change: only rows in our mailboxes or created by our login
  4. dispatch (MCP, or a local tool) and parse the result into typed rows → a tool error is kind="tool_error";
     a result that does not match its contract is kind="contract". Rows from mailboxes that are not ours are
     left out before the model sees them (the platform leaks some: BUG-019, BUG-026).
  5. writes are recorded (WriteRecord, with the changed fields' values before the write)
With dry_run=True, steps 1–3 run as normal but writes are not sent: they are recorded as dry-run writes.
Large results go to the artifact store; the model sees a preview and the artifact id (S7).

Local tools (ours, not the platform's) live in LOCAL_TOOLS. Reads: `mailbox_overview`, `price_overview` (one call
gathers what would take dozens of reads; they read the local mailbox copy, `mailbox_store.py`, brought up to date once
per run before the first read, `sync.py`) and `recall_memory` (Stage 7: what we remember about a party, from the read
copy of AgentMemory). Whole-mailbox work is not done here: it is the graph's `judge_threads` (workers.py, flows.py).
The old loop's page-by-page tools (`conversation_digest`, `sort_threads`, `save_summaries`, `create_follow_ups`,
`record_price_agreements`) were removed after Stage 9: in 153 graph runs the planner never used them.
`remember_fact` (Stage 7) is the only way the agent adds a memory: code checks the party exists and skips a repeat of an
active memory with the same text before it writes AgentMemory.create through the same path.
"""

from __future__ import annotations

import asyncio
import json
import logging
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Callable

from pydantic import BaseModel, ValidationError

from email_agent.contracts import tool_args
from email_agent.contracts.agent import (
    ActionResult,
    ConversationOverview,
    DealLine,
    DealView,
    MailboxOverviewInput,
    PriceOverviewInput,
    RunContext,
    WriteRecord,
)
from email_agent.contracts.llm import ToolCall
from email_agent.contracts.mcp import tool_entity, tool_operation
from email_agent.contracts.memory import (
    MemoryScope,
    RecallMemoryInput,
    RememberFactInput,
    SourceRef,
)
from email_agent.contracts.mirror import SyncReport
from email_agent.contracts.platform import ROW_MODELS, AgentMemory, Deal, Row, list_page
from email_agent.contracts.tool_args import TOOL_ARGS
from email_agent.mailbox.store import MailboxStore
from email_agent.mailbox.sync import MailboxSync, SyncError
from email_agent.memory.store import LongTermMemory
from email_agent.memory.sync import MemorySync
from email_agent.platform.mcp_session import McpSession
from email_agent.platform.writes import WriteOutcome, WritePath
from email_agent.record.artifacts import Artifacts


@dataclass(frozen=True)
class LocalTool:
    model: type[BaseModel]
    description: str
    writes: bool = False


LOCAL_TOOLS: dict[str, LocalTool] = {
    # reads
    "mailbox_overview": LocalTool(
        MailboxOverviewInput,
        "Every conversation in our mailboxes with plain facts worked out from its messages: who wrote the newest "
        "real message and when, its text, whether we replied after it, our last message's id, and its current flag, "
        "importance, category and summary. Mirror copies (a message repeating an earlier one word for word) are not "
        "counted as replies."),
    "price_overview": LocalTool(
        PriceOverviewInput,
        "Conversations in our mailboxes that talk about prices, quotes or orders, a page at a time: references "
        "(quote / RFQ / order numbers), the price lines from OUR messages (qty @ unit price = total), the other "
        "side's newest messages, and the linked deal's stage, notes and line rates. Use next_offset for the next page."),
    "recall_memory": LocalTool(
        RecallMemoryInput,
        "What we remember (active, not expired): with party_id, that party's memories and the company-wide ones; "
        "with query, memories containing those words (the platform's own memory search does not work). The top 8, "
        "newest first, each with its id, category and content."),
    "remember_fact": LocalTool(
        RememberFactInput,
        "Remember one thing about a party (saved as agent memory linked to that party). Refused for an unknown "
        "party id; skipped (nothing written) when an active memory of that party already says exactly the same.",
        writes=True),
}
READ_OPS = {"list", "get", "search", "describe"}
DRY_RUN_NOTE = ("dry run: this write counts as done but was not sent, so later reads will not show it — "
                "do not repeat it")
ARTIFACT_OVER_CHARS = 40_000          # flash-lite has a large context; only truly large results are parked
DROP_FIELDS = {"body_html", "company_id", "updated_by", "_permissions", "_display", "_can_create"}


def _payload_row(data: Any) -> Any:
    return data["data"] if isinstance(data, dict) and isinstance(data.get("data"), dict) else data


def _compact(row: BaseModel) -> dict[str, Any]:
    d = row.model_dump(mode="json", exclude_none=True)
    d = {k: v for k, v in d.items() if k not in DROP_FIELDS and not k.startswith("_") and v not in ("", [], {})}
    if isinstance(d.get("body_text"), str) and len(d["body_text"]) > 1500:
        d["body_text"] = d["body_text"][:1500] + " …"
    return d


def _mailbox_of(row: BaseModel) -> str | None:
    return getattr(row, "mailbox_id", None) or (getattr(row, "model_extra", None) or {}).get("mailbox_id")


class Action:
    def __init__(self, mcp: McpSession, ctx: RunContext, artifacts: Artifacts | None, mirror: MailboxStore,
                 writes: WritePath | None, on_sync: Callable[[SyncReport], None] | None = None, full_sync: bool = False,
                 memory: LongTermMemory | None = None, mail_search: Any = None, memory_search: Any = None):
        self.mcp, self.ctx, self.artifacts, self.writes = mcp, ctx, artifacts, writes
        self.memory = memory
        self._memory_synced = False
        # hybrid search (Stage 9, search.py); set to None for the rest of the run if embedding fails
        self.mail_search, self.memory_search = mail_search, memory_search
        self.memory_vector_floor = 0.65             # Settings.memory_vector_floor, set by agent.py
        self.dry_run = writes.dry_run if writes else True
        self.mirror, self.on_sync, self.full_sync = mirror, on_sync, full_sync
        self._synced = False
        self._sync_lock = asyncio.Lock()
        self.created: set[str] = set()

    # ── entry point ──────────────────────────────────────────────────────────
    async def execute(self, call: ToolCall, allowed: list[str]) -> ActionResult:
        base = {"tool": call.name, "arguments": call.arguments}
        if call.name not in allowed:
            return ActionResult(**base, kind="not_in_skill", ok=False,
                                message=f"{call.name} is not a tool of this skill; use one of {allowed}")
        if call.name in LOCAL_TOOLS:
            return await self._local(call, allowed)
        model = TOOL_ARGS.get(call.name)
        if model is None:
            return ActionResult(**base, kind="contract", ok=False,
                                message=f"no arguments model for {call.name}; run scripts/repo/gen_tool_args.py")
        try:
            args = model.model_validate(call.arguments)
        except ValidationError as e:
            return ActionResult(**base, kind="contract", ok=False, message=f"invalid arguments: {_errors(e)}")
        if not (call.name.startswith("tools.") or tool_operation(call.name) in READ_OPS):
            return await self._write_call(base, call.name, args)
        outcome = await self.mcp.call(call.name, args)
        if not outcome.ok:
            err = outcome.error
            detail = f" {json.dumps(err.data)[:600]}" if err and err.data else ""
            return ActionResult(**base, kind="tool_error", ok=False, elapsed_ms=outcome.elapsed_ms,
                                message=(err.message if err else outcome.text)[:1000] + detail)
        try:
            rows, shown = self._parse(call.name, outcome.data())
        except ValidationError as e:
            return ActionResult(**base, kind="contract", ok=False, elapsed_ms=outcome.elapsed_ms,
                                message=f"the result of {call.name} did not match its contract: {_errors(e)}")
        if tool_operation(call.name) == "get" and rows and not self._ours(rows[0]):
            return ActionResult(**base, kind="guard", ok=False, elapsed_ms=outcome.elapsed_ms,
                                message=f"{tool_entity(call.name)} {call.arguments.get('id')} belongs to a mailbox "
                                        "that is not ours; it is not shown (refuse with not_our_mailbox if the "
                                        "request is about that mailbox)")
        return self._result(base, shown, rows, None, outcome.elapsed_ms)

    async def _write_call(self, base: dict, tool: str, args: BaseModel) -> ActionResult:
        """A write the model asked for directly (one row): through WritePath."""
        if self.writes is None:
            return ActionResult(**base, kind="guard", ok=False, message="this run cannot write")
        out = await self.writes.write(tool, args.model_dump(exclude_unset=True, mode="json"))
        if out.uncertain:
            return ActionResult(**base, kind="tool_error", ok=False, message=out.error or "", uncertain=[out.uncertain])
        if not out.ok:
            return ActionResult(**base, kind="guard" if out.guard else "tool_error", ok=False, message=out.error or "")
        shown: Any = {"dry_run": True, "would_set": out.record.fields, "row_id": out.row_id, "note": DRY_RUN_NOTE} \
            if out.record and out.record.dry_run else (out.receipt if out.receipt is not None else {"row_id": out.row_id})
        return self._result(base, shown, [], out.record, 0.0)

    def _ours(self, row: BaseModel) -> bool:
        """Rows without a mailbox (memories, deals …) are not mailbox-scoped; mail rows must be in our working set."""
        mailbox = _mailbox_of(row)
        return mailbox is None or mailbox in self.ctx.mailbox_ids

    # ── results ──────────────────────────────────────────────────────────────
    def _parse(self, name: str, data: Any) -> tuple[list[BaseModel], Any]:
        """Typed rows for entity tools; the generated *Output* model for the others."""
        entity, op = tool_entity(name), tool_operation(name)
        row_model = ROW_MODELS.get(entity, Row)
        if name.startswith("tools."):
            out_model = getattr(tool_args, TOOL_ARGS[name].__name__.replace("Input", "Output"), None)
            parsed = out_model.model_validate(data) if out_model else data
            return [], (parsed.model_dump(mode="json", exclude_none=True) if out_model else data)
        if op == "list":
            page = list_page(row_model).model_validate(data)
            rows = [r for r in page.data if self._ours(r)]
            shown: dict[str, Any] = {"total": page.total, "rows": [_compact(r) for r in rows]}
            if len(rows) < len(page.data):
                shown["left_out"] = f"{len(page.data) - len(rows)} row(s) from mailboxes that are not ours"
            return list(rows), shown
        row = row_model.model_validate(_payload_row(data))
        return [row], _compact(row)

    def _result(self, base: dict, shown: Any, rows: Sequence[Any], write: WriteRecord | None,
                ms: float) -> ActionResult:
        text = json.dumps(shown, ensure_ascii=False, default=str)
        art = None
        if len(text) > ARTIFACT_OVER_CHARS and self.artifacts is not None:
            art = self.artifacts.put(text, source=base["tool"])
            text = f"[result is {len(text)} characters, saved as {art}] preview: {text[:2000]} …"
        return ActionResult(**base, kind="ok", ok=True, preview=text, rows=len(rows) if rows else None,
                            artifact_id=art, write=write, elapsed_ms=ms)

    # ── local tools ──────────────────────────────────────────────────────────
    async def _local(self, call: ToolCall, allowed: list[str] | None = None) -> ActionResult:
        base = {"tool": call.name, "arguments": call.arguments}
        try:
            args = LOCAL_TOOLS[call.name].model.model_validate(call.arguments)
        except ValidationError as e:
            return ActionResult(**base, kind="contract", ok=False, message=f"invalid arguments: {_errors(e)}")
        if isinstance(args, MailboxOverviewInput):
            rows = await self.mailbox_overview(args)
            shown = [r.model_dump(mode="json", exclude_none=True) for r in rows]
            return self._result(base, {"conversations": len(rows), "items": shown}, rows, None, 0.0)
        if isinstance(args, PriceOverviewInput):
            return await self._price_page(base, args)
        if isinstance(args, RecallMemoryInput):
            return await self._recall(base, args)
        assert isinstance(args, RememberFactInput)
        return await self._remember(base, args)

    # ── local reads ──────────────────────────────────────────────────────────
    async def _ensure_synced(self) -> None:
        """Bring the local mailbox copy up to date once per run, before the first mailbox read."""
        async with self._sync_lock:                  # graph nodes run in parallel: sync once
            if self._synced:
                return
            await self._sync()

    async def refresh(self) -> None:
        """Sync again now (before a batch of writes is guarded from the local copy)."""
        async with self._sync_lock:
            await self._sync()

    async def _sync(self) -> None:
        report = await MailboxSync(self.mcp, self.mirror, self.ctx.instance, self.ctx.mailboxes,
                                   self._ours_addresses()).run(full=self.full_sync)
        self._synced = True
        if self.mail_search is not None:            # new and changed messages get their vectors (Stage 9)
            try:
                report.embedded = (await self.mail_search.update(self._mailbox_ids))["embedded"]
            except Exception as e:  # noqa: BLE001 — search falls back to full text; the run goes on
                report.search_note = f"meaning-based search is off for this run: {type(e).__name__}: {str(e)[:200]}"
                self.mail_search = None
        if self.on_sync:
            self.on_sync(report)

    @property
    def _mailbox_ids(self) -> list[str]:
        return [m.id for m in self.ctx.mailboxes]

    def _ours_addresses(self) -> set[str]:
        return set(self.ctx.our_addresses) or {m.email for m in self.ctx.mailboxes}

    def _address(self, mailbox_id: str | None) -> str | None:
        return {m.id: m.email for m in self.ctx.mailboxes}.get(mailbox_id or "")

    @staticmethod
    def _matches(search: str | None, *texts: str | None) -> bool:
        if not search:
            return True
        hay = " ".join(x or "" for x in texts).lower()
        return all(word in hay for word in search.lower().split())

    async def _price_page(self, base: dict, args: PriceOverviewInput) -> ActionResult:
        await self._ensure_synced()
        ids = self.mirror.candidate_ids(self._mailbox_ids, args.search)
        views = [(th, v.model_copy(update={"mailbox": self._address(mb)}))
                 for th, mb, v in self.mirror.price_views(self._mailbox_ids, ids=ids)
                 if self._matches(args.search, v.subject, v.counterpart, " ".join(v.references),
                                  *(m.text for m in v.their_messages))]
        page = views[args.offset:args.offset + args.limit]
        deals: dict[str, DealView | None] = {}
        for th, _ in page:                                   # the linked deal, read once per deal
            if th.deal_id and th.deal_id not in deals:
                deals[th.deal_id] = await self._deal(th.deal_id)
        items = [v.model_copy(update={"deal": deals.get(th.deal_id or "")}) for th, v in page]
        return self._page(base, items, args.offset, args.limit, total=len(views))

    async def _deal(self, deal_id: str) -> DealView | None:
        out = await self.mcp.call("Deal.get", TOOL_ARGS["Deal.get"].model_validate({"id": deal_id}))
        if not out.ok:
            return None
        deal = Deal.model_validate(_payload_row(out.data()))
        lines = [DealLine.model_validate(x) for x in (deal.model_extra or {}).get("products") or [] if isinstance(x, dict)]
        return DealView(id=deal.id, title=deal.title, stage=deal.stage, value=deal.value, currency=deal.currency,
                        notes=(deal.notes or "")[:300] or None, lines=lines)

    def _page(self, base: dict, items: Sequence[BaseModel], offset: int, limit: int, total: int | None = None) -> ActionResult:
        """`items` is the whole list (sliced here), or — when `total` is given — the page already cut."""
        page = items[offset:offset + limit] if total is None else items
        total = len(items) if total is None else total
        nxt = offset + limit if offset + limit < total else None
        shown = {"total": total, "offset": offset, "next_offset": nxt,
                 "items": [x.model_dump(mode="json", exclude_none=True) for x in page]}
        return self._result(base, shown, page, None, 0.0)

    # ── long-term memory (Stage 7) ───────────────────────────────────────────
    async def _memory_sync(self) -> MemorySync:
        """The read copy of AgentMemory, brought up to date once per run (one call when nothing changed)."""
        if self.memory is None:
            raise RuntimeError("this run has no long-term memory store")
        sync = MemorySync(self.mcp, self.memory)
        async with self._sync_lock:
            if not self._memory_synced:
                await sync.run()
                self._memory_synced = True
                if self.memory_search is not None:
                    try:
                        await self.memory_search.update()
                    except Exception:  # noqa: BLE001 — recall falls back to full text
                        logging.getLogger(__name__).warning("memory embeddings failed; full-text recall only",
                                                            exc_info=True)
                        self.memory_search = None
        return sync

    async def _recall(self, base: dict, args: RecallMemoryInput) -> ActionResult:
        if self.memory is None:
            return ActionResult(**base, kind="tool_error", ok=False, message="this run has no long-term memory store")
        try:
            sync = await self._memory_sync()
            if args.party_id:                      # the party's rows live, so a deleted or switched-off one is seen
                await sync.party(args.party_id)
        except SyncError as e:
            return ActionResult(**base, kind="tool_error", ok=False, message=str(e)[:1000])
        vector = None
        if self.memory_search is not None and args.query:          # hybrid (Stage 9): meaning + full text
            try:
                vector = await self.memory_search.scores(args.query)
            except Exception:  # noqa: BLE001
                logging.getLogger(__name__).warning("memory search failed; full-text recall only", exc_info=True)
        records, total = self.memory.recall(MemoryScope(instance=self.ctx.instance, party_id=args.party_id), args.query,
                                            vector=vector, vector_floor=self.memory_vector_floor)
        shown: dict[str, Any] = {"party_id": args.party_id, "query": args.query, "remembered": total,
                                 "memories": [r.shown() for r in records]}
        if total > len(records):
            shown["note"] = f"the top {len(records)} of {total}"
        return self._result(base, shown, records, None, 0.0)

    @staticmethod
    def _same_text(a: str, b: str) -> bool:
        def norm(x: str) -> str:
            return " ".join(x.lower().split()).rstrip(" .")
        return norm(a) == norm(b)

    async def _remember(self, base: dict, args: RememberFactInput) -> ActionResult:
        party = await self.mcp.call("Party.get", TOOL_ARGS["Party.get"].model_validate({"id": args.party_id}))
        if not party.ok:
            return ActionResult(**base, kind="tool_error", ok=False, message=(
                f"no party with id {args.party_id}: find it with Party.list first (or refuse with unknown_record)"))
        if args.source_message_id:
            await self._ensure_synced()
            if self.mirror.message_thread(args.source_message_id, self._mailbox_ids) is None:
                return ActionResult(**base, kind="contract", ok=False, message=(
                    f"message {args.source_message_id} is not in our mailboxes"))
        live: list[AgentMemory] = []
        if self.memory is not None:
            try:
                live = await MemorySync(self.mcp, self.memory).party(args.party_id)
            except SyncError as e:
                return ActionResult(**base, kind="tool_error", ok=False, message=str(e)[:1000])
        same = next((m for m in live if m.is_active and self._same_text(m.content or "", args.content)), None)
        if same is not None:
            return self._result(base, {"already_remembered": True, "id": same.id, "category": same.category,
                                       "content": same.content, "note": "nothing written"}, [], None, 0.0)
        fields = {"party_id": args.party_id, "category": args.category, "content": args.content,
                  "source": "extracted" if args.source_message_id else "manual", "is_active": True}
        out = await self._write("AgentMemory.create", fields)
        if out.uncertain:
            return ActionResult(**base, kind="tool_error", ok=False, message=out.error or "", uncertain=[out.uncertain])
        if not out.ok:
            return ActionResult(**base, kind="guard" if out.guard else "tool_error", ok=False, message=out.error or "")
        if out.record and not out.record.dry_run and out.row_id and self.memory is not None:
            now = datetime.now(timezone.utc).isoformat()
            origin = (SourceRef(uri=f"message:{args.source_message_id}", author=self.ctx.me.email)
                      if args.source_message_id else
                      SourceRef(uri=f"run:{self.ctx.run_id}/request", author=self.ctx.me.email,
                                excerpt=self.ctx.request[:300]))
            self.memory.upsert_platform([AgentMemory(id=out.row_id, created_at=now, updated_at=now,
                                                     created_by=self.ctx.me.id, **fields)], {out.row_id: [origin]})
        shown: Any = {"dry_run": True, "would_set": out.record.fields, "note": DRY_RUN_NOTE} \
            if out.record and out.record.dry_run else {"remembered": True, "id": out.row_id, **fields}
        return self._result(base, shown, [], out.record, 0.0)

    # ── local batch writes ───────────────────────────────────────────────────
    async def _write(self, tool: str, fields: dict[str, Any]) -> WriteOutcome:
        """One platform write of a batch tool: through WritePath (guard, dry run, outbox, send, record)."""
        if self.writes is None:
            return WriteOutcome(tool=tool, ok=False, guard=True, error="this run cannot write")
        return await self.writes.write(tool, fields)

    async def mailbox_overview(self, args: MailboxOverviewInput) -> list[ConversationOverview]:
        await self._ensure_synced()
        return [ov.model_copy(update={"mailbox": self._address(mb)})
                for mb, ov in self.mirror.overviews(self._mailbox_ids, folder=args.folder,
                                                    only_waiting_on_us=args.only_waiting_on_us)]


def _errors(e: ValidationError) -> str:
    return "; ".join(f"{'.'.join(str(p) for p in err['loc']) or 'arguments'}: {err['msg']}" for err in e.errors()[:8])
