"""Action (no LLM): run the one tool call Decision chose — validated, guarded, recorded.

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

Local tools (ours, not the platform's) live in LOCAL_TOOLS. Reads: `mailbox_overview`, `conversation_digest`,
`price_overview` (thin model, thick tools: one call gathers what would take dozens of reads). `refuse` is offered
with every skill and changes nothing. Batch writes (`record_price_agreements`, `save_summaries`, `sort_threads`,
`create_follow_ups`) send each row through the same contract → guard → dry run → record steps as a model's call.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any, Callable

from pydantic import BaseModel, ValidationError

from email_agent.artifacts import Artifacts
from email_agent.contracts import tool_args
from email_agent.contracts.agent import (ActionResult, ConversationDigestInput, ConversationOverview,
                                         CreateFollowUpsInput, DealLine, DealView, MailboxOverviewInput,
                                         PriceOverviewInput, RecordPriceAgreementsInput,
                                         RefuseInput, RunContext, SaveSummariesInput, SortThreadsInput, WriteRecord)
from email_agent.contracts.llm import ToolCall, ToolSpec
from email_agent.contracts.mcp import tool_entity, tool_operation
from email_agent.contracts.platform import (ROW_MODELS, AgentMemory, Deal, EmailMessage, EmailReminder, EmailThread,
                                            ListPage, Row)
from email_agent.contracts.tool_args import TOOL_ARGS
from email_agent.conversation import digest, overview, price_view
from email_agent.mcp_session import McpSession


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
    "conversation_digest": LocalTool(
        ConversationDigestInput,
        "Conversations in our mailboxes, a page at a time, each with its newest real messages (text shortened), its "
        "current summary and whether that summary is current. Use next_offset for the next page."),
    "price_overview": LocalTool(
        PriceOverviewInput,
        "Conversations in our mailboxes that talk about prices, quotes or orders, a page at a time: references "
        "(quote / RFQ / order numbers), the price lines from OUR messages (qty @ unit price = total), the other "
        "side's newest messages, and the linked deal's stage, notes and line rates. Use next_offset for the next page."),
    "refuse": LocalTool(
        RefuseInput,
        "Decline the current goal and change nothing: use it when the request needs another app's data, is about a "
        "mailbox that is not ours, asks for an action this agent must not take (sending, deleting shared mail), or "
        "when the mail does not support an answer. Never guess instead."),
    # writes (each row is guarded and recorded on its own)
    "record_price_agreements": LocalTool(
        RecordPriceAgreementsInput,
        "Save every agreed price found, in one call: each becomes a fact in agent memory (AgentMemory, linked to the "
        "other side's party) and its conversation is starred. Use the figures from our quote line.", writes=True),
    "save_summaries": LocalTool(
        SaveSummariesInput,
        "Write the summary of each listed conversation (and set its summary date to today). Changes nothing else.",
        writes=True),
    "sort_threads": LocalTool(
        SortThreadsInput,
        "Set importance and category tab on each listed conversation. Changes nothing else.", writes=True),
    "create_follow_ups": LocalTool(
        CreateFollowUpsInput,
        "Create a 'remind me if no reply' follow-up reminder on each listed conversation. A conversation that "
        "already has an unfired follow-up reminder is skipped.", writes=True),
}
ALWAYS_OFFERED = ["refuse"]          # every skill may decline
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


def tool_specs(names: list[str], seat_tools: dict[str, Any]) -> list[ToolSpec]:
    """The tools offered to Decision for one skill: live MCP schemas, or our local tools' models."""
    specs = []
    for n in names:
        if n in LOCAL_TOOLS:
            t = LOCAL_TOOLS[n]
            specs.append(ToolSpec(name=n, description=t.description, parameters=t.model.model_json_schema()))
        else:
            t = seat_tools[n]
            specs.append(ToolSpec(name=n, description=t.description or "", parameters=t.input_schema))
    return specs


