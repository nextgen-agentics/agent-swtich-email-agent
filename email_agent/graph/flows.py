"""Flows (Revision 12, Stage 4): the part of each bulk skill that is plain code.

For each skill that works over the whole mailbox (or the conversations a search names):
  select   which conversations are candidates — SQL over the local copy's worked-out facts, no LLM
  digest   what one judging shard sees per conversation (compact, from the local copy)
  shard    the verdict model the shard's LLM call must return (contracts/flows.py)
  plan     verdicts → the writes that really change something (a WritePlan), checked by code
The LLM only judges (needs a reply? agreed? which tab?). Paging, selection, figures copied from the record and the
write fields are code, so they cannot be skipped, mis-copied or repeated.
"""

from __future__ import annotations

from datetime import date, timedelta
from typing import Any, Awaitable, Callable

from pydantic import ValidationError

from email_agent.contracts.agent import (
    ConversationOverview,
    PriceAgreementInput,
    RunContext,
)
from email_agent.contracts.capabilities import JudgeThreadsInput
from email_agent.contracts.flows import (
    FollowUpShard,
    FollowUpVerdict,
    PlannedWrite,
    PriceShard,
    PriceVerdict,
    ShardReply,
    SortShard,
    SortVerdict,
    SummaryShard,
    SummaryVerdict,
    TriageShard,
    TriageVerdict,
    Verdict,
    WritePlan,
)
from email_agent.mailbox.store import MailboxStore

SHARD_SIZE = 20
PAGE = 1000                      # the platform's largest page


def matches(search: str | None, *texts: str | None) -> bool:
    """The agent's word check: every search word appears in the texts (case-insensitive)."""
    if not search:
        return True
    hay = " ".join(x or "" for x in texts).lower()
    return all(word in hay for word in search.lower().split())


def price_memory_content(run_id: str, a: PriceAgreementInput) -> str:
    """The AgentMemory text for one agreed price. Its first line is machine-readable: the harness parses it, and
    BUG-014 (memory text cannot be searched) means checks find our rows by run id."""
    first = (f"price-agreement | run={run_id} | thread={a.thread_id} | message={a.agreement_message_id}"
             f" | ref={a.reference or '-'} | qty={a.quantity:g} | unit={a.unit_price:.2f} | total={a.total:.2f}"
             f" | currency={a.currency} | agreed_on={a.agreed_on}")
    text = (f"Agreed price: {a.item}, {a.quantity:g} @ {a.unit_price:,.2f} {a.currency} = {a.total:,.2f} "
            f"{a.currency}{f' (ref {a.reference})' if a.reference else ''}, agreed on {a.agreed_on}."
            + (f" Record check: {a.record_check}" if a.record_check else ""))
    return f"{first}\n{text}"


def working_days_after(start: date, n: int) -> date:
    d = start
    while n:
        d += timedelta(days=1)
        if d.weekday() < 5:
            n -= 1
    return d


class ScopedMailbox:
    """The local mailbox copy as a run limited to some conversations sees it (a watcher run, Stage 8): every read a flow
    makes returns only those conversations, so selection, counts and the write plan all follow the scope. Found live:
    the scope applied only to selection, and the answer said "checked 20 conversations" for a one-conversation run."""

    def __init__(self, store: MailboxStore, only: list[str]):
        self.store, self.only = store, set(only)

    def __getattr__(self, name: str):
        return getattr(self.store, name)

    def candidate_ids(self, mailbox_ids, search):
        found = self.store.candidate_ids(mailbox_ids, search)
        return set(self.only) if found is None else found & self.only

    def overviews(self, mailbox_ids, **kw):
        return [(mb, o) for mb, o in self.store.overviews(mailbox_ids, **kw) if o.thread_id in self.only]

    def digests(self, mailbox_ids, **kw):
        return [(mb, d) for mb, d in self.store.digests(mailbox_ids, **kw) if d.thread_id in self.only]

    def price_views(self, mailbox_ids, **kw):
        return [x for x in self.store.price_views(mailbox_ids, **kw) if x[0].id in self.only]

    def threads(self, mailbox_ids, **kw):
        return {k: v for k, v in self.store.threads(mailbox_ids, **kw).items() if k in self.only}

    def conversation_count(self, mailbox_ids):
        return len(self.overviews(mailbox_ids))


