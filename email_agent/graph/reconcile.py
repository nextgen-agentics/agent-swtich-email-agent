"""Reconcile (Revision 12, Stage 6): settle every write a crash or an unanswered call left "started", by reading the
live platform; then make writes.jsonl match the outbox and let the parked nodes run again.

Adapted from S17 `events/outbox.py` (an uncertain action is settled by reading its target, never by sending it again
blindly). For each outbox record still `started`:

  EmailThread.update    the live row holds every field we sent        → happened  (completed; recorded once)
                        the live row holds every field's `before`      → not_sent  (dropped; the node sends it again)
                        anything else                                  → changed   (failed: someone else changed the row)
  AgentMemory.create    a memory of the same party with exactly our content, created after the write started
                        → happened (its id becomes the receipt); none → not_sent
  EmailReminder.create  a reminder on the same thread and message, same type and day, created after the write started
                        → happened; none → not_sent
  any other tool, or a row that cannot be read → unreadable: left uncertain; its node keeps waiting (check it by hand)
  a dry run: nothing was ever sent → dry_run (dropped; recorded again on rerun)

Then every completed write that writes.jsonl lacks (the crash came between the platform's answer and the record) is
added, so `undo_run.py` takes it back too; and every node waiting on "outbox.reconcile" whose writes are all settled goes
back to pending, to run again: a completed write reuses its receipt, a dropped one is sent.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any

from email_agent.contracts.agent import RunContext, WriteRecord
from email_agent.contracts.graph import (
    OutboxRecord,
    OutboxStatus,
    ReconcileItem,
    ReconcileReport,
    ReconcileVerdict,
)
from email_agent.contracts.mcp import tool_entity, tool_operation
from email_agent.contracts.tool_args import TOOL_ARGS
from email_agent.graph.outbox import RECONCILE, WriteOutbox
from email_agent.graph.store import RunStore
from email_agent.platform.mcp_session import McpSession
from email_agent.record.runlog import RunLog

CLOCK_SLACK = timedelta(minutes=5)          # our clock vs the server's, when matching "created after the write started"


async def reconcile(*, store: RunStore, mcp: McpSession, ctx: RunContext, log: RunLog, dry_run: bool) -> ReconcileReport:
    outbox = WriteOutbox(store)
    items = [await _settle(rec, outbox, mcp, ctx, dry_run) for rec in outbox.records(OutboxStatus.STARTED)]
    backfilled = _backfill(outbox, log)
    released = []
    for node in store.waiting():
        if node.wait is None or node.wait.event_type != RECONCILE:
            continue
        keys = node.wait.metadata.get("keys") or [node.wait.handle]
        if any((r := outbox.get(k)) is not None and r.status == OutboxStatus.STARTED for k in keys):
            continue                                   # still uncertain: the node keeps waiting
        if store.release_waiting(node.wait.handle, RECONCILE, note="uncertain writes settled against the live rows"):
            released.append(node.id)
    return ReconcileReport(items=items, backfilled=backfilled, released=released)


async def _settle(rec: OutboxRecord, outbox: WriteOutbox, mcp: McpSession, ctx: RunContext,
                  dry_run: bool) -> ReconcileItem:
    base = {"key": rec.key, "node_id": rec.node_id, "tool": rec.tool, "row_id": rec.row_id}
    if dry_run:
        outbox.resolve(rec.key, happened=False, note="dry run: nothing was sent")
        return ReconcileItem(**base, verdict="dry_run", detail="dry run: nothing was sent")
    try:
        verdict, detail, receipt = await _read(rec, mcp, ctx)
    except Exception as e:  # noqa: BLE001 — an unreadable row stays uncertain; it is never guessed
        verdict, detail, receipt = "unreadable", f"{type(e).__name__}: {e}"[:300], None
    if verdict == "happened":
        outbox.resolve(rec.key, happened=True, receipt=receipt, note=detail)
    elif verdict == "not_sent":
        outbox.resolve(rec.key, happened=False, note=detail)
    elif verdict == "changed":
        outbox.give_up(rec.key, f"the row was changed by someone else before reconcile: {detail}")
    return ReconcileItem(**base, verdict=verdict, detail=detail)


async def _read(rec: OutboxRecord, mcp: McpSession, ctx: RunContext) -> tuple[ReconcileVerdict, str, dict | None]:
    op, entity = tool_operation(rec.tool), tool_entity(rec.tool)
    sent = {k: v for k, v in rec.arguments.items() if k != "id"}
    if op == "update" and rec.row_id and f"{entity}.get" in TOOL_ARGS:
        got = await mcp.call(f"{entity}.get", TOOL_ARGS[f"{entity}.get"].model_validate({"id": rec.row_id}))
        if not got.ok:
            return "unreadable", f"{entity}.get failed: {got.error.message if got.error else got.text}"[:300], None
        row = _payload_row(got.data()) or {}
        if all(same(v, row.get(k)) for k, v in sent.items()):
            return "happened", "the live row holds what was sent", {"id": rec.row_id, "reconciled": True}
        before = rec.before or {}
        if all(same(before.get(k), row.get(k)) for k in sent):
            return "not_sent", "the live row still holds the values from before the write", None
        diff = {k: row.get(k) for k, v in sent.items() if not same(v, row.get(k))}
        return "changed", f"live values {diff}"[:300], None
    if rec.tool == "AgentMemory.create":
        args: dict[str, Any] = {"limit": 1000, "is_active": True}
        if sent.get("party_id"):
            args["party_id"] = sent["party_id"]
        rows = await _list(mcp, "AgentMemory.list", args)
        if rows is None:
            return "unreadable", "AgentMemory.list failed", None
        hit = next((r for r in rows if str(r.get("content", "")).strip() == str(sent.get("content", "")).strip()
                    and _after(r.get("created_at"), rec.updated_at)), None)
        return _found(hit)
    if rec.tool == "EmailReminder.create":
        rows = await _list(mcp, "EmailReminder.list", {"limit": 200, "message_id": sent.get("message_id"),
                                                       **({"thread_id": sent["thread_id"]} if sent.get("thread_id") else {})})
        if rows is None:
            return "unreadable", "EmailReminder.list failed", None
        hit = next((r for r in rows if r.get("type") == sent.get("type")
                    and str(r.get("remind_at", ""))[:10] == str(sent.get("remind_at", ""))[:10]
                    and (r.get("note") or None) == (sent.get("note") or None)
                    and r.get("created_by") in (None, ctx.me.id)
                    and _after(r.get("created_at"), rec.updated_at)), None)
        return _found(hit)
    return "unreadable", f"no way to read back {rec.tool}: check it by hand", None


def _found(row: dict | None) -> tuple[ReconcileVerdict, str, dict | None]:
    if row is None:
        return "not_sent", "no row like it was created after the write started", None
    return "happened", f"found the created row {row.get('id')}", {"id": row.get("id"), "reconciled": True}


async def _list(mcp: McpSession, tool: str, args: dict[str, Any]) -> list[dict] | None:
    out = await mcp.call(tool, TOOL_ARGS[tool].model_validate(args))
    if not out.ok:
        return None
    data = out.data()
    rows = data.get("data") if isinstance(data, dict) else data
    return [r for r in rows or [] if isinstance(r, dict)]


def _backfill(outbox: WriteOutbox, log: RunLog) -> int:
    """Completed writes missing from writes.jsonl (a crash between the platform's answer and the record) are added."""
    have = {w.key for w in log.writes if w.key}
    n = 0
    for rec in outbox.records(OutboxStatus.COMPLETED):
        if rec.key in have:
            continue
        receipt = rec.receipt or {}
        row_id = rec.row_id
        if tool_operation(rec.tool) == "create":
            row_id = (_payload_row(receipt) or {}).get("id") if isinstance(receipt, dict) else None
        log.record_write(WriteRecord(tool=rec.tool, entity=tool_entity(rec.tool), row_id=row_id,
                                     fields={k: v for k, v in rec.arguments.items() if k != "id"},
                                     before=rec.before or {}, dry_run=bool(receipt.get("dry_run")), key=rec.key))
        n += 1
    return n


# ── comparing a live value with the one we sent ──────────────────────────────

def same(want: Any, live: Any) -> bool:
    """Equal as the platform stores them: "" and None alike, "1"/"0" as booleans (C3), numbers as numbers, a date
    the same as a timestamp on that day, timestamps by instant."""
    if want in (None, "") or live in (None, ""):
        return want in (None, "") and live in (None, "")
    if isinstance(want, bool):
        return want == (str(live).lower() in ("1", "true"))
    if isinstance(want, (int, float)):
        try:
            return abs(float(live) - float(want)) < 1e-6
        except (TypeError, ValueError):
            return False
    a, b = str(want), str(live)
    if a == b:
        return True
    ta, tb = _when(a), _when(b)
    if ta is None or tb is None:
        return False
    if len(a) == 10 or len(b) == 10:
        return a[:10] == b[:10]
    return ta == tb


def _when(text: Any) -> datetime | None:
    try:
        t = datetime.fromisoformat(str(text).replace("Z", "+00:00"))
    except ValueError:
        return None
    return t if t.tzinfo else t.replace(tzinfo=timezone.utc)


def _after(created_at: Any, started: datetime) -> bool:
    t = _when(created_at)
    s = started if started.tzinfo else started.replace(tzinfo=timezone.utc)
    return t is not None and t >= s - CLOCK_SLACK


def _payload_row(data: Any) -> Any:
    return data["data"] if isinstance(data, dict) and isinstance(data.get("data"), dict) else data
