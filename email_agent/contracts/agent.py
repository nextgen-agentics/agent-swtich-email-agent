"""Agent contracts: every hand-off inside a run (S7 layout).

    context → RunContext
    planner (LLM)    → Goal per thing asked (Revision 12; contracts/capabilities.py has its replies)
    action (no LLM)  → ActionResult (+ WriteRecord)
    start of run     → RunRequest (written before any network call)
    end of run       → FinalAnswer, RunOutcome (+ RunError when the run crashed or was interrupted)

Plus how the agent sees one conversation (ConversationOverview, built by conversation.py).
"""

from __future__ import annotations

from datetime import date, datetime, timezone
from typing import Any, Literal

from pydantic import BaseModel, Field, model_validator

from email_agent.contracts.llm import Usage
from email_agent.contracts.platform import Me, RegimeLocale

Direction = Literal["us", "them"]


class MessageView(BaseModel):
    """One message, reduced to what "who owes a reply" needs."""

    id: str
    direction: Direction                 # "us" = sent from one of our mailboxes
    sender: str | None = None
    at: str | None = None                # received_at, else sent_at, else created_at
    folder: str | None = None
    status: str | None = None
    text: str = ""                       # body_text, else snippet (never None)
    mirror_of: str | None = None         # id of the earlier message from the other side this one repeats word for word


class ConversationOverview(BaseModel):
    """Plain facts about one conversation, worked out from its messages (never from the
    thread's summary fields, which are not kept up to date — BUG-005). No judgement here:
    whether a reply is *needed* is decided by the agent (LLM) or by you (ground truth)."""

    thread_id: str
    subject: str | None = None
    mailbox: str | None = None                   # which of our addresses the conversation is in
    folder: str | None = None
    counterpart: str | None = None               # the other side's address in the newest real message
    messages: int = 0
    mirror_copies: int = 0                       # messages that only repeat an earlier message
    has_inbound: bool = False                    # any real message from them at all
    newest_real_from: Direction | None = None    # who wrote the newest real (non-mirror) message
    newest_real_at: str | None = None
    newest_real_text: str = ""
    we_replied_after_them: bool = False          # a real message from us after their newest real message
    newest_real_message_id: str | None = None
    our_last_message_id: str | None = None       # our newest real message (a follow-up reminder points at it)
    our_last_at: str | None = None
    flag_status: str | None = None
    flag_due_date: str | None = None
    is_starred: bool | None = None
    importance: str | None = None
    split_category: str | None = None
    summary: str | None = None
    summary_updated_at: str | None = None
    party_id: str | None = None
    deal_id: str | None = None


class MailboxOverviewInput(BaseModel):
    """Arguments of the local tool `mailbox_overview` (validated like any MCP tool's arguments)."""

    model_config = {"extra": "forbid"}

    folder: Literal["inbox", "sent", "archive", "all"] = Field(
        "all", description="Only conversations in this folder; 'all' for every folder.")
    only_waiting_on_us: bool = Field(
        False, description="Only conversations whose newest real message is from them and unanswered.")


# Why a goal was refused — data, so the harness can check a refusal without reading the answer text.
RefusalReason = Literal["out_of_seat", "not_our_mailbox", "not_permitted", "no_evidence", "unknown_record"]
# out_of_seat: needs another app's data (salaries, invoices) · not_our_mailbox: a mailbox our login does not own
# not_permitted: an action this seat must not do (send mail, delete shared mail) · no_evidence: the data does not
# support an answer · unknown_record: a named RFQ, customer or conversation does not exist


