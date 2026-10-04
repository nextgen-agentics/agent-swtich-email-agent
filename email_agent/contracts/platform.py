"""Platform shapes the agent reads: who we are (Me, RegimeLocale), the login reply, and the row models
returned by <Entity>.list / .get, with the ListPage envelope.

Read models use extra="allow" so a new platform field never breaks parsing.
The /api/schemas, /api/agent/tools and bug-report shapes are used only by scripts: scripts/contracts/catalog.py.
"""

from __future__ import annotations

from typing import Any, Generic, TypeVar

from typing import Annotated

from pydantic import BaseModel, BeforeValidator, ConfigDict, Field


class Me(BaseModel):
    """GET /api/auth/me — the seat as the server states it."""

    model_config = ConfigDict(extra="allow")

    id: str
    email: str
    name: str | None = None
    role: str | None = None
    roles: list[str] = Field(default_factory=list)
    allowed_apps: list[str] = Field(default_factory=list)
    company_id: str | None = None
    party_id: str | None = None
    employee_id: str | None = None


class LoginResponse(BaseModel):
    model_config = ConfigDict(extra="allow")

    token: str


class RegimeLocale(BaseModel):
    """GET /api/accounting/display-locale → .locale — how money and dates are
    written for the caller's company. Readable by every seat (the email seat
    gets 403 on /api/accounting/locale, which is the accounting app's own)."""

    model_config = ConfigDict(extra="allow")

    country: str                 # "IN", "US"
    base_currency: str           # "INR", "USD"
    currency_symbol: str | None = None
    number_format: str | None = None
    date_format: str | None = None
    locale_tag: str | None = None
    fiscal_year_start_month: str | None = None


# ── rows returned by <Entity>.list / .get ────────────────────────────────────
# WORKAROUND(BUG-008): booleans arrive as 0/1, counts as floats, datetimes
# sometimes date-only. We rely on pydantic's lax coercion and keep timestamps
# as `str`. Temporary — tighten the types once BUG-008 is fixed.
# Observed on the wire (2026-09-24): booleans arrive as 0/1, counts as floats
# (2.0), datetimes sometimes date-only ("2026-08-04"). Pydantic's lax mode
# coerces 0/1 → bool and 2.0 → int; timestamps stay `str` so a date-only
# value never fails parsing. `_display`-style helper keys land in extras.

# WORKAROUND(BUG-001): fields typed `text` in /api/schemas arrive in three shapes:
#   "a@x,b@y"                              (Suryodaya seed, and anything written via MCP/REST create)
#   ["a@x","b@y"]                          (Keystone rows)
#   [{"email":"a@x","name":"A"}, …]        (rows written by the send path, e.g. endpoint.email.messages.send)
# Temporary — remove once scripts/check_brief_claims.py shows BUG-001 fixed.
def _to_str_list(value: Any) -> list[str] | None:
    """Normalise every wire form seen for the same `text` field to a list of strings."""
    if value is None or value == "":
        return None
    if isinstance(value, str):
        return [part.strip() for part in value.split(",") if part.strip()]
    if isinstance(value, list):
        return [str(v.get("email") or v.get("name") or v) if isinstance(v, dict) else str(v) for v in value]
    return [str(value)]


StrList = Annotated[list[str] | None, BeforeValidator(_to_str_list)]


class Row(BaseModel):
    model_config = ConfigDict(extra="allow")

    id: str
    company_id: str | None = None
    created_at: str | None = None
    updated_at: str | None = None
    created_by: str | None = None
    updated_by: str | None = None


class Mailbox(Row):
    email: str | None = None
    display_name: str | None = None
    user_id: str | None = None
    provider: str | None = None
    kind: str | None = None
    is_default: bool | None = None
    is_active: bool | None = None
    connection_status: str | None = None


