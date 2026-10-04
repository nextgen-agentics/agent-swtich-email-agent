"""Decision (LLM): for ONE goal, with ONE skill's instructions and tools, choose exactly one of
an answer or a tool call. The reply must pass `DecisionOutput`; one retry, then give up cleanly."""

from __future__ import annotations

import json
from pathlib import Path

from pydantic import ValidationError

from email_agent.contracts.agent import DecisionOutput, Goal, HistoryItem, RunContext
from email_agent.contracts.llm import ChatMessage, LlmRequest, ToolSpec
from email_agent.contracts.runlog import DecisionStep, LlmStep
from email_agent.contracts.skills import SkillSpec
from email_agent.llm import Llm
from email_agent.runlog import RunLog

SYSTEM = (Path(__file__).parent / "prompts" / "decision.md").read_text()


def _facts(ctx: RunContext) -> str:
    return json.dumps({
        "today": ctx.today.isoformat(),
        "company_country": ctx.locale.country,
        "currency": ctx.locale.base_currency,
        "currency_symbol": ctx.locale.currency_symbol,
        "date_format": ctx.locale.date_format,
        "person": {"name": ctx.me.name, "email": ctx.me.email},
        "our_mailboxes": [m.email for m in ctx.mailboxes],
    }, indent=1)


OLDER_RESULT_CHARS = 400


def _history(history: list[HistoryItem]) -> str:
    """What Decision sees as DONE SO FAR. The newest result of each tool is shown whole; older results of the same
    tool (earlier pages, earlier reads) are shortened, so the prompt does not grow with every page read. A skill
    saves what it found on a page before reading the next."""
    newest = {h.tool: i for i, h in enumerate(history) if h.kind == "action"}
    out = []
    for i, h in enumerate(history):
        if h.kind == "answer":
            out.append(f"[{h.goal_id}] ANSWER: {h.text}")
            continue
        text = h.text
        if newest.get(h.tool) != i and len(text) > OLDER_RESULT_CHARS:
            text = text[:OLDER_RESULT_CHARS] + " … (older result shortened; call the tool again if you need it)"
        out.append(f"[{h.goal_id}] CALLED {h.tool}({json.dumps(h.arguments, ensure_ascii=False)}) "
                   f"→ {'OK' if h.ok else 'FAILED'}: {text}")
    return "\n".join(out) or "(nothing yet)"


async def next_step(llm: Llm, ctx: RunContext, goal: Goal, skill: SkillSpec, tools: list[ToolSpec],
                    history: list[HistoryItem], log: RunLog, it: int) -> DecisionOutput | None:
    text = "\n\n".join([
        f"RUN FACTS:\n{_facts(ctx)}",
        f"CURRENT GOAL ({goal.id}): {goal.text}",
        f"SKILL '{skill.name}' — INSTRUCTIONS:\n{skill.instructions}",
        f"DONE SO FAR:\n{_history(history)}",
    ])
    messages = [ChatMessage(role="user", text=text)]
    for _ in range(2):
        req = LlmRequest(purpose="decision", system=SYSTEM, messages=messages, tools=tools)
        reply = await llm.chat(req)
        data = {"tool_call": reply.tool_calls[0]} if reply.tool_calls else {"answer": reply.text or None}
        note = f"model proposed {len(reply.tool_calls)} calls; the first is used" if len(reply.tool_calls) > 1 else None
        try:
            out = DecisionOutput.model_validate(data)
        except ValidationError as e:
            log.step(LlmStep(iter=it, layer="decision", request=req, reply=reply, valid=False, error=str(e)[:2000]))
            messages = messages + [ChatMessage(role="user", text="Reply with exactly one tool call, or with the "
                                                                   "answer for this goal as plain text.")]
            continue
        log.step(LlmStep(iter=it, layer="decision", request=req, reply=reply, valid=True, error=note))
        log.step(DecisionStep(iter=it, goal_id=goal.id, skill=skill.name, offered_tools=[t.name for t in tools],
                              output=out))
        return out
    return None