class Flow:
    """One bulk skill's code. Subclasses fill in select / digest / plan."""

    skill: str
    shard: type[ShardReply]
    date_bound = False                     # verdicts depend on today (the verdict cache keys them by date)

    def __init__(self, store: MailboxStore | ScopedMailbox, ctx: RunContext):
        self.store, self.ctx = store, ctx
        self.ids = [m.id for m in ctx.mailboxes]
        self.address = {m.id: m.email for m in ctx.mailboxes}

    def select(self, args: JudgeThreadsInput) -> list[str]:
        raise NotImplementedError

    # `fetch(tool, args)` reads the platform: a list tool returns typed rows, `Deal.get` a DealView dict (or None).
    async def digests(self, thread_ids: list[str], fetch: Callable[[str, dict], Awaitable[Any]]) -> list[dict]:
        raise NotImplementedError

    async def plan(self, verdicts: list[Verdict], args: JudgeThreadsInput,
                   fetch: Callable[[str, dict], Awaitable[Any]]) -> WritePlan:
        raise NotImplementedError

    def counts(self, verdicts: list[Verdict]) -> dict[str, int]:
        raise NotImplementedError

    # the cross-model check (Stage 5): which verdicts lead to a write, and when two verdicts agree
    checkable = True                       # False for free text (summaries): code checks only

    def positive(self, v: Verdict) -> bool:
        raise NotImplementedError

    def agree(self, a: Verdict, b: Verdict) -> bool:
        raise NotImplementedError

    # helpers
    def _overviews(self, search: str | None) -> list[ConversationOverview]:
        cand = self.store.candidate_ids(self.ids, search)
        return [o for _, o in self.store.overviews(self.ids)
                if (cand is None or o.thread_id in cand)
                and matches(search, o.subject, o.counterpart, o.newest_real_text)]

    def _overview_digest(self, ids: list[str], keys: list[str]) -> list[dict]:
        by_id = {o.thread_id: (mb, o) for mb, o in self.store.overviews(self.ids, ids=set(ids))}
        out = []
        for tid in ids:
            mb, o = by_id[tid]
            d = o.model_dump(mode="json", include=set(keys), exclude_none=True)
            out.append({"thread_id": tid, "mailbox": self.address.get(mb), **d})
        return out


class TriageFlow(Flow):
    skill, shard = "triage-replies", TriageShard
    KEYS = ["subject", "counterpart", "newest_real_at", "newest_real_text", "flag_status", "flag_due_date"]

    def select(self, args: JudgeThreadsInput) -> list[str]:
        """Rule 3 of the skill, as code: only conversations whose newest real message is theirs and unanswered."""
        return [o.thread_id for o in self._overviews(args.search)
                if o.has_inbound and o.newest_real_from == "them" and not o.we_replied_after_them]

    async def digests(self, thread_ids, fetch):
        return self._overview_digest(thread_ids, self.KEYS)

    async def plan(self, verdicts, args, fetch):
        today = self.ctx.today.isoformat()
        rows = self.store.threads(self.ids, ids={v.thread_id for v in verdicts})
        plan = WritePlan(skill=self.skill)
        for v in verdicts:
            th = rows.get(v.thread_id)
            if not v.needs_reply or th is None:
                continue
            if th.flag_status == "flagged" and th.flag_due_date and th.flag_due_date[:10] <= today:
                plan.unchanged.append(v.thread_id)
                continue
            plan.writes.append(PlannedWrite(tool="EmailThread.update", thread_id=v.thread_id, why=v.why,
                                            fields={"id": v.thread_id, "flag_status": "flagged", "flag_due_date": today}))
        return plan

    def positive(self, v):
        return v.needs_reply

    def agree(self, a, b):
        return a.needs_reply == b.needs_reply

    def counts(self, verdicts):
        n = sum(1 for v in verdicts if v.needs_reply)
        total = self.store.conversation_count(self.ids)
        return {"needs_reply": n, "no_reply_needed": len(verdicts) - n, "not_waiting_on_us": total - len(verdicts),
                "conversations_checked": total}


