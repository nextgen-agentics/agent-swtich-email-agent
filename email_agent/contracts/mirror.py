"""Local mailbox copy contracts (Revision 12, Stage 2): what `state/<instance>/mailbox.sqlite` remembers about each sync,
and what one sync did."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field


def _now() -> datetime:
    return datetime.now(timezone.utc)


class SyncState(BaseModel):
    """Per (table, mailbox): the newest `updated_at` seen and when the last full pass ran."""

    model_config = ConfigDict(frozen=True)

    entity: Literal["EmailThread", "EmailMessage"]
    mailbox_id: str
    watermark: str | None = None          # server time, as the platform writes it
    last_full_at: datetime | None = None
    rows: int = 0                         # rows of this table and mailbox in the copy after the sync


class EntitySync(BaseModel):
    """One table in one mailbox (or, for AgentMemory, the company), in one sync."""

    entity: Literal["EmailThread", "EmailMessage", "AgentMemory"]
    mailbox: str
    full: bool                            # read everything (first sync, or --full-sync)
    calls: int
    fetched: int                          # rows read from the platform
    changed: int                          # rows new or different from the copy
    deleted: int = 0                      # rows gone from the platform (found only by a full pass)
    watermark_before: str | None = None
    watermark_after: str | None = None


class SyncReport(BaseModel):
    instance: str
    started_at: datetime = Field(default_factory=_now)
    seconds: float = 0.0
    calls: int = 0
    tables: list[EntitySync] = Field(default_factory=list)
    facts_recomputed: int = 0             # conversations whose worked-out facts were rebuilt
    threads_total: int = 0
    messages_total: int = 0
    embedded: int | None = None           # hybrid search (Stage 9): messages embedded after this sync
    search_note: str | None = None        # why meaning-based search is off for the rest of the run, if it failed
