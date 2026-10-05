"""Conversations in the fake book, written the way you would describe them: who said what, and when."""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any

from tests.kit.platform import ROOT, FakePlatform

SALES = json.loads((ROOT / "data" / "explore" / "suryodaya" / "mailboxes.json").read_text())[0]
TEAM11 = {"id": "mb-team11-orders", "email": "orders@team11.example"}       # shares the book; not ours


@dataclass
class Said:
    who: str          # "us" or "them"
    text: str
    at: str


def them(text: str, at: str) -> Said:
    return Said("them", text, at)


def us(text: str, at: str) -> Said:
    return Said("us", text, at)


class Mail:
    def __init__(self, platform: FakePlatform):
        self.platform = platform
        platform.add("Mailbox", **{k: SALES[k] for k in ("id", "email", "display_name", "user_id", "is_default",
                                                         "is_active")})

    def conversation(self, subject: str, *said: Said, counterpart: str = "buyer@acme.in",
                     mailbox: dict[str, Any] = SALES, **thread: Any) -> str:
        t = self.platform.add("EmailThread", subject=subject, mailbox_id=mailbox["id"], folder="inbox",
                              flag_status="not_flagged", flag_due_date=None, is_starred=False,
                              participant_emails=[mailbox["email"], counterpart],
                              updated_at=said[-1].at if said else "2026-10-01T09:00:00", **thread)
        for s in said:
            self.say(t["id"], s, counterpart=counterpart, mailbox=mailbox)
        return t["id"]

    def messages(self, thread_id: str) -> list[dict[str, Any]]:
        rows = [m for m in self.platform.rows["EmailMessage"].values() if m["thread_id"] == thread_id]
        return sorted(rows, key=lambda m: m["received_at"])

    def say(self, thread_id: str, s: Said, *, counterpart: str = "buyer@acme.in",
            mailbox: dict[str, Any] = SALES) -> str:
        sender = mailbox["email"] if s.who == "us" else counterpart
        m = self.platform.add("EmailMessage", thread_id=thread_id, mailbox_id=mailbox["id"], from_email=sender,
                              to=[counterpart if s.who == "us" else mailbox["email"]],
                              subject=self.platform.row("EmailThread", thread_id)["subject"], body_text=s.text,
                              folder="sent" if s.who == "us" else "inbox", received_at=s.at, sent_at=s.at,
                              created_at=s.at, updated_at=s.at)
        return m["id"]
