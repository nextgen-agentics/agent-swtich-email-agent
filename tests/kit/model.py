"""A scripted model in place of the LLM route. Each graph role (goals, planner, judge, validator, critic, answer) has
its own script, and every request is kept so a test can check what the model was shown."""

from __future__ import annotations

import asyncio
import json
import re
from collections.abc import Callable
from typing import Any

from email_agent.contracts.llm import LlmReply, LlmRequest, Usage

IDS = re.compile(r"Return exactly one verdict for each of these thread ids: (\[.*\])")
SHARDS = {"TriageShard": "triage-replies", "PriceShard": "find-price-agreement", "SummaryShard": "summarize-threads",
          "SortShard": "sort-inbox", "FollowUpShard": "follow-up-reminders"}
BULK = set(SHARDS.values())

Script = Callable[[LlmRequest], Any]
Verdicts = Callable[[str], dict[str, Any]]


def goal(text: str, skill: str | None, reason: str | None = None) -> dict[str, Any]:
    if skill is None:
        return {"text": text, "skill": None, "no_skill_reason": reason or "out_of_seat",
                "explanation": "This agent works on email only and cannot read that."}
    return {"text": text, "skill": skill}


def task(node: str, capability: str, goal_id: str, after: list[str] | None = None, **input: Any) -> dict[str, Any]:
    return {"id": node, "capability": capability, "goal_id": goal_id, "input": input, "after": after or []}


def planner_body(req: LlmRequest) -> dict[str, Any]:
    return json.loads(req.messages[-1].text)


def judge_then_answer(body: dict[str, Any]) -> dict[str, Any]:
    """What a sensible planner does with bulk skills: judge the mailbox, then answer once the work is done."""
    tasks = []
    for g in body["goals"]:
        if g["status"] != "open" or g["skill"] is None:
            continue
        mine = [n for n in body["graph"] if n["goal_id"] == g["id"]]
        if not mine and g["skill"] in BULK:
            tasks.append(task(f"judge_{g['id']}", "judge_threads", g["id"], skill=g["skill"]))
        elif mine and not any(n["state"] in ("pending", "running", "waiting") for n in mine):
            tries = sum(n["capability"] == "answer" for n in mine)           # a failed answer is tried again
            tasks.append(task(f"answer_{g['id']}" + (f"_{tries + 1}" if tries else ""), "answer", g["id"]))
    return {"tasks": tasks, "reason": "judge the mailbox, then answer"}


def skill_of(req: LlmRequest) -> str:
    return SHARDS[(req.response_schema or {}).get("title", "")]


def thread_ids(req: LlmRequest) -> list[str]:
    found = IDS.search(req.messages[0].text)
    assert found, "a judging request always names its conversations"
    return json.loads(found.group(1))


class ScriptedModel:
    def __init__(self, *, goals: list[dict[str, Any]], plan: Callable[[dict[str, Any]], dict[str, Any]] = judge_then_answer,
                 verdict: Verdicts | dict[str, Verdicts] | None = None, answer: str = "Done: see the conversations above.",
                 critic: Script | None = None, model: str = "fake-flash", delay: float = 0.0):
        self.model, self.delay = model, delay
        self.requests: list[LlmRequest] = []
        self.in_flight = self.peak = 0
        self.scripts: dict[str, Script] = {
            "goals": lambda req: {"goals": goals},
            "planner": lambda req: plan(planner_body(req)),
            "judge": lambda req: {"verdicts": [{"thread_id": t, **_pick(verdict, req)(t)} for t in thread_ids(req)]},
            "answer": lambda req: answer,
            "critic": critic or _ready_or_score,
        }
        self.scripts["validator"] = self.scripts["judge"]

    def calls(self, purpose: str) -> list[LlmRequest]:
        return [r for r in self.requests if r.purpose == purpose]

    async def chat(self, req: LlmRequest) -> LlmReply:
        self.requests.append(req)
        if self.delay:
            self.in_flight += 1
            self.peak = max(self.peak, self.in_flight)
            try:
                await asyncio.sleep(self.delay)
            finally:
                self.in_flight -= 1
        out = self.scripts[req.purpose](req)
        if isinstance(out, Exception):
            raise out
        text = out if isinstance(out, str) else json.dumps(out)
        prompt = len(req.system) + sum(len(m.text) for m in req.messages)
        return LlmReply(model=self.model, text=text, usage=Usage(input_tokens=prompt // 4, output_tokens=len(text) // 4))


def _pick(verdict: Verdicts | dict[str, Verdicts] | None, req: LlmRequest) -> Verdicts:
    if isinstance(verdict, dict):
        return verdict[skill_of(req)]
    return verdict or _no_reply


def _no_reply(_thread_id: str) -> dict[str, Any]:
    return {"needs_reply": False, "why": "nothing is asked of us"}


def _ready_or_score(req: LlmRequest) -> dict[str, Any]:
    # the evidence critic and the answer's verifier share the "critic" purpose; their schemas tell them apart
    if (req.response_schema or {}).get("title") == "VerifierScore":
        return {"score": 90, "critique": "matches the evidence", "issues": []}
    return {"ready": True, "missing": [], "reason": "the evidence covers the goal"}


def rounds(*batches: list[dict[str, Any]]) -> Callable[[dict[str, Any]], dict[str, Any]]:
    """The planner's tasks round by round (each round once the last one's work is done); then answer every open goal."""
    left = iter(batches)

    def plan(body: dict[str, Any]) -> dict[str, Any]:
        if any(n["state"] in ("pending", "running", "waiting") for n in body["graph"]):
            return {"tasks": [], "reason": "waiting for the running work"}
        batch = next(left, None)
        if batch is not None:
            return {"tasks": batch, "reason": "the next step"}
        return {"tasks": [task(f"answer_{g['id']}", "answer", g["id"]) for g in body["goals"]
                          if g["status"] == "open" and g["skill"]], "reason": "the work is done"}
    return plan