class Action:
    def __init__(self, mcp: McpSession, ctx: RunContext, artifacts: Artifacts,
                 on_write: Callable[[WriteRecord], None], dry_run: bool = False):
        self.mcp, self.ctx, self.artifacts, self.on_write = mcp, ctx, artifacts, on_write
        self.dry_run = dry_run
        self.created: set[str] = set()
        # A page read by conversation_digest in a skill that saves page by page (sort, summaries) must be saved
        # before the next page is read: a model once read three pages, saved one, and said all were done.
        self.page_pending: tuple[int, list[str]] | None = None   # (offset, the batch tools that close it)

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
                                message=f"no arguments model for {call.name}; run scripts/gen_tool_args.py")
        try:
            args = model.model_validate(call.arguments)
        except ValidationError as e:
            return ActionResult(**base, kind="contract", ok=False, message=f"invalid arguments: {_errors(e)}")
        refusal, before = await self._guard(call.name, args)
        if refusal:
            return ActionResult(**base, kind="guard", ok=False, message=refusal)
        if self.dry_run and not (call.name.startswith("tools.") or tool_operation(call.name) in READ_OPS):
            fields = args.model_dump(exclude_unset=True, mode="json")
            rec = WriteRecord(tool=call.name, entity=tool_entity(call.name), row_id=fields.get("id"),
                              fields={k: v for k, v in fields.items() if k != "id"}, before=before, dry_run=True)
            self.on_write(rec)
            return self._result(base, {"dry_run": True, "would_set": rec.fields, "row_id": rec.row_id,
                                       "note": DRY_RUN_NOTE}, [], rec, 0.0)
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
        write = self._record(call.name, args, rows, before)
        return self._result(base, shown, rows, write, outcome.elapsed_ms)

    def _ours(self, row: BaseModel) -> bool:
        """Rows without a mailbox (memories, deals …) are not mailbox-scoped; mail rows must be in our working set."""
        mailbox = _mailbox_of(row)
        return mailbox is None or mailbox in self.ctx.mailbox_ids

    # ── guard ────────────────────────────────────────────────────────────────
    async def _guard(self, name: str, args: BaseModel) -> tuple[str | None, dict[str, Any]]:
        op, entity = tool_operation(name), tool_entity(name)
        if name.startswith("tools.") or op in READ_OPS or op == "create":
            return None, {}
        fields = args.model_dump(exclude_unset=True, mode="json")
        row_id = fields.get("id")
        if op == "delete":
            return (None if row_id in self.created else "only rows created by this run may be deleted"), {}
        if not row_id:
            return f"{name} needs the row's id so it can be checked before changing it", {}
        if row_id in self.created:
            return None, {}
        get_args = TOOL_ARGS[f"{entity}.get"].model_validate({"id": row_id}) if f"{entity}.get" in TOOL_ARGS \
            else {"id": row_id}
        got = await self.mcp.call(f"{entity}.get", get_args)
        if not got.ok:
            return f"could not read {entity} {row_id} to check it is ours: {got.error.message if got.error else ''}", {}
        row = ROW_MODELS.get(entity, Row).model_validate(_payload_row(got.data()))
        mailbox = _mailbox_of(row)
        if mailbox not in self.ctx.mailbox_ids and row.created_by != self.ctx.me.id:
            return f"{entity} {row_id} is not in our mailboxes and was not created by us, so it is not changed", {}
        dumped = row.model_dump(mode="json")
        return None, {k: dumped.get(k) for k in fields if k != "id"}

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
            page = ListPage[row_model].model_validate(data)
            rows = [r for r in page.data if self._ours(r)]
            shown: dict[str, Any] = {"total": page.total, "rows": [_compact(r) for r in rows]}
            if len(rows) < len(page.data):
                shown["left_out"] = f"{len(page.data) - len(rows)} row(s) from mailboxes that are not ours"
            return list(rows), shown
        row = row_model.model_validate(_payload_row(data))
        return [row], _compact(row)

    def _record(self, name: str, args: BaseModel, rows: list[BaseModel], before: dict[str, Any]) -> WriteRecord | None:
        op = tool_operation(name)
        if name.startswith("tools.") or op in READ_OPS:
            return None
        fields = args.model_dump(exclude_unset=True, mode="json")
        row_id = fields.get("id") or (getattr(rows[0], "id", None) if rows else None)
        if op == "create" and row_id:
            self.created.add(row_id)
        rec = WriteRecord(tool=name, entity=tool_entity(name), row_id=row_id,
                          fields={k: v for k, v in fields.items() if k != "id"}, before=before)
        self.on_write(rec)
        return rec

    def _result(self, base: dict, shown: Any, rows: list, write: WriteRecord | None, ms: float) -> ActionResult:
        text = json.dumps(shown, ensure_ascii=False, default=str)
        art = None
        if len(text) > ARTIFACT_OVER_CHARS:
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
        if isinstance(args, RefuseInput):
            return ActionResult(**base, kind="ok", ok=True, preview=f"Refused ({args.reason}): {args.explanation}")
        if isinstance(args, MailboxOverviewInput):
            rows = await self.mailbox_overview(args)
            shown = [r.model_dump(mode="json", exclude_none=True) for r in rows]
            return self._result(base, {"conversations": len(rows), "items": shown}, rows, None, 0.0)
        if isinstance(args, ConversationDigestInput):
            if self.page_pending and args.offset != self.page_pending[0]:
                off, savers = self.page_pending
                return ActionResult(**base, kind="guard", ok=False, message=(
                    f"the page at offset {off} is not saved yet: call {' or '.join(savers)} for it first"
                    + (" (an empty items list if nothing on that page needs a change)" if "sort_threads" in savers
                       else "") + ", then read the next page"))
            items = await self._digests(args)
            savers = [t for t in ("sort_threads", "save_summaries") if t in (allowed or [])]
            if savers and items[args.offset:args.offset + args.limit]:
                self.page_pending = (args.offset, savers)
            return self._page(base, items, args.offset, args.limit)
        if isinstance(args, PriceOverviewInput):
            return await self._price_page(base, args)
        if isinstance(args, (SortThreadsInput, SaveSummariesInput)):
            self.page_pending = None                  # this page is saved (or explicitly needs nothing)
            if not args.items:
                return ActionResult(**base, kind="ok", ok=True, preview="nothing on this page needed a change")
        batch = {RecordPriceAgreementsInput: self._record_prices, SaveSummariesInput: self._save_summaries,
                 SortThreadsInput: self._sort_threads, CreateFollowUpsInput: self._create_follow_ups}[type(args)]
        outcomes, writes = await batch(args)
        failed = [o for o in outcomes if not o.get("ok")]
        shown = {"done": len(outcomes) - len(failed), "failed": len(failed), "items": outcomes}
        if self.dry_run:
            shown["dry_run"] = DRY_RUN_NOTE
        result = self._result(base, shown, [], None, 0.0)
        return result.model_copy(update={"writes": writes})

    # ── local reads ──────────────────────────────────────────────────────────
    async def _listing(self) -> tuple[list[EmailThread], list[EmailMessage]]:
        """Our working mailboxes' conversations and every message we can read (reads, MCP)."""
        listing = {}
        for tool, model in (("EmailThread.list", EmailThread), ("EmailMessage.list", EmailMessage)):
            out = await self.mcp.call(tool, TOOL_ARGS[tool].model_validate({"limit": 1000}))
            if not out.ok:
                raise RuntimeError(f"{tool} failed: {out.error.message if out.error else out.text}")
            listing[tool] = ListPage[model].model_validate(out.data()).data
        threads = [t for t in listing["EmailThread.list"] if t.mailbox_id in self.ctx.mailbox_ids]
        return threads, listing["EmailMessage.list"]

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

    async def _digests(self, args: ConversationDigestInput) -> list[BaseModel]:
        threads, messages = await self._listing()
        out = []
        for th in threads:
            d = digest(th, messages, self._ours_addresses()).model_copy(update={"mailbox": self._address(th.mailbox_id)})
            if args.only_stale_summaries and d.summary_current:
                continue
            if self._matches(args.search, d.subject, d.counterpart, *(m.text for m in d.messages)):
                out.append(d)
        return sorted(out, key=lambda d: d.newest_real_at or "", reverse=True)

    async def _price_page(self, base: dict, args: PriceOverviewInput) -> ActionResult:
        threads, messages = await self._listing()
        views = []
        for th in threads:
            v = price_view(th, messages, self._ours_addresses())
            if v and self._matches(args.search, v.subject, v.counterpart, " ".join(v.references),
                                   *(m.text for m in v.their_messages)):
                views.append((th, v.model_copy(update={"mailbox": self._address(th.mailbox_id)})))
        views.sort(key=lambda tv: tv[1].our_last_at or "", reverse=True)
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

    def _page(self, base: dict, items: list[BaseModel], offset: int, limit: int, total: int | None = None) -> ActionResult:
        """`items` is the whole list (sliced here), or — when `total` is given — the page already cut."""
        page = items[offset:offset + limit] if total is None else items
        total = len(items) if total is None else total
        nxt = offset + limit if offset + limit < total else None
        shown = {"total": total, "offset": offset, "next_offset": nxt,
                 "items": [x.model_dump(mode="json", exclude_none=True) for x in page]}
        return self._result(base, shown, page, None, 0.0)

    # ── local batch writes ───────────────────────────────────────────────────
    async def _write(self, tool: str, fields: dict[str, Any]) -> tuple[WriteRecord | None, str | None]:
        """One platform write through the same steps as a model's call: contract, guard, dry run, record."""
        try:
            args = TOOL_ARGS[tool].model_validate(fields)
        except ValidationError as e:
            return None, f"invalid arguments: {_errors(e)}"
        refusal, before = await self._guard(tool, args)
        if refusal:
            return None, refusal
        if self.dry_run:
            sent = args.model_dump(exclude_unset=True, mode="json")
            rec = WriteRecord(tool=tool, entity=tool_entity(tool), row_id=sent.get("id"),
                              fields={k: v for k, v in sent.items() if k != "id"}, before=before, dry_run=True)
            self.on_write(rec)
            return rec, None
        out = await self.mcp.call(tool, args)
        if not out.ok:
            return None, (out.error.message if out.error else out.text)[:500]
        try:
            rows, _ = self._parse(tool, out.data())
        except ValidationError as e:
            return None, f"the result did not match its contract: {_errors(e)}"
        return self._record(tool, args, rows, before), None

    async def _our_threads(self) -> dict[str, EmailThread]:
        threads, _ = await self._listing()
        return {th.id: th for th in threads}

    async def _record_prices(self, args: RecordPriceAgreementsInput) -> tuple[list[dict], list[WriteRecord]]:
        """Per agreement: one AgentMemory fact whose first line is machine-readable (the harness parses it; BUG-014
        means memory text cannot be searched, so the check reads our rows by run id), then star the conversation."""
        threads, outcomes, writes = await self._our_threads(), [], []
        for a in args.items:
            th = threads.get(a.thread_id)
            if th is None:
                outcomes.append({"thread_id": a.thread_id, "ok": False, "error": "not a conversation in our mailboxes"})
                continue
            first = (f"price-agreement | run={self.ctx.run_id} | thread={a.thread_id} | message={a.agreement_message_id}"
                     f" | ref={a.reference or '-'} | qty={a.quantity:g} | unit={a.unit_price:.2f} | total={a.total:.2f}"
                     f" | currency={a.currency} | agreed_on={a.agreed_on}")
            text = (f"Agreed price: {a.item}, {a.quantity:g} @ {a.unit_price:,.2f} {a.currency} = {a.total:,.2f} "
                    f"{a.currency}{f' (ref {a.reference})' if a.reference else ''}, agreed on {a.agreed_on}."
                    + (f" Record check: {a.record_check}" if a.record_check else ""))
            mem, err = await self._write("AgentMemory.create", {
                "category": "fact", "content": f"{first}\n{text}", "party_id": a.party_id or th.party_id,
                "source": "extracted", "is_active": True})
            if err:
                outcomes.append({"thread_id": a.thread_id, "ok": False, "error": f"memory not saved: {err}"})
                continue
            writes.append(mem)
            star = None
            if not th.is_starred:
                star, err = await self._write("EmailThread.update", {"id": a.thread_id, "is_starred": True})
                if star:
                    writes.append(star)
            outcomes.append({"thread_id": a.thread_id, "ok": err is None, "memory": mem.row_id,
                             "starred": "already" if th.is_starred else bool(star), **({"error": err} if err else {})})
        return outcomes, writes

    async def _thread_updates(self, items: list[tuple[str, dict[str, Any]]]) -> tuple[list[dict], list[WriteRecord]]:
        threads, outcomes, writes = await self._our_threads(), [], []
        for thread_id, fields in items:
            if thread_id not in threads:
                outcomes.append({"thread_id": thread_id, "ok": False, "error": "not a conversation in our mailboxes"})
                continue
            rec, err = await self._write("EmailThread.update", {"id": thread_id, **fields})
            if rec:
                writes.append(rec)
            outcomes.append({"thread_id": thread_id, "ok": err is None, **({"error": err} if err else {})})
        return outcomes, writes

    async def _save_summaries(self, args: SaveSummariesInput) -> tuple[list[dict], list[WriteRecord]]:
        today = self.ctx.today.isoformat()
        return await self._thread_updates([(i.thread_id, {"summary": i.summary, "summary_updated_at": today})
                                           for i in args.items])

    async def _sort_threads(self, args: SortThreadsInput) -> tuple[list[dict], list[WriteRecord]]:
        return await self._thread_updates([(i.thread_id, {"importance": i.importance,
                                                          "split_category": i.split_category}) for i in args.items])

    async def _create_follow_ups(self, args: CreateFollowUpsInput) -> tuple[list[dict], list[WriteRecord]]:
        threads, outcomes, writes = await self._our_threads(), [], []
        for i in args.items:
            if i.thread_id not in threads:
                outcomes.append({"thread_id": i.thread_id, "ok": False, "error": "not a conversation in our mailboxes"})
                continue
            if i.remind_at <= self.ctx.today:
                outcomes.append({"thread_id": i.thread_id, "ok": False, "error": "remind_at must be after today"})
                continue
            out = await self.mcp.call("EmailReminder.list", TOOL_ARGS["EmailReminder.list"].model_validate(
                {"thread_id": i.thread_id, "limit": 100}))
            existing = ListPage[EmailReminder].model_validate(out.data()).data if out.ok else []
            if any(r.type == "follow_up" and not r.is_fired and r.thread_id == i.thread_id for r in existing):
                outcomes.append({"thread_id": i.thread_id, "ok": True, "skipped": "already has a follow-up reminder"})
                continue
            rec, err = await self._write("EmailReminder.create", {
                "company_id": self.ctx.me.company_id,           # the schema requires it; our login's company
                "thread_id": i.thread_id, "message_id": i.message_id, "type": "follow_up", "condition": "no_reply",
                "remind_at": i.remind_at.isoformat(), "note": i.note})
            if rec:
                writes.append(rec)
            outcomes.append({"thread_id": i.thread_id, "ok": err is None, **({"error": err} if err else {})})
        return outcomes, writes

    async def mailbox_overview(self, args: MailboxOverviewInput) -> list[ConversationOverview]:
        threads, messages = await self._listing()
        rows = [overview(t, messages, self._ours_addresses()).model_copy(update={"mailbox": self._address(t.mailbox_id)})
                for t in threads]
        if args.folder != "all":
            rows = [r for r in rows if r.folder == args.folder]
        if args.only_waiting_on_us:
            rows = [r for r in rows if r.has_inbound and r.newest_real_from == "them" and not r.we_replied_after_them]
        return sorted(rows, key=lambda r: r.newest_real_at or "", reverse=True)


def _errors(e: ValidationError) -> str:
    return "; ".join(f"{'.'.join(str(p) for p in err['loc']) or 'arguments'}: {err['msg']}" for err in e.errors()[:8])
