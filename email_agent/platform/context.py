"""Who, where and when — gathered before the first LLM call, with no LLM involved."""

from __future__ import annotations

import asyncio
from datetime import date

from email_agent.config import Settings
from email_agent.contracts.agent import OurMailbox, RunContext
from email_agent.contracts.platform import ListPage, Mailbox
from email_agent.contracts.tool_args import TOOL_ARGS
from email_agent.platform.mcp_session import McpSession
from email_agent.platform.rest import RestClient


class ContextError(Exception):
    pass


def select_mailboxes(mailboxes: list[Mailbox], wanted: list[str] | None) -> list[Mailbox]:
    """The working set: the named addresses (all must be ours), or every mailbox when none are named."""
    if not wanted:
        return mailboxes
    by_email = {(m.email or "").lower(): m for m in mailboxes}
    unknown = [w for w in wanted if w.lower() not in by_email]
    if unknown:
        raise ContextError(f"not a mailbox of this login: {unknown}; ours are {sorted(by_email)}")
    return [by_email[w.lower()] for w in wanted]


async def build_context(settings: Settings, mcp: McpSession, instance: str, request: str, run_id: str,
                        today: date | None = None, mailboxes: list[str] | None = None,
                        only_threads: list[str] | None = None) -> RunContext:
    """`mailboxes`: addresses to work in; None = the instance default (config.INSTANCES), else every mailbox."""
    rest = RestClient(settings, instance)
    me, locale = await asyncio.to_thread(lambda: (rest.me(), rest.display_locale()))
    outcome = await mcp.call("Mailbox.list", TOOL_ARGS["Mailbox.list"].model_validate({"limit": 100}))
    if not outcome.ok:
        raise ContextError(f"Mailbox.list failed: {outcome.error.message if outcome.error else outcome.text}")
    page = ListPage[Mailbox].model_validate(outcome.data())
    owned = [m for m in page.data if m.email]
    if not owned:
        raise ContextError("this login has no mailbox on this instance")
    working = select_mailboxes(owned, mailboxes if mailboxes is not None else settings.instance(instance).mailboxes)
    return RunContext(run_id=run_id, instance=instance, request=request, today=today or date.today(),
                      me=me, locale=locale, mailboxes=[OurMailbox(id=m.id, email=(m.email or "").lower()) for m in working],
                      our_addresses=sorted((m.email or "").lower() for m in owned), house_rules=settings.house_rules(instance),
                      only_threads=only_threads)
