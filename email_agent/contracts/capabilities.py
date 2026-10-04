"""Capability and planner contracts (Revision 12, Stage 3).

A capability is a typed task the planner may put in the graph (adapted from S17 `capabilities.py`, which uses dataclasses
and a hand-written argument checker; here every capability's arguments are a pydantic model). Two kinds:
  tool    run through Action: one of the seat's MCP tools or one of our local tools (validated, guarded, recorded)
  graph   one of our graph workers: judge_threads (fan-out into shards), answer, refuse

The planner replies with PlannerOutput (goals in its first call, then tasks), validated here and by the planner's own
checks before anything reaches the graph.
"""

from __future__ import annotations

from datetime import date
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

from email_agent.contracts.agent import RefusalReason
from email_agent.contracts.graph import NODE_ID

NoSkillReason = Literal["out_of_seat", "not_our_mailbox", "not_permitted"]
BulkSkill = Literal["triage-replies", "find-price-agreement", "summarize-threads", "sort-inbox", "follow-up-reminders"]


class Capability(BaseModel):
    """One entry of the manifest the planner sees."""

    model_config = ConfigDict(frozen=True, arbitrary_types_allowed=True)

    name: str
    description: str
    args: Any                                  # the pydantic model its input must pass (a class)
    kind: Literal["tool", "graph"]
    writes: bool = False                       # changes platform data: needs the run's write authority
    terminal: bool = False                     # ends its goal (answer, refuse)
    always: bool = False                       # offered whatever the goals' skills are
    timeout_s: float = 300.0

    def manifest(self) -> dict[str, Any]:
        """Compact form for the planner prompt: argument names with their type or allowed values."""
        schema = self.args.model_json_schema()
        defs = schema.get("$defs", {})
        args = {}
        for name, prop in (schema.get("properties") or {}).items():
            if "$ref" in prop:
                prop = defs.get(prop["$ref"].split("/")[-1], prop)
            kind = prop.get("enum") or prop.get("type") or [p.get("type") for p in prop.get("anyOf", [])]
            args[name] = kind if name in schema.get("required", []) else {"optional": kind}
        return {"name": self.name, "what": self.description, "args": args, "writes": self.writes,
                "ends_goal": self.terminal}


# ── arguments of the graph capabilities ─────────────────────────────────────

class JudgeThreadsInput(BaseModel):
    """`judge_threads`: judge every candidate conversation for one skill, in parallel shards, then write the result."""

    model_config = ConfigDict(extra="forbid")

    skill: BulkSkill
    search: str | None = Field(None, description="Only conversations whose subject, other side or text contains these "
                                                 "words (a party, part, reference, subject word). Omit for all.")
    remind_on: date | None = Field(None, description="follow-up-reminders only: the date the request names, if any.")


class AnswerInput(BaseModel):
    """`answer`: write the goal's answer from what this run found for it. Add it only when the goal's work is done."""

    model_config = ConfigDict(extra="forbid")


class RefuseGoalInput(BaseModel):
    """`refuse`: decline the goal, change nothing."""

    model_config = ConfigDict(extra="forbid")

    reason: RefusalReason
    explanation: str = Field(min_length=5, max_length=800)


# ── what the planner returns ─────────────────────────────────────────────────

class PlannerGoal(BaseModel):
    """One thing the person asked for (first planner call only)."""

    model_config = ConfigDict(extra="forbid")

    text: str = Field(min_length=3, max_length=240, description="What must happen, in a short instruction.")
    skill: str | None = Field(None, description="The one skill that does it (exact name), or null if none may.")
    no_skill_reason: NoSkillReason | None = Field(None, description="Only when skill is null.")
    explanation: str | None = Field(None, max_length=400, description="Only when skill is null: one sentence for the "
                                                                      "person on why it is declined.")


class GoalsOutput(BaseModel):
    goals: list[PlannerGoal] = Field(min_length=1, max_length=8)


class PlannedTask(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str = Field(pattern=NODE_ID, description="A new, unique node id, e.g. judge_g1, answer_g1, party_g2.")
    capability: str
    goal_id: str = Field(description="The goal this task works for, e.g. g1.")
    input: dict[str, Any] = Field(default_factory=dict, description="The capability's arguments.")
    after: list[str] = Field(default_factory=list, description="Node ids this task must wait for.")


class PlannerOutput(BaseModel):
    tasks: list[PlannedTask] = Field(default_factory=list, max_length=4)
    cancel: list[str] = Field(default_factory=list)
    reason: str = Field("", max_length=400, description="One sentence: why these tasks now.")
