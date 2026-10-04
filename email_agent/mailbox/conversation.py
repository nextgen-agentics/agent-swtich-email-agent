"""Who wrote last, worked out from the messages themselves.

Used by the agent's local tool `mailbox_overview` and by scripts/agent/propose_ground_truth.py.
Plain code, no LLM: it reports facts; deciding whether a reply is needed is judgement.

Two data realities it handles:
- The thread's own `last_sender_email` / `message_count` are not updated when messages are
  added (BUG-005), so they are ignored.
- The pre-loaded Suryodaya mail pairs every real message with a "mirror": the same text a day
  later from the other side. A mirror is not a reply, so it is marked and skipped
  (WORKAROUND(BUG-022); scripts/platform/check_brief_claims.py F15 reports it through `mirror_pairs`).
"""

from __future__ import annotations

import hashlib
import re

from email_agent.contracts.agent import (
    ConversationDigest,
    ConversationOverview,
    DigestMessage,
    MessageView,
    PriceConversation,
)
from email_agent.contracts.platform import EmailMessage, EmailThread

FACTS_VERSION = 1      # bump when overview / digest / price_view change: the local mailbox copy then rebuilds its facts


def content_hash(thread: EmailThread, messages: list[EmailMessage]) -> str:
    """Changes whenever anything a judgement about the conversation could depend on changes: its subject, or any
    message's id, sender, time, folder, status or text. Thread flags (star, summary, importance) are not included:
    they are what the agent writes, not what it reads. Used as the key of the verdict cache (Revision 12)."""
    parts = [thread.id, thread.subject or ""]
    for m in sorted((m for m in messages if m.thread_id == thread.id), key=lambda m: m.id):
        parts += [m.id, (m.from_email or "").lower(), m.received_at or m.sent_at or m.created_at or "", m.folder or "",
                  m.status or "", (m.body_text or m.snippet or "").strip()]
    return hashlib.sha256("\x1f".join(parts).encode()).hexdigest()[:32]


def _norm(text: str) -> str:
    return re.sub(r"\s+", " ", text).strip().lower()[:300]


def message_view(m: EmailMessage, our_emails: set[str]) -> MessageView:
    sender = (m.from_email or "").lower() or None
    return MessageView(
        id=m.id,
        direction="us" if sender in our_emails else "them",
        sender=sender,
        at=m.received_at or m.sent_at or m.created_at,
        folder=m.folder,
        status=m.status,
        text=(m.body_text or m.snippet or "").strip(),
    )


def _views(thread: EmailThread, messages: list[EmailMessage], our_emails: set[str]) -> list[MessageView]:
    """The thread's messages, oldest first, each mirror copy pointing at the message it copies.
    WORKAROUND(BUG-022): the pre-loaded Suryodaya mail has these copies; remove once the sample data is fixed."""
    ours = {e.lower() for e in our_emails}
    views = sorted((message_view(m, ours) for m in messages if m.thread_id == thread.id), key=lambda v: v.at or "")
    for i, v in enumerate(views):
        key = _norm(v.text)
        if not key:
            continue
        earlier = next((e for e in views[:i] if e.direction != v.direction and _norm(e.text) == key), None)
        if earlier:
            v.mirror_of = earlier.id
    return views


def mirror_pairs(thread: EmailThread, messages: list[EmailMessage], our_emails: set[str]) -> list[tuple[str, str]]:
    """(original message id, mirror copy id) for every mirror copy in the thread."""
    return [(v.mirror_of, v.id) for v in _views(thread, messages, our_emails) if v.mirror_of]