class PriceAgreementInput(BaseModel):
    """Arguments of the local tool `record_price_agreement`: one agreed price, saved as an AgentMemory fact."""

    model_config = {"extra": "forbid"}

    thread_id: str = Field(description="The conversation where the price was agreed.")
    agreement_message_id: str = Field(description="The other side's message that accepts our price.")
    party_id: str | None = Field(None, description="The other side's party id (from the conversation), if known.")
    reference: str | None = Field(None, description="Our quote or order reference the agreement names, e.g. a "
                                                    "quotation or sales-order number.")
    item: str = Field(min_length=2, max_length=200, description="What was priced, as written in our quote.")
    quantity: float = Field(gt=0)
    unit_price: float = Field(gt=0)
    total: float = Field(gt=0, description="quantity × unit price, before tax, as written in our quote.")
    currency: str = Field(pattern=r"^[A-Z]{3}$", description="The company's currency code from the run facts.")
    agreed_on: str = Field(description="Date of the agreeing message (YYYY-MM-DD).")
    record_check: str | None = Field(None, description="What a linked deal or order says about this price, if "
                                                       "you checked one (agrees / differs: …).")

    @model_validator(mode="after")
    def _total_matches(self) -> "PriceAgreementInput":
        expected = self.quantity * self.unit_price
        if abs(self.total - expected) > max(0.01 * self.total, 1.0):
            raise ValueError(f"total {self.total} is not quantity × unit price ({expected:.2f}); copy the figures "
                             "from our quote line")
        return self


class PriceOverviewInput(BaseModel):
    """Arguments of the local tool `price_overview`."""

    model_config = {"extra": "forbid"}

    search: str | None = Field(None, description="Only conversations whose subject, other side or text contains "
                                                 "these words, e.g. a party, part or reference (case-insensitive).")
    offset: int = Field(0, ge=0, description="Start here; use next_offset from the previous page.")
    limit: int = Field(20, ge=1, le=30)


class DigestMessage(BaseModel):
    id: str
    direction: Direction
    at: str | None = None
    text: str = ""


class ConversationDigest(BaseModel):
    """One conversation, compact: its real messages (mirror copies left out), oldest first."""

    thread_id: str
    subject: str | None = None
    mailbox: str | None = None
    counterpart: str | None = None
    party_id: str | None = None
    deal_id: str | None = None
    folder: str | None = None
    newest_real_at: str | None = None
    summary: str | None = None
    summary_updated_at: str | None = None
    summary_current: bool = False              # summary_updated_at is on or after the newest real message
    importance: str | None = None
    split_category: str | None = None
    messages: list[DigestMessage] = Field(default_factory=list)


class DealLine(BaseModel):
    description: str | None = None
    qty: float | None = None
    rate: float | None = None
    amount: float | None = None


class DealView(BaseModel):
    id: str
    title: str | None = None
    stage: str | None = None
    value: float | None = None
    currency: str | None = None
    notes: str | None = None
    lines: list[DealLine] = Field(default_factory=list)


class PriceConversation(BaseModel):
    """A conversation that talks about prices or orders, with the facts the price skill judges from."""

    thread_id: str
    subject: str | None = None
    mailbox: str | None = None
    counterpart: str | None = None
    party_id: str | None = None
    is_starred: bool | None = None
    references: list[str] = Field(default_factory=list)       # quote / RFQ / order numbers seen in the text
    our_price_lines: list[str] = Field(default_factory=list)  # lines from OUR messages with "qty @ price"
    our_last_at: str | None = None
    their_messages: list[DigestMessage] = Field(default_factory=list)   # the other side's newest real messages
    deal: DealView | None = None


# ── run context ──────────────────────────────────────────────────────────────

class OurMailbox(BaseModel):
    id: str
    email: str


class RunContext(BaseModel):
    """Plain facts about who, where and when, gathered before the first LLM call (no LLM)."""

    run_id: str
    instance: str
    request: str
    today: date
    me: Me
    locale: RegimeLocale
    mailboxes: list[OurMailbox]                 # the working set: what the agent reads and may change
    our_addresses: list[str] = Field(default_factory=list)   # every address our login owns: decides who "us" is
    house_rules: str | None = None              # rules/<instance>.md, your standing instructions (memory layer 1)
    only_threads: list[str] | None = None       # a run started by the watcher: only these conversations (Stage 8)
    started_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))

    @property
    def mailbox_ids(self) -> set[str]:
        return {m.id for m in self.mailboxes}


# ── goals ────────────────────────────────────────────────────────────────────

class Goal(BaseModel):
    id: str
    text: str
    skill: str | None = None
    done: bool = False
    answer: str | None = None          # the goal's answer (or the refusal's explanation), when done
    refused: bool = False              # the goal was declined: no skill fits, or the skill called `refuse`
    refusal: RefusalReason | None = None


