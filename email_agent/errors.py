"""Exceptions turned into words and data (no LLM).

The MCP client wraps failures in exception groups (anyio task groups), so the first thing a
person sees is "unhandled errors in a TaskGroup". `describe` digs out the real causes. Used by
the agent (RunError in final.json) and the harness (SavedRun.error).
"""

from __future__ import annotations

import traceback

from email_agent.contracts.agent import RunError, Where

TRACEBACK_LINES = 30


def leaves(e: BaseException) -> list[BaseException]:
    """The real exceptions inside any nesting of exception groups (just [e] when there is none)."""
    subs = getattr(e, "exceptions", None)
    if not subs:
        return [e]
    return [leaf for sub in subs for leaf in leaves(sub)]


def describe(e: BaseException) -> str:
    """"Type: message" for every real cause, joined with "; "."""
    return "; ".join(f"{type(x).__name__}: {x}" for x in leaves(e))[:2000]


def run_error(e: BaseException, where: Where, it: int) -> RunError:
    found = leaves(e)
    message = str(found[0]) if len(found) == 1 else describe(e)
    # the first real cause's own traceback: where it was raised, not the MCP client's task-group plumbing
    lines = "".join(traceback.format_exception(found[0])).splitlines()[-TRACEBACK_LINES:]
    return RunError(type=type(found[0]).__name__, message=message[:2000], where=where, iter=it,
                    traceback="\n".join(lines))