def overview(thread: EmailThread, messages: list[EmailMessage], our_emails: set[str]) -> ConversationOverview:
    views = _views(thread, messages, our_emails)
    real = [v for v in views if not v.mirror_of]
    theirs = [v for v in real if v.direction == "them"]
    newest = real[-1] if real else None
    last_theirs = theirs[-1] if theirs else None
    last_ours = next((v for v in reversed(real) if v.direction == "us"), None)
    return ConversationOverview(
        thread_id=thread.id,
        subject=thread.subject,
        folder=thread.folder,
        counterpart=(last_theirs.sender if last_theirs else None),
        messages=len(views),
        mirror_copies=sum(1 for v in views if v.mirror_of),
        has_inbound=bool(theirs),
        newest_real_from=newest.direction if newest else None,
        newest_real_at=newest.at if newest else None,
        newest_real_text=(newest.text if newest else "")[:400],
        we_replied_after_them=bool(last_theirs and any(v.direction == "us" and (v.at or "") > (last_theirs.at or "")
                                                       for v in real)),
        newest_real_message_id=newest.id if newest else None,
        our_last_message_id=last_ours.id if last_ours else None,
        our_last_at=last_ours.at if last_ours else None,
        flag_status=thread.flag_status,
        flag_due_date=thread.flag_due_date,
        is_starred=thread.is_starred,
        importance=thread.importance,
        split_category=thread.split_category,
        summary=thread.summary,
        summary_updated_at=thread.summary_updated_at,
        party_id=thread.party_id,
        deal_id=thread.deal_id,
    )


# ── compact views for the summary and price skills ───────────────────────────

DIGEST_TEXT = 300          # characters kept per message
DIGEST_MESSAGES = 4        # newest real messages kept per conversation
PRICE_WORDS = re.compile(r"\b(quot\w*|pric\w*|rates?|award\w*|purchase order|PO|orders?|accept\w*|agree\w*)\b|"
                         r"\d\s*(pcs|units|nos|sets)?\s*@", re.I)
REFERENCE = re.compile(r"\b(?:[A-Z]{2,5}-\d{4}-\d{2,6}|[A-Z]{2,5}-\d{4,})\b")   # QTN-2026-00003, RFQ-2026-0003, PO CTW-491492
PRICE_LINE = re.compile(r"@\s*\S*\d")


def _short(text: str, limit: int = DIGEST_TEXT) -> str:
    text = " ".join(text.split())
    return text if len(text) <= limit else text[:limit] + " …"


def _real(thread: EmailThread, messages: list[EmailMessage], our_emails: set[str]) -> list[MessageView]:
    return [v for v in _views(thread, messages, our_emails) if not v.mirror_of]


def digest(thread: EmailThread, messages: list[EmailMessage], our_emails: set[str]) -> ConversationDigest:
    real = _real(thread, messages, our_emails)
    newest = real[-1].at if real else None
    current = bool(thread.summary and thread.summary_updated_at and newest
                   and thread.summary_updated_at[:10] >= newest[:10])
    theirs = [v for v in real if v.direction == "them"]
    return ConversationDigest(
        thread_id=thread.id, subject=thread.subject, counterpart=theirs[-1].sender if theirs else None,
        party_id=thread.party_id, deal_id=thread.deal_id, folder=thread.folder, newest_real_at=newest,
        summary=thread.summary, summary_updated_at=thread.summary_updated_at, summary_current=current,
        importance=thread.importance, split_category=thread.split_category,
        messages=[DigestMessage(id=v.id, direction=v.direction, at=v.at, text=_short(v.text))
                  for v in real[-DIGEST_MESSAGES:]])


def price_view(thread: EmailThread, messages: list[EmailMessage], our_emails: set[str]) -> PriceConversation | None:
    """None when nothing in the conversation is about prices or orders."""
    real = _real(thread, messages, our_emails)
    if not any(PRICE_WORDS.search(f"{thread.subject or ''} {v.text}") for v in real):
        return None
    text = " ".join([thread.subject or ""] + [v.text for v in real])
    ours = [v for v in real if v.direction == "us"]
    lines = [" ".join(line.split()) for v in ours for line in v.text.splitlines() if PRICE_LINE.search(line)]
    theirs = [v for v in real if v.direction == "them"]
    return PriceConversation(
        thread_id=thread.id, subject=thread.subject, counterpart=theirs[-1].sender if theirs else None,
        party_id=thread.party_id, is_starred=thread.is_starred,
        references=sorted(set(REFERENCE.findall(text)))[:12], our_price_lines=lines[:4],
        our_last_at=ours[-1].at if ours else None,
        their_messages=[DigestMessage(id=v.id, direction=v.direction, at=v.at, text=_short(v.text, 260))
                        for v in theirs[-2:]])