# ── action ───────────────────────────────────────────────────────────────────

ActionKind = Literal["ok", "not_in_skill", "contract", "guard", "tool_error"]


class WriteRecord(BaseModel):
    """One change the agent made to platform data (also appended to runs/<id>/writes.jsonl)."""

    tool: str
    entity: str
    row_id: str | None
    fields: dict[str, Any]              # what we set
    before: dict[str, Any] = Field(default_factory=dict)   # those fields' values before the write
    dry_run: bool = False               # true = recorded only; the platform was not changed
    key: str | None = None              # the write's outbox key (Revision 12): never sent twice on resume
    at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))


class ActionResult(BaseModel):
    tool: str
    arguments: dict[str, Any]
    kind: ActionKind
    ok: bool
    message: str = ""                   # error / guard reason, in words the model can act on
    preview: str = ""                   # what the model sees (full result, or a preview + artifact id)
    rows: int | None = None
    artifact_id: str | None = None
    write: WriteRecord | None = None
    writes: list[WriteRecord] = Field(default_factory=list)   # a batch tool's writes (one per row)
    uncertain: list[str] = Field(default_factory=list)       # outbox keys of writes a crash left unsettled
    elapsed_ms: float = 0.0


# ── end of run ───────────────────────────────────────────────────────────────

class RunRequest(BaseModel):
    """What was asked — runs/<run_id>/request.json, written before any network call, so even a run
    that fails at login leaves a record."""

    run_id: str
    request: str
    instance: str
    provider: str                           # the primary provider
    model: str                              # the route's first model
    route: str | None = None                # every option in priority order (llm.route_text)
    dry_run: bool = False
    today: date | None = None               # --as-of; None = the real date
    mailboxes: list[str] | None = None      # --mailbox; None = the instance default
    full_sync: bool = False                 # --full-sync: re-read the whole mailbox into the local copy
    cache: bool = False                     # reuse saved verdicts for unchanged conversations (CLI default; not harness)
    approve_writes: bool = False            # --approve-writes: stop before any platform write until you say yes
    history: bool = False                   # show the last runs on these mailboxes to the planner (CLI default; not
                                            # harness: scores must measure the model, not what earlier runs did)
    threads: list[str] | None = None        # only these conversations may be judged or written (the watcher's
                                            # runs: code-enforced, whatever the request text says)
    search: Literal["fts", "hybrid"] | None = None   # --search; None = the SEARCH setting (Stage 9)
    started_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))


# The part of the run that was working when something went wrong.
Where = Literal["connect", "context", "graph", "reconcile", "finish"]

Stopped = Literal["done", "max_steps", "error", "crashed", "interrupted", "waiting"]
# done: every goal answered · max_steps: ran out of steps · error: the model's reply failed its contract twice
# crashed: an exception (LLM, MCP, network …) · interrupted: Ctrl-C or the task was cancelled
# waiting: paused for your approval of its writes (--approve-writes) or on a write it could not settle; resumable


class RunError(BaseModel):
    """Why a run crashed or was interrupted, saved as data (final.json, steps.jsonl, report.md)."""

    type: str                               # e.g. "LlmError", "ContextError", "KeyboardInterrupt"
    message: str
    where: Where
    iter: int                               # the step the loop was on (0 = before the first step)
    traceback: str = ""                     # the last lines of the Python traceback


class FinalAnswer(BaseModel):
    answer: str
    goals: list[Goal]
    refused: bool = False
    reason: str | None = None
    stopped: Stopped
    error: RunError | None = None


class RunOutcome(BaseModel):
    run_id: str
    run_dir: str
    instance: str
    final: FinalAnswer
    writes: list[WriteRecord]
    usage: Usage
    iterations: int
    provider: str | None = None
    model: str | None = None
    route: str | None = None
    served_by: dict[str, int] = Field(default_factory=dict)   # option label → LLM calls it answered
    dry_run: bool = False
    started_at: datetime | None = None
    ended_at: datetime | None = None