class SortFlow(Flow):
    skill, shard = "sort-inbox", SortShard

    def select(self, args):
        cand = self.store.candidate_ids(self.ids, args.search)
        return [d.thread_id for _, d in self.store.digests(self.ids, ids=cand)
                if matches(args.search, d.subject, d.counterpart, *(m.text for m in d.messages))]

    async def digests(self, thread_ids, fetch):
        wanted = set(thread_ids)
        return [{"mailbox": self.address.get(mb), **d.model_dump(mode="json", exclude_none=True,
                                                                exclude={"summary", "summary_updated_at", "mailbox"})}
                for mb, d in self.store.digests(self.ids, ids=wanted)]

    async def plan(self, verdicts, args, fetch):
        rows = self.store.threads(self.ids, ids={v.thread_id for v in verdicts})
        plan = WritePlan(skill=self.skill)
        for v in verdicts:
            th = rows.get(v.thread_id)
            if th is None:
                continue
            if th.importance == v.importance and th.split_category == v.split_category:
                plan.unchanged.append(v.thread_id)
                continue
            plan.writes.append(PlannedWrite(tool="EmailThread.update", thread_id=v.thread_id, why=v.why, fields={
                "id": v.thread_id, "importance": v.importance, "split_category": v.split_category}))
        return plan

    def positive(self, v):
        if not hasattr(self, "_rows"):            # once per flow, not once per verdict (10,000 verdicts = 10,000 reads)
            self._rows = self.store.threads(self.ids)
        th = self._rows.get(v.thread_id)
        return th is not None and (th.importance != v.importance or th.split_category != v.split_category)

    def agree(self, a, b):
        return a.importance == b.importance       # the tab is a softer call; the importance decides

    def counts(self, verdicts):
        out: dict[str, int] = {}
        for v in verdicts:
            out[f"importance_{v.importance}"] = out.get(f"importance_{v.importance}", 0) + 1
        return out


class SummaryFlow(Flow):
    skill, shard = "summarize-threads", SummaryShard

    def select(self, args):
        """Named conversations when the request names some; otherwise those with no summary or an old one."""
        cand = self.store.candidate_ids(self.ids, args.search)
        return [d.thread_id for _, d in self.store.digests(self.ids, only_stale_summaries=not args.search, ids=cand)
                if matches(args.search, d.subject, d.counterpart, *(m.text for m in d.messages))]

    async def digests(self, thread_ids, fetch):
        wanted = set(thread_ids)
        return [{"mailbox": self.address.get(mb), **d.model_dump(mode="json", exclude_none=True,
                                                                exclude={"importance", "split_category", "mailbox"})}
                for mb, d in self.store.digests(self.ids, ids=wanted)]

    async def plan(self, verdicts, args, fetch):
        today = self.ctx.today.isoformat()
        return WritePlan(skill=self.skill, writes=[
            PlannedWrite(tool="EmailThread.update", thread_id=v.thread_id,
                         fields={"id": v.thread_id, "summary": v.summary, "summary_updated_at": today})
            for v in verdicts])

    checkable = False                      # free text: two summaries cannot be compared

    def counts(self, verdicts):
        return {"summarised": len(verdicts)}


class FollowUpFlow(Flow):
    skill, shard = "follow-up-reminders", FollowUpShard
    date_bound = True
    KEYS = ["subject", "counterpart", "newest_real_at", "newest_real_text", "our_last_at", "our_last_message_id"]

    def select(self, args):
        return [o.thread_id for o in self._overviews(args.search) if o.has_inbound and o.newest_real_from == "us"]

    async def digests(self, thread_ids, fetch):
        return self._overview_digest(thread_ids, self.KEYS)

    async def plan(self, verdicts, args, fetch):
        """`message_id` comes from the facts (our last message), never from the model; the date is checked in code."""
        overviews = {o.thread_id: o for _, o in self.store.overviews(self.ids, ids={v.thread_id for v in verdicts})}
        waiting = [v for v in verdicts if v.waiting_on_them and v.thread_id in overviews]
        existing: list[Any] = []
        while True:                    # every page: a reminder past the first 1,000 must not be created again
            page = await fetch("EmailReminder.list", {"limit": PAGE, "offset": len(existing)})
            existing += page
            if len(page) < PAGE:
                break
        have = {r.thread_id for r in existing if r.type == "follow_up" and not r.is_fired}
        plan = WritePlan(skill=self.skill)
        for v in waiting:
            o = overviews[v.thread_id]
            if v.thread_id in have:
                plan.unchanged.append(v.thread_id)
                continue
            if not o.our_last_message_id:
                plan.problems.append(f"{v.thread_id}: no message of ours to point the reminder at")
                continue
            when = args.remind_on or v.remind_at
            if when is None:
                last = date.fromisoformat((o.our_last_at or self.ctx.today.isoformat())[:10])
                when = working_days_after(last, 3)
            if when <= self.ctx.today:
                when = working_days_after(self.ctx.today, 1)
            plan.writes.append(PlannedWrite(tool="EmailReminder.create", thread_id=v.thread_id, why=v.note or "", fields={
                "company_id": self.ctx.me.company_id, "thread_id": v.thread_id, "message_id": o.our_last_message_id,
                "type": "follow_up", "condition": "no_reply", "remind_at": when.isoformat(),
                "note": (v.note or "waiting for their reply")[:200]}))
        return plan

    def positive(self, v):
        return v.waiting_on_them

    def agree(self, a, b):
        return a.waiting_on_them == b.waiting_on_them

    def counts(self, verdicts):
        n = sum(1 for v in verdicts if v.waiting_on_them)
        return {"waiting_on_them": n, "not_waiting": len(verdicts) - n}


