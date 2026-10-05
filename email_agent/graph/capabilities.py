"""The capability registry: what the planner may put in the graph (Revision 12, Stage 3).

Adapted from S17 `capabilities.py` (CapabilityRegistry: name, description, argument contract, side effect, terminal).
Here a capability's arguments are a pydantic model, and the list is built from the skills:
  - graph capabilities, ours: `judge_threads` (fan-out over the mailbox for a bulk skill), `answer`, `refuse`;
  - tool capabilities: every MCP or local tool a skill names (SKILL.md `tools`), run through Action.
A goal may use only its own skill's capabilities plus the ones offered to every goal (`answer`, `refuse`), the same rule
the old loop had ("a skill names the tools it needs; nothing else is ever offered").
Internal fan-out steps (`judge_shard`, `join_verdicts`, `apply_writes`) have workers but are never offered: only code
adds them.
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any

from email_agent.common.skill_registry import SkillRegistry
from email_agent.contracts.capabilities import (
    AnswerInput,
    Capability,
    JudgeThreadsInput,
    RefuseGoalInput,
)
from email_agent.contracts.mcp import tool_operation
from email_agent.contracts.tool_args import TOOL_ARGS
from email_agent.platform.action import LOCAL_TOOLS, READ_OPS

GRAPH_CAPABILITIES = {"judge_threads", "answer", "refuse"}
INTERNAL = {"judge_shard", "join_verdicts", "validate_verdicts", "apply_writes"}

JUDGE_TIMEOUT_S = 900.0         # selects and fans out, but the run's first mailbox sync happens inside it (a cold
                                # sync of a 50,000-message mailbox is about 60 calls at ~1 s each)
SHARD_TIMEOUT_S = 900.0         # one LLM call (the route may wait and fall back) + a repair retry
WRITES_TIMEOUT_S = 1800.0       # about 1 s per write on this platform


class Capabilities:
    def __init__(self, skills: SkillRegistry, seat_tools: dict[str, Any]):
        self.skills = skills
        self.items: dict[str, Capability] = {
            "judge_threads": Capability(
                name="judge_threads", kind="graph", args=JudgeThreadsInput, writes=True, timeout_s=JUDGE_TIMEOUT_S,
                description="Whole-mailbox work for a bulk skill: select the candidate conversations, judge them in "
                            "parallel shards, then write what changes (flags, stars + price memories, summaries, "
                            "importance/category, follow-up reminders). Returns counts and the write result."),
            "answer": Capability(
                name="answer", kind="graph", args=AnswerInput, terminal=True, always=True, timeout_s=600.0,
                description="Write this goal's answer from what the graph found for it. Ends the goal."),
            "refuse": Capability(
                name="refuse", kind="graph", args=RefuseGoalInput, terminal=True, always=True, timeout_s=30.0,
                description="Decline this goal with a reason and a one-sentence explanation; changes nothing. "
                            "Ends the goal."),
        }
        for name in sorted(skills.all_tools()):
            if name in self.items or name == "refuse":
                continue
            if name in LOCAL_TOOLS:
                t = LOCAL_TOOLS[name]
                self.items[name] = Capability(name=name, kind="tool", args=t.model, writes=t.writes,
                                              description=t.description[:400])
            else:
                desc = getattr(seat_tools.get(name), "description", "") or ""
                self.items[name] = Capability(name=name, kind="tool", args=TOOL_ARGS[name], description=desc[:300],
                                              writes=not (name.startswith("tools.") or tool_operation(name) in READ_OPS))

    def get(self, name: str) -> Capability | None:
        return self.items.get(name)

    def for_skill(self, skill: str | None) -> list[str]:
        """What a goal with this skill may use (a goal with no skill may only be refused)."""
        if skill is None:
            return ["refuse"]
        own = [t for t in self.skills.get(skill).tools if t in self.items]
        return own + [n for n, c in self.items.items() if c.always and n not in own]

    def manifest(self, skills: Sequence[str | None]) -> list[dict[str, Any]]:
        names: list[str] = []
        for s in skills:
            names += [n for n in self.for_skill(s) if n not in names]
        return [self.items[n].manifest() | {"for_skills": [s for s in skills if n in self.for_skill(s)]}
                for n in names]
