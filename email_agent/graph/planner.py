"""RunPlanner: the LLM that grows the run's graph (Revision 12, Stage 3). Replaces Perception and Decision.

Adapted from S17 `planner.py` (GeneralAgentPlanner). Kept: the planner sees the goal, the capability manifest, the
graph with clipped outcomes and the event that woke it, and returns only the next frontier (at most 4 tasks); code
checks every proposal (known capability, valid arguments, no repeated work, dependencies that exist); a rejected
proposal goes back with the reason, at most 3 times, then the run ends visibly (no hidden fallback).

Changed for this agent:
  - The first call of a run is the goals step (the old Perception's rules): one goal per thing asked, each with its one
    skill, or none with a refusal reason. A goal with no skill is refused by code at once.
  - A goal's skill instructions go into the planner prompt as soon as the goal has picked it (S17 loads skills with an
    explicit `load_skill` task; here the goals step is that decision, and it is journalled).
  - The run finishes by code once every goal has a succeeded `answer` or `refuse` — the LLM never decides "finish".
  - `answer` added while its goal still has open work is made to wait for that work instead of being refused; it never
    waits on a failed or cancelled task (it would never run — found by the Stage 6 reject drill).
  - A write to a conversation the validator held for the user's check is refused by code (Stage 6 drill: the critic
    asked to settle a held conversation and the planner flagged it with a plain tool task, around the hold).
  - Shards never wake the planner; only a fan-out's write step does, so planner calls grow with the request.
  - Every call is charged to the run's `planner_rounds` and `llm_calls` budgets (saved in run.sqlite).
  - Memory (Stage 7): your house rules (`rules/<instance>.md`, layer 1) follow the planner's instructions; the first
    round also sees the last runs on the same mailbox(es) (`recent_runs`, layer 7), as history only; a graph that grows
    past GRAPH_CHARS is folded behind a visible `compacted_before` marker (S17), never cut silently (layer 8).
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from pydantic import ValidationError

from email_agent.common.skill_registry import SkillRegistry
from email_agent.contracts.agent import Goal, RunContext
from email_agent.contracts.capabilities import GoalsOutput, PlannerOutput
from email_agent.contracts.graph import (
    ACTIVE_STATES,
    GraphPatch,
    GraphSnapshot,
    JournalEvent,
    NodeState,
    TaskSpec,
)
from email_agent.contracts.llm import ChatMessage, LlmRequest
from email_agent.contracts.runlog import LlmStep, PlanStep
from email_agent.graph.capabilities import Capabilities
from email_agent.graph.store import BudgetExceeded
from email_agent.graph.workers import DRY_RUN_NOTE, GraphRuntime, house_rules, run_facts

PROMPTS = Path(__file__).resolve().parent.parent / "prompts"
GOALS_SYSTEM = (PROMPTS / "goals.md").read_text()
PLANNER_SYSTEM = (PROMPTS / "planner.md").read_text()
REPAIRS = 3
RESULT_CHARS = 4000              # per node in the planner prompt (S17's clip); shards are left out
GRAPH_CHARS = 40_000             # the whole graph in the planner prompt; past it, finished work is folded (layer 8)
HISTORY_NOTE = ("History only: what earlier runs on these mailboxes did. The mailbox may have changed since, so never "
                "skip, repeat or undo work because of it; use it only when the request refers to an earlier run.")
TERMINAL = {"answer", "refuse"}


KEEP = {"id", "name", "_display", "display_name", "subject", "email", "party_id", "thread_id", "category", "content",
        "is_active", "status", "type", "folder", "mailbox_id", "from_email", "snippet", "stage", "title", "total",
        "next_offset", "summary", "importance", "split_category", "flag_status", "counterpart"}


def _for_planner(result: dict[str, Any]) -> str:
    """A node's result as the planner sees it. A tool's list result is cut down to its rows' key fields first, so
    every row fits (found live: 5 parties clipped to 2, and the planner picked the wrong one)."""
    raw = result.get("result")
    if isinstance(raw, str):
        try:
            data = json.loads(raw)
        except json.JSONDecodeError:
            data = None
        if isinstance(data, dict) and isinstance(data.get("rows"), list):
            rows = [{k: (v[:160] if isinstance(v, str) else v) for k, v in row.items() if k in KEEP}
                    for row in data["rows"] if isinstance(row, dict)]
            result = {**result, "result": {"total": data.get("total"), "rows": rows}}
    return json.dumps(result, ensure_ascii=False, default=str)[:RESULT_CHARS]


class PlannerFailed(RuntimeError):
    pass


class PlanRejected(ValueError):
    pass


class RunPlanner:
    def __init__(self, rt: GraphRuntime, caps: Capabilities, skills: SkillRegistry, ctx: RunContext,
                 history: list[str] | None = None):
        self.rt, self.caps, self.skills, self.ctx = rt, caps, skills, ctx
        self.history = history or []           # episode lines of the last runs (layer 7), fixed at the run's start
        self.goals: dict[str, Goal] = {}
        self.budget_hit: str | None = None
        self._seen_failures: set[str] = set()

    # ── the Planner protocol ─────────────────────────────────────────────────
    async def plan(self, graph: GraphSnapshot, event: JournalEvent) -> GraphPatch:
        """Stuck tasks (pending, but waiting on a task that failed or was cancelled, so they can never run) are
        cancelled with this round's patch, and the round plans as if they were gone — e.g. an answer that waited on a
        write you declined is replaced by one that says nothing was written (found by the Stage 6 reject drill)."""
        stuck = self._stuck(graph)
        if stuck:
            graph = graph.model_copy(update={"nodes": {
                k: v.model_copy(update={"state": NodeState.CANCELLED}) if k in stuck else v
                for k, v in graph.nodes.items()}})
        patch = await self._plan(graph, event)
        if stuck:
            patch = patch.model_copy(update={"cancel": [*patch.cancel, *sorted(stuck - set(patch.cancel))],
                                             "reason": (patch.reason + f" [cancelled, can never run: "
                                                                       f"{', '.join(sorted(stuck))}]")[:500]})
        return patch

    @staticmethod
    def _stuck(graph: GraphSnapshot) -> set[str]:
        dead = {k for k, n in graph.nodes.items() if n.state in (NodeState.FAILED, NodeState.CANCELLED)}
        parents: dict[str, set[str]] = {}
        for parent, child in graph.edges:
            parents.setdefault(child, set()).add(parent)
        stuck: set[str] = set()
        while True:
            more = {k for k, n in graph.nodes.items() if n.state == NodeState.PENDING and k not in stuck
                    and parents.get(k, set()) & (dead | stuck)}
            if not more:
                return stuck
            stuck |= more

    async def _plan(self, graph: GraphSnapshot, event: JournalEvent) -> GraphPatch:
        if not self.goals:
            self._recover(graph)
        if self.goals and self._all_ended(graph):
            return GraphPatch(finish=True, reason="every goal is answered or refused")
        if self.goals and event.kind != "run_started" and self._every_open_goal_busy(graph):
            return GraphPatch(reason="every open goal still has work running: nothing to decide yet")
        try:
            self.rt.store.spend("planner_rounds", 1)
        except BudgetExceeded as e:
            self.budget_hit = str(e)
            return GraphPatch(finish=True, reason=f"planner budget used up: {e}", metadata={"budget": "planner_rounds"})
        self.rt.round += 1
        trigger = "run_started" if event.kind == "run_started" else f"{event.node_id} {event.kind.split('_')[1]}"
        new_goals: list[Goal] = []
        if not self.goals:
            new_goals = await self._goals_step()
            self.goals = {g.id: g for g in new_goals}
        rejected: list[str] = []
        auto = self._refuse_goals_without_skill(graph) if new_goals else []
        for attempt in range(REPAIRS + 1):
            try:
                out = await self._tasks_step(graph, event, rejected, auto)
                patch = self._check(out, graph, auto)
                break
            except PlanRejected as e:
                rejected.append(str(e))
        else:
            self.rt.log.step(PlanStep(iter=self.rt.round, trigger=trigger, goals=new_goals, rejected=rejected,
                                      reason="no valid plan after repairs"))
            raise PlannerFailed(f"the planner's proposals were rejected {REPAIRS + 1} times: {rejected[-1]}")
        meta: dict[str, Any] = {"round": self.rt.round}
        if new_goals:
            meta["goals"] = [g.model_dump(mode="json") for g in new_goals]
        if rejected:
            meta["rejected"] = rejected
        patch = patch.model_copy(update={"metadata": meta})
        self.rt.log.step(PlanStep(iter=self.rt.round, trigger=trigger, goals=new_goals, rejected=rejected,
                                  added=[f"{t.id}: {t.capability}({json.dumps(t.input, ensure_ascii=False)[:120]})"
                                         for t in patch.add], cancelled=patch.cancel, reason=patch.reason))
        return patch

    # ── goals ────────────────────────────────────────────────────────────────
    async def _goals_step(self) -> list[Goal]:
        text = "\n\n".join([
            f"REQUEST:\n{self.ctx.request}",
            "OUR MAILBOXES (the only ones this agent may read or change):\n" + ", ".join(m.email for m in self.ctx.mailboxes),
            "SKILLS:\n" + json.dumps([c.model_dump() for c in self.skills.catalogue()], indent=1)])
        messages = [ChatMessage(role="user", text=text)]
        known = set(self.skills.names())
        for _ in range(2):
            req = LlmRequest(purpose="goals", system=GOALS_SYSTEM, messages=messages,
                             response_schema=GoalsOutput.model_json_schema(), temperature=0.1)
            reply = await self.rt.chat(req, None, "goals")
            try:
                out = GoalsOutput.model_validate_json(reply.text)
                bad = [g.skill for g in out.goals if g.skill is not None and g.skill not in known]
                if bad:
                    raise ValueError(f"unknown skill(s) {bad}; choose from {sorted(known)} or null")
            except (ValidationError, ValueError) as e:
                self.rt.log.step(LlmStep(iter=self.rt.round, layer="goals", request=req, reply=reply, valid=False,
                                         error=str(e)[:2000]))
                messages = messages + [ChatMessage(role="model", text=reply.text or "(empty)"),
                                       ChatMessage(role="user", text=f"That reply was rejected: {e}. Return corrected JSON.")]
                continue
            self.rt.log.step(LlmStep(iter=self.rt.round, layer="goals", request=req, reply=reply, valid=True))
            return [Goal(id=f"g{i}", text=g.text, skill=g.skill,
                         refusal=(g.no_skill_reason or "out_of_seat") if g.skill is None else None,
                         answer=(g.explanation or "No skill of this agent may do this.") if g.skill is None else None)
                    for i, g in enumerate(out.goals, start=1)]
        raise PlannerFailed("the goals step did not return a valid goal list")

    def _refuse_goals_without_skill(self, graph: GraphSnapshot) -> list[TaskSpec]:
        return [TaskSpec(id=f"refuse_{g.id}", capability="refuse", goal_id=g.id, timeout_s=30.0,
                         input={"reason": g.refusal, "explanation": g.answer or "No skill of this agent may do this."})
                for g in self.goals.values() if g.skill is None and f"refuse_{g.id}" not in graph.nodes]

    def restore(self) -> None:
        """On resume, before any node runs again: the goals from the journal, the round count from the saved budget."""
        self._recover(self.rt.store.snapshot())
        try:
            self.rt.round = int(self.rt.store.budget("planner_rounds").spent)
        except KeyError:
            pass

    def _recover(self, graph: GraphSnapshot) -> None:
        """After a resume: the goals were journalled with the first patch."""
        for event in self.rt.store.events():
            if event.kind == "graph_patched" and event.payload.get("goals"):
                self.goals = {g["id"]: Goal.model_validate(g) for g in event.payload["goals"]}
                return

    # ── tasks ────────────────────────────────────────────────────────────────
    def _status(self, graph: GraphSnapshot, goal_id: str) -> str:
        for n in graph.nodes.values():
            if n.goal_id == goal_id and n.capability in TERMINAL and n.state == NodeState.SUCCEEDED:
                return "answered" if n.capability == "answer" else "refused"
        return "open"

    def _every_open_goal_busy(self, graph: GraphSnapshot) -> bool:
        """True when no LLM call is needed: each open goal has work pending or running, and the event that woke us
        was not a failure (a failure always gets a planner look)."""
        open_goals = [g for g in self.goals if self._status(graph, g) == "open"]
        busy = {n.goal_id for n in graph.nodes.values() if n.state in ACTIVE_STATES}
        return bool(open_goals) and all(g in busy for g in open_goals) and not self._last_failed(graph)

    def _last_failed(self, graph: GraphSnapshot) -> bool:
        """True once per new failure: a failure seen here is remembered, so it wakes the planner only once."""
        failed = {n.id for n in graph.nodes.values() if n.state == NodeState.FAILED}
        new = bool(failed - self._seen_failures)
        self._seen_failures |= failed
        return new

    def _all_ended(self, graph: GraphSnapshot) -> bool:
        return all(self._status(graph, g) != "open" for g in self.goals)

    async def _tasks_step(self, graph: GraphSnapshot, event: JournalEvent, rejected: list[str],
                          auto: list[TaskSpec]) -> PlannerOutput:
        open_goals = [g for g in self.goals.values() if self._status(graph, g.id) == "open" and g.skill]
        if not open_goals:
            return PlannerOutput(reason="only goals with no skill: refused by code")
        skills = list(dict.fromkeys(g.skill for g in open_goals if g.skill))
        system = PLANNER_SYSTEM + house_rules(self.ctx) + "".join(
            f"\n\n## Skill '{s}' — instructions\n{self.skills.get(s).instructions}" for s in skills)
        nodes, compacted = self._graph_view(graph)
        body = {
            "request": self.ctx.request,
            "run_facts": run_facts(self.ctx),
            **({"dry_run": DRY_RUN_NOTE} if self.rt.writes.dry_run else {}),
            "goals": [{"id": g.id, "text": g.text, "skill": g.skill, "status": self._status(graph, g.id)}
                      for g in self.goals.values()],
            "capabilities": self.caps.manifest(skills),
            "graph": nodes,
            **({"compacted_before": compacted} if compacted else {}),
            **({"recent_runs": {"note": HISTORY_NOTE, "runs": self.history}}
               if self.history and event.kind == "run_started" else {}),
            "event": {"kind": event.kind, "node_id": event.node_id},
            "already_added_by_code": [t.id for t in auto],
            "limits": {"max_new_tasks": 4},
        }
        if rejected:
            body["your_previous_proposals_were_rejected"] = rejected
        req = LlmRequest(purpose="planner", system=system,
                         messages=[ChatMessage(role="user", text=json.dumps(body, ensure_ascii=False, indent=1))],
                         response_schema=PlannerOutput.model_json_schema(), temperature=0.1)
        reply = await self.rt.chat(req, None, "planner")
        try:
            out = PlannerOutput.model_validate_json(reply.text)
        except ValidationError as e:
            self.rt.log.step(LlmStep(iter=self.rt.round, layer="planner", request=req, reply=reply, valid=False,
                                     error=str(e)[:2000]))
            raise PlanRejected(f"the reply did not match the schema: {str(e)[:600]}") from None
        self.rt.log.step(LlmStep(iter=self.rt.round, layer="planner", request=req, reply=reply, valid=True))
        return out

    def _graph_view(self, graph: GraphSnapshot) -> tuple[list[dict[str, Any]], dict[str, Any] | None]:
        """The graph as the planner sees it, and the `compacted_before` marker when it had to be folded (S17 layer 8:
        the marker says what was left out, so nothing disappears silently). First the finished goals' tasks go (their
        answer or refusal stays), then the oldest succeeded results."""
        nodes = []
        for n in graph.nodes.values():
            if n.capability == "judge_shard":
                continue                                     # the join and write steps carry their outcome
            item: dict[str, Any] = {"id": n.id, "capability": n.capability, "goal_id": n.goal_id, "state": n.state,
                                    "input": n.input if n.capability != "join_verdicts" else {"shards": len(n.input.get("shards", []))}}
            if n.result is not None:
                item["result"] = _for_planner(n.result)
            if n.error is not None:
                item["error"] = n.error.message[:600]
            nodes.append(item)

        def size() -> int:
            return len(json.dumps(nodes, ensure_ascii=False, default=str))
        if size() <= GRAPH_CHARS:
            return nodes, None
        ended = {g for g in self.goals if self._status(graph, g) != "open"}
        folded = [x["id"] for x in nodes if x["goal_id"] in ended and x["capability"] not in TERMINAL]
        nodes = [x for x in nodes if x["id"] not in folded]
        dropped = []
        for x in nodes:
            if size() <= GRAPH_CHARS:
                break
            if x["state"] == NodeState.SUCCEEDED and "result" in x and x["capability"] not in TERMINAL:
                x["result"] = "(compacted: see compacted_before)"
                dropped.append(x["id"])
        return nodes, {"why": f"the graph passed {GRAPH_CHARS} characters", "tasks_of_ended_goals_left_out": folded,
                       "results_left_out": dropped}

    def _check(self, out: PlannerOutput, graph: GraphSnapshot, auto: list[TaskSpec]) -> GraphPatch:
        """Code's checks before anything reaches the graph (S17 `_parse`, for our capabilities)."""
        known = set(graph.nodes) | {t.id for t in auto}
        add: list[TaskSpec] = list(auto)
        connect: list[tuple[str, str]] = []
        ending = {n.goal_id for n in graph.nodes.values() if n.capability in TERMINAL
                  and n.state in ACTIVE_STATES | {NodeState.SUCCEEDED}} | {t.goal_id for t in auto}
        dropped: list[str] = []
        twins: dict[str, str] = {}          # a task dropped as a repeat → the node it repeats (for later `after`s)
        problems: list[str] = []
        held: dict[str, set[str]] = {}       # goal → conversations the two models disagree on (held or possible miss)
        for n in graph.nodes.values():
            if n.capability == "validate_verdicts" and n.result and n.goal_id:
                held.setdefault(n.goal_id, set()).update(h.get("thread_id") for h in
                                                         [*n.result.get("held", []), *n.result.get("possible_misses", [])])
        for t in out.tasks:
            goal = self.goals.get(t.goal_id)
            if goal is None:
                problems.append(f"{t.id}: unknown goal {t.goal_id!r}")
                continue
            if self._status(graph, goal.id) != "open":
                dropped.append(f"{t.id} (goal {goal.id} already ended)")
                continue
            if goal.skill is None:
                dropped.append(f"{t.id} (goal {goal.id} has no skill: code refuses it)")
                continue
            if t.capability in TERMINAL and goal.id in ending:
                dropped.append(f"{t.id} (goal {goal.id} already has an answer or refusal)")
                continue
            if t.capability not in self.caps.for_skill(goal.skill):
                problems.append(f"{t.id}: {t.capability!r} is not offered for goal {goal.id} (skill {goal.skill}); "
                                f"use one of {self.caps.for_skill(goal.skill)}")
                continue
            cap = self.caps.get(t.capability)
            if cap is None:                          # offered for the skill, so registered: this cannot happen
                problems.append(f"{t.id}: unknown capability {t.capability!r}")
                continue
            try:
                args = cap.args.model_validate(t.input)
            except ValidationError as e:
                # Stage 7 dry run: the planner added remember_fact with no arguments "after" the recall it wanted to
                # see first, three times over; the bare schema error did not tell it what to do instead.
                hint = (" A task is added with its complete arguments. If they depend on a result you have not seen "
                        "yet, add only the task that produces it now, and this one in a later round.") \
                    if not t.input else ""
                problems.append(f"{t.id}: arguments of {t.capability} are invalid: {str(e)[:400]}{hint}")
                continue
            if t.id in known:
                problems.append(f"{t.id}: this node id already exists; choose a new one")
                continue
            clean = args.model_dump(mode="json", exclude_unset=True)
            twin = next((n for n in graph.nodes.values() if n.capability == t.capability and n.goal_id == goal.id
                         and n.input == clean and n.state in ACTIVE_STATES | {NodeState.SUCCEEDED}), None)
            if twin is not None:
                dropped.append(f"{t.id} (same as {twin.id})")
                twins[t.id] = twin.id
                continue
            # a dependency on a task dropped as a repeat means its twin (Stage 7 harness: answer_g1_final waited on
            # judge_g1_retry, a repeat of judge_g1, and every proposal was rejected as "unknown node")
            after = list(dict.fromkeys(twins.get(a, a) for a in t.after))
            unknown = [a for a in after if a not in known]
            if unknown:
                problems.append(f"{t.id}: waits for unknown node(s) {unknown}")
                continue
            dead = [a for a in after if a in graph.nodes
                    and graph.nodes[a].state in (NodeState.FAILED, NodeState.CANCELLED)]
            if dead and t.capability == "answer":             # an answer reports the failure; it must not wait on it
                after = [a for a in after if a not in dead]
            elif dead:
                problems.append(f"{t.id}: waits for {dead}, which failed or was cancelled and will never succeed")
                continue
            row = clean.get("id") or clean.get("thread_id")
            if cap.writes and row and row in held.get(goal.id, set()):
                problems.append(f"{t.id}: conversation {row} is for the user's check (the two models judged it "
                                "differently); the agent never writes it — list it in the answer under 'Needs your "
                                "check'")
                continue
            if t.capability == "answer" and any(a.goal_id == goal.id and a.capability not in TERMINAL
                                                and a not in auto for a in add):
                # Stage 7 harness: an answer added with its goal's work, before any result, made the goal "busy", so
                # the planner never saw the result and never refused (refuse-price-not-agreed: a quote request only).
                # How a goal ends — answer or refuse — is decided once its work's result is in.
                dropped.append(f"{t.id} (decide how goal {goal.id} ends after this round's work has a result)")
                continue
            if t.capability == "answer":                    # wait for the goal's open work instead of refusing
                after += [n.id for n in graph.nodes.values() if n.goal_id == goal.id and n.state in ACTIVE_STATES
                          and n.id not in after]
                after += [a.id for a in add if a.goal_id == goal.id and a.id not in after]   # same-round work too
            add.append(TaskSpec(id=t.id, capability=t.capability, goal_id=goal.id, input=clean, timeout_s=cap.timeout_s))
            if t.capability in TERMINAL:
                ending.add(goal.id)
            connect += [(a, t.id) for a in dict.fromkeys(after)]
            known.add(t.id)
        cancel = [c for c in out.cancel if c in graph.nodes and graph.nodes[c].state in ACTIVE_STATES]
        if problems:
            raise PlanRejected("; ".join(problems))
        active = any(n.state in ACTIVE_STATES for n in graph.nodes.values())
        if not add and not active and not self._all_ended(graph):
            raise PlanRejected("nothing new to do, but goals are still open: add the next task for each open goal, "
                               "or answer / refuse it" + (f" (dropped as repeats: {dropped})" if dropped else ""))
        reason = out.reason + (f" [dropped as repeats: {', '.join(dropped)}]" if dropped else "")
        return GraphPatch(add=add, connect=connect, cancel=cancel, reason=reason[:400])

    # ── the run's goals at the end ───────────────────────────────────────────
    def final_goals(self, graph: GraphSnapshot) -> list[Goal]:
        """Each goal with its answer or refusal; if a goal somehow has two, the first one to finish counts."""
        goals = []
        ends = sorted((n for n in graph.nodes.values() if n.state == NodeState.SUCCEEDED and n.capability in TERMINAL),
                      key=lambda n: (str(n.ended_at), n.id))
        for g in self.goals.values():
            g = g.model_copy()
            for n in ends:
                if n.goal_id != g.id or g.done:
                    continue
                if n.capability == "answer":
                    g.done, g.answer = True, (n.result or {}).get("text")
                else:
                    g.done, g.refused = True, True
                    g.refusal, g.answer = (n.result or {}).get("reason"), (n.result or {}).get("explanation")
            goals.append(g)
        return goals