class PriceFlow(Flow):
    skill, shard = "find-price-agreement", PriceShard

    def select(self, args):
        cand = self.store.candidate_ids(self.ids, args.search)
        return [v.thread_id for _, _, v in self.store.price_views(self.ids, ids=cand)
                if matches(args.search, v.subject, v.counterpart, " ".join(v.references),
                           *(m.text for m in v.their_messages))]

    async def digests(self, thread_ids, fetch):
        wanted = set(thread_ids)
        out = []
        for th, mb, v in self.store.price_views(self.ids, ids=wanted):
            item = {"mailbox": self.address.get(mb), **v.model_dump(mode="json", exclude_none=True, exclude={"mailbox"})}
            if th.deal_id:
                deal = await fetch("Deal.get", {"id": th.deal_id})
                if deal is not None:
                    item["deal"] = deal
            out.append(item)
        return out

    async def plan(self, verdicts, args, fetch):
        rows = self.store.threads(self.ids, ids={v.thread_id for v in verdicts})
        plan = WritePlan(skill=self.skill)
        for v in verdicts:
            if v.status != "agreed":
                continue
            th = rows.get(v.thread_id)
            if th is None:
                continue
            try:
                a = PriceAgreementInput(thread_id=v.thread_id, agreement_message_id=v.agreement_message_id or "",
                                        party_id=v.party_id or th.party_id, reference=v.reference, item=v.item or "",
                                        quantity=v.quantity or 0, unit_price=v.unit_price or 0, total=v.total or 0,
                                        currency=self.ctx.locale.base_currency, agreed_on=v.agreed_on or "",
                                        record_check=v.record_check)
                if not a.agreement_message_id or not a.agreed_on:
                    raise ValueError("agreed, but no accepting message id or date")
            except (ValidationError, ValueError) as e:
                plan.problems.append(f"{v.thread_id}: agreed, but the figures fail the check: {str(e)[:200]}")
                continue
            plan.writes.append(PlannedWrite(tool="AgentMemory.create", thread_id=v.thread_id, why=v.why, fields={
                "category": "fact", "content": price_memory_content(self.ctx.run_id, a), "party_id": a.party_id,
                "source": "extracted", "is_active": True}))
            if not th.is_starred:
                plan.writes.append(PlannedWrite(tool="EmailThread.update", thread_id=v.thread_id,
                                                fields={"id": v.thread_id, "is_starred": True}))
        return plan

    def positive(self, v):
        return v.status == "agreed"

    def agree(self, a, b):
        """Both "agreed" with the same figures, or both not agreed."""
        if (a.status == "agreed") != (b.status == "agreed"):
            return False
        if a.status != "agreed":
            return True
        return all(x is not None and y is not None and abs(x - y) <= 0.01
                   for x, y in ((a.quantity, b.quantity), (a.unit_price, b.unit_price), (a.total, b.total)))

    def counts(self, verdicts):
        out: dict[str, int] = {}
        for v in verdicts:
            out[v.status] = out.get(v.status, 0) + 1
        return out


FLOWS: dict[str, type[Flow]] = {f.skill: f for f in (TriageFlow, SortFlow, SummaryFlow, FollowUpFlow, PriceFlow)}
VERDICTS: dict[str, type[Verdict]] = {"triage-replies": TriageVerdict, "sort-inbox": SortVerdict,
                                        "summarize-threads": SummaryVerdict, "follow-up-reminders": FollowUpVerdict,
                                        "find-price-agreement": PriceVerdict}