class EmailThread(Row):
    subject: str | None = None
    mailbox_id: str | None = None
    participant_emails: StrList = None
    participant_names: StrList = None
    message_count: int | None = None
    unread_count: int | None = None
    last_message_at: str | None = None
    last_sender_email: str | None = None
    last_sender_name: str | None = None
    snippet: str | None = None
    has_attachments: bool | None = None
    is_read: bool | None = None
    is_starred: bool | None = None
    star_type: str | None = None
    is_pinned: bool | None = None
    is_muted: bool | None = None
    folder: str | None = None
    labels: StrList = None
    split_category: str | None = None
    inference_class: str | None = None
    importance: str | None = None
    flag_status: str | None = None
    flag_due_date: str | None = None
    party_id: str | None = None
    deal_id: str | None = None
    summary: str | None = None
    summary_updated_at: str | None = None
    snoozed_until: str | None = None


class EmailMessage(Row):
    message_id: str | None = None
    mailbox_id: str | None = None
    thread_id: str | None = None
    in_reply_to: str | None = None
    references: str | None = None
    from_email: str | None = None
    from_name: str | None = None
    to: StrList = None
    cc: StrList = None
    bcc: StrList = None
    subject: str | None = None
    body_text: str | None = None
    body_html: str | None = None
    snippet: str | None = None
    folder: str | None = None
    labels: StrList = None
    is_read: bool | None = None
    is_starred: bool | None = None
    star_type: str | None = None
    is_draft: bool | None = None
    is_answered: bool | None = None
    is_forwarded: bool | None = None
    importance: str | None = None
    split_category: str | None = None
    flag_status: str | None = None
    flag_due_date: str | None = None
    party_id: str | None = None
    deal_id: str | None = None
    campaign_recipient_id: str | None = None
    status: str | None = None
    list_unsubscribe: str | None = None
    spam_score: float | None = None
    phishing_score: float | None = None
    received_at: str | None = None
    sent_at: str | None = None


class Party(Row):
    name: str | None = None
    type: str | None = None
    email: str | None = None
    company_name: str | None = None
    contact_type: str | None = None


class Deal(Row):
    title: str | None = None
    party_id: str | None = None
    value: float | None = None
    currency: str | None = None
    stage: str | None = None
    expected_close_date: str | None = None
    notes: str | None = None


class AgentMemory(Row):
    content: str | None = None
    category: str | None = None
    importance: float | None = None
    source: str | None = None
    party_id: str | None = None
    session_id: str | None = None
    is_active: bool | None = None
    user_id: str | None = None


class AgentTodo(Row):
    title: str | None = None
    detail: str | None = None
    status: str | None = None
    priority: str | None = None
    due_date: str | None = None


class EmailReminder(Row):
    message_id: str | None = None
    thread_id: str | None = None
    type: str | None = None
    remind_at: str | None = None
    condition: str | None = None
    is_fired: bool | None = None
    note: str | None = None


class EmailLabel(Row):
    name: str | None = None
    mailbox_id: str | None = None
    type: str | None = None
    user_id: str | None = None


class AgentToolPolicy(Row):
    name: str | None = None
    description: str | None = None
    allowed_domains: str | None = None
    denied_domains: str | None = None
    allowed_entities: str | None = None
    denied_entities: str | None = None
    write_domains: str | None = None
    read_only: bool | None = None
    role: str | None = None
    is_active: bool | None = None


RowT = TypeVar("RowT", bound=Row)

# Entity → row model, for parsing tool results. Entities not listed parse as plain `Row` (extra="allow").
ROW_MODELS: dict[str, type[Row]] = {
    "Mailbox": Mailbox, "EmailThread": EmailThread, "EmailMessage": EmailMessage, "Party": Party, "Deal": Deal,
    "AgentMemory": AgentMemory, "AgentTodo": AgentTodo, "EmailReminder": EmailReminder, "EmailLabel": EmailLabel,
    "AgentToolPolicy": AgentToolPolicy,
}


class ListPage(BaseModel, Generic[RowT]):
    """The envelope every <Entity>.list returns: {data, total, limit, offset}."""

    data: list[RowT]
    total: int
    limit: int
    offset: int
