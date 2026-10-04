"""Perception (LLM): keep the goal list, and name the skill for each goal.

Like S7, goals are identified by position; like the S9 planner, Perception names skills, never
tools. Goals already known keep their id, text, skill and done-state (done is set by the loop
when Decision answers); Perception may only append new goals. Its reply must pass
`PerceptionOutput` (skill names checked against the registry); one retry with the error text,
then the run stops cleanly.
"""

from __future__ import annotations

import json
from pathlib import Path

from pydantic import ValidationError

from email_agent.contracts.agent import Goal, HistoryItem, Observation, PerceptionOutput, RunContext
from email_agent.contracts.llm import ChatMessage, LlmRequest
from email_agent.contracts.runlog import LlmStep
from email_agent.llm import Llm
from email_agent.runlog import RunLog
from email_agent.skill_registry import SkillRegistry

SYSTEM = (Path(__file__).parent / "prompts" / "perception.md").read_text()


def _history_lines(history: list[HistoryItem]) -> str:
    lines = []
    for h in history[-12:]:
        what = f"answer: {h.text[:300]}" if h.kind == "answer" else f"{h.tool} ok={h.ok}: {h.text[:160]}"
        lines.append(f"- [{h.goal_id}] {what}")
    return "\n".join(lines) or "(nothing yet)"


async def observe(llm: Llm, ctx: RunContext, registry: SkillRegistry, goals: list[Goal],
                  history: list[HistoryItem], log: RunLog, it: int) -> Observation | None:
    text = "\n\n".join([
        f"REQUEST:\n{ctx.request}",
        "OUR MAILBOXES (the only ones this agent may read or change):\n" + ", ".join(m.email for m in ctx.mailboxes),
        "SKILLS:\n" + json.dumps([c.model_dump() for c in registry.catalogue()], indent=1),
        "CURRENT GOALS:\n" + (json.dumps([{"text": g.text, "skill": g.skill, "done": g.done} for g in goals],
                                         indent=1) if goals else "(none yet)"),
        f"HISTORY:\n{_history_lines(history)}",
    ])
    messages = [ChatMessage(role="user", text=text)]
    for _ in range(2):
        req = LlmRequest(purpose="perception", system=SYSTEM, messages=messages,
                         response_schema=PerceptionOutput.model_json_schema(), temperature=0.1)
        reply = await llm.chat(req)
        try:
            out = PerceptionOutput.model_validate_json(reply.text, context={"skills": set(registry.names())})
        except ValidationError as e:
            log.step(LlmStep(iter=it, layer="perception", request=req, reply=reply, valid=False, error=str(e)[:2000]))
            messages = messages + [ChatMessage(role="model", text=reply.text or "(empty)"),
                                   ChatMessage(role="user", text=f"That reply was rejected: {e}. Return corrected JSON.")]
            continue
        log.step(LlmStep(iter=it, layer="perception", request=req, reply=reply, valid=True))
        merged = list(goals)
        for i, d in enumerate(out.goals[len(goals):], start=len(goals)):
            merged.append(Goal(id=f"g{i + 1}", text=d.text, skill=d.skill,
                               refusal=(d.no_skill_reason or "out_of_seat") if d.skill is None else None))
        return Observation(goals=merged)
    return None
