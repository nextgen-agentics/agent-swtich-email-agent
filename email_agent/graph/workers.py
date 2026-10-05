"""Graph workers (Revision 12, Stages 3–4): one async function per capability, all sharing one run's tools.

  tool capabilities   one MCP or local tool call through Action (validated, guarded; writes through WritePath)
  judge_threads       select the candidates (flows.py, SQL on the local copy) and fan out: shards → join → write
  judge_shard         one LLM call judging up to SHARD_SIZE conversations (schema-bound verdicts, ids checked,
                      one repair retry); the verdict cache answers conversations that have not changed
  join_verdicts       collect the shards' verdicts, check them, build the write plan (code)
  validate_verdicts   the cross-model check (Stage 5, S17 `validate_work`): another model re-judges every verdict that
                      would cause a write (+ a small sample of the rest); only agreed ones are written, disputed ones held
  apply_writes        send the plan through WritePath (guard, dry run, outbox, record)
  answer              the evidence critic (S17), then one LLM call for the goal's answer, then a verifier score
  refuse              decline a goal (no LLM; an evidence-based refusal goes past the critic first)

Every LLM call is charged to the run's `llm_calls` budget first and logged as an LlmStep (with its node id); every node
outcome is logged as a NodeStep. A write left uncertain by a crash parks its node (Deferred "outbox.reconcile").
Stage 6: a reply saved in run.sqlite for the same node and the same request is reused (after a resume) instead of
calling the model again; with the approval gate on (--approve-writes), a node about to write parks
(Deferred "approval.received") with a preview of its writes, and goes ahead only after `--resume <run> --approve`.
Stage 7: your house rules (rules/<instance>.md, saved with the run's context) go into the judging and answer prompts,
and into the verdict cache's key, so a change of rules is never answered from the cache.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import time
from pathlib import Path
from typing import Any, Awaitable, Callable

from pydantic import BaseModel, ValidationError

from email_agent.common.skill_registry import SkillRegistry
from email_agent.contracts.agent import ActionResult, RunContext
from email_agent.contracts.capabilities import JudgeThreadsInput, RefuseGoalInput
from email_agent.contracts.flows import (
    CriticVerdict,
    HeldVerdict,
    JoinResult,
    ShardResult,
    ValidatorReport,
    Verdict,
    VerifierScore,
    WritePlan,
)
from email_agent.contracts.graph import APPROVAL, Deferred, FanOut, GraphPatch, TaskSpec
from email_agent.contracts.llm import ChatMessage, LlmReply, LlmRequest, ToolCall
from email_agent.contracts.mcp import tool_entity
from email_agent.contracts.platform import ROW_MODELS, Row, list_page
from email_agent.contracts.runlog import ActionStep, LlmStep, NodeStep
from email_agent.contracts.tool_args import TOOL_ARGS
from email_agent.graph.capabilities import (
    SHARD_TIMEOUT_S,
    WRITES_TIMEOUT_S,
    Capabilities,
)
from email_agent.graph.flows import FLOWS, SHARD_SIZE, VERDICTS, Flow, ScopedMailbox
from email_agent.graph.outbox import RECONCILE
from email_agent.graph.store import BudgetExceeded, RunStore
from email_agent.llm.route import Llm
from email_agent.mailbox.search import select_candidates
from email_agent.mailbox.store import MailboxStore
from email_agent.memory.store import VerdictCache, verdict_key
from email_agent.platform.action import Action
from email_agent.platform.writes import CURRENT_NODE, WritePath
from email_agent.record.runlog import RunLog

PROMPTS = Path(__file__).resolve().parent.parent / "prompts"
JUDGE_SYSTEM = (PROMPTS / "judge.md").read_text()
ANSWER_SYSTEM = (PROMPTS / "answer.md").read_text()
CRITIC_SYSTEM = (PROMPTS / "critic.md").read_text()
VERIFIER_SYSTEM = (PROMPTS / "verifier.md").read_text()
EVIDENCE_CHARS = 12_000          # per node, in the answer prompt
CRITIC_OVERRULE_AFTER = 2        # S17: the same gap reported again means the fact is unavailable — let the goal end
VALIDATE_SAMPLE = 5              # besides every write-causing verdict, this many of the others, to spot misses
ROOM_NODES = 4                   # kept free when a fan-out is sized: join, check, write, answer
ROOM_CALLS = 4                   # … and model calls: the planner round after the write, critic, answer, verifier
# Found by the Stage 6 drill: without it the critic read "dry_run: true" as "not written yet", and the planner re-added
# the same writes as separate tasks.
DRY_RUN_NOTE = ("DRY RUN: writes are recorded, not sent to the platform. A write step whose result says dry_run: true "
                "counts as done; never ask for those writes to be made again or for real.")


def _hash(text: str) -> str:
    return hashlib.sha256(text.encode()).hexdigest()[:16]


def run_facts(ctx: RunContext) -> dict[str, Any]:
    return {"today": ctx.today.isoformat(), "company_country": ctx.locale.country, "currency": ctx.locale.base_currency,
            "currency_symbol": ctx.locale.currency_symbol, "date_format": ctx.locale.date_format,
            "person": {"name": ctx.me.name, "email": ctx.me.email}, "our_mailboxes": [m.email for m in ctx.mailboxes]}


def house_rules(ctx: RunContext) -> str:
    """Your house rules for this book as a prompt section ("" when there are none). They are your instructions, so
    they go with the system instructions, not with the data."""
    if not ctx.house_rules:
        return ""
    return f"\n\n## House rules for this book (from the person who runs this agent)\n{ctx.house_rules}"


def section(body: str, heading: str) -> str:
    """One '## heading' section of a SKILL.md body (the whole body when it has no such heading)."""
    lines, out, inside = body.splitlines(), [], False
    for line in lines:
        if line.startswith("## "):
            inside = line[3:].strip().lower().startswith(heading.lower())
        if inside:
            out.append(line)
    return "\n".join(out) or body


class LimitedLlm:
    """At most `n` model calls at once, whoever asks (planner, shards, answers)."""

    def __init__(self, llm: Llm, n: int):
        self.llm, self.model, self._sem = llm, llm.model, asyncio.Semaphore(max(1, n))

    def sharing(self, other: Llm) -> "LimitedLlm":
        """Another model behind the same limit (the validator's route)."""
        twin = LimitedLlm(other, 1)
        twin._sem = self._sem
        return twin

    async def chat(self, req: LlmRequest) -> LlmReply:
        async with self._sem:
            return await self.llm.chat(req)


class NodeFailed(Exception):
    """A tool call the platform or our guard refused: the node fails and the planner reads why."""


class GraphRuntime:
    def __init__(self, *, ctx: RunContext, store: RunStore, mirror: MailboxStore, action: Action, writes: WritePath,
                 llm: Llm, log: RunLog, skills: SkillRegistry, caps: Capabilities, cache: VerdictCache | None,
                 goals: Callable[[], dict[str, Any]], route: Any = None, validate: bool = True,
                 approve_writes: bool = False):
        self.ctx, self.store, self.mirror, self.action, self.writes = ctx, store, mirror, action, writes
        self.llm, self.log, self.skills, self.caps, self.cache = llm, log, skills, caps, cache
        self.goals = goals                      # goal id → Goal, owned by the planner
        self.round = 0                          # the planner's round, for step numbering
        self.route, self.validate = route, validate   # the RoutedLlm under `llm` (for the validator's other model)
        self.approve_writes = approve_writes    # in a dry run too: it stops before recording what would be written
        self._reused: set[tuple[str, str]] = set()   # saved replies already reused in this process (each once)
        self.vector_candidates = 10             # hybrid search: at most N added by meaning (Settings.search_vector_*)
        self.vector_margin = 0.04               # … within this of the best meaning score

    # ── wiring ───────────────────────────────────────────────────────────────
    def workers(self) -> dict[str, Callable[[TaskSpec], Awaitable[Any]]]:
        graph = {"judge_threads": self.judge_threads, "judge_shard": self.judge_shard,
                 "join_verdicts": self.join_verdicts, "validate_verdicts": self.validate_verdicts,
                 "apply_writes": self.apply_writes,
                 "answer": self.answer, "refuse": self.refuse}
        tools = {n: self.tool for n, c in self.caps.items.items() if c.kind == "tool"}
        return {name: self._logged(fn) for name, fn in {**tools, **graph}.items()}

    def _logged(self, fn: Callable[[TaskSpec], Awaitable[Any]]) -> Callable[[TaskSpec], Awaitable[Any]]:
        async def run(task: TaskSpec) -> Any:
            CURRENT_NODE.set(task.id)
            started = time.monotonic()
            try:
                out = await fn(task)
            except Exception as e:
                self.log.step(NodeStep(iter=self.round, node_id=task.id, capability=task.capability,
                                       goal_id=task.goal_id, state="failed", summary=f"{type(e).__name__}: {e}"[:300],
                                       seconds=time.monotonic() - started))
                raise
            state = "waiting" if isinstance(out, Deferred) else "fanned_out" if isinstance(out, FanOut) else "succeeded"
            self.log.step(NodeStep(iter=self.round, node_id=task.id, capability=task.capability, goal_id=task.goal_id,
                                   state=state, summary=_summary(out), seconds=time.monotonic() - started))
            return out
        return run

    async def chat(self, req: LlmRequest, node_id: str | None, layer: str, llm: Llm | None = None) -> LlmReply:
        """One model call for a node (None = the planner). The same request from the same node, saved before a crash,
        is answered from run.sqlite: no call, no budget, no tokens (`reused` on the reply and the journal line)."""
        request_hash = hashlib.sha256(req.model_dump_json().encode()).hexdigest()
        mark = (node_id or "planner", request_hash)
        saved = self.store.saved_reply(*mark) if mark not in self._reused else None
        if saved is not None:
            self._reused.add(mark)
            reply = LlmReply.model_validate_json(saved).model_copy(update={"reused": True})
            self.store.record_event("llm_call_finished", node_id, {"purpose": req.purpose, "model": reply.model,
                                                                   "provider": reply.provider, "reused": True})
            return reply
        self.store.spend("llm_calls", 1, node_id=node_id)
        self.store.record_event("llm_call_started", node_id, {"purpose": req.purpose})
        reply = await (llm or self.llm).chat(req)
        self.store.finish_llm_call(node_id, request_hash, reply.model_dump_json(), {
            "purpose": req.purpose, "model": reply.model, "provider": reply.provider,
            "input_tokens": reply.usage.input_tokens, "output_tokens": reply.usage.output_tokens})
        self._reused.add(mark)                  # a second identical request in this process really calls the model
        return reply

    def _approval(self, task: TaskSpec, preview: Any) -> Deferred | None:
        """The approval gate: None = go ahead (gate off, or you approved this node); else park the node until you
        answer (`--resume <run> --approve` or `--reject`)."""
        if not self.approve_writes:
            return None
        handle = f"approval:{task.id}"
        if self.store.received(handle, APPROVAL):
            return None
        return Deferred(handle=handle, event_type=APPROVAL, metadata={"goal_id": task.goal_id, "writes": preview})

    async def fetch(self, tool: str, args: dict[str, Any]) -> Any:
        """Platform reads for the flows: a list tool → typed rows; `Deal.get` → a DealView dict (or None)."""
        if tool == "Deal.get":
            deal = await self.action._deal(args["id"])
            return deal.model_dump(mode="json", exclude_none=True) if deal else None
        out = await self.action.mcp.call(tool, TOOL_ARGS[tool].model_validate(args))
        if not out.ok:
            raise NodeFailed(f"{tool} failed: {out.error.message if out.error else out.text}")
        return list_page(ROW_MODELS.get(tool_entity(tool), Row)).model_validate(out.data()).data

    # ── tool capabilities ────────────────────────────────────────────────────
    async def tool(self, task: TaskSpec) -> dict[str, Any] | Deferred:
        cap = self.caps.get(task.capability)
        if cap is not None and cap.writes:
            wait = self._approval(task, [{"tool": task.capability, "arguments": task.input}])
            if wait is not None:
                return wait
        result = await self.action.execute(ToolCall(name=task.capability, arguments=task.input), [task.capability])
        self.log.step(ActionStep(iter=self.round, goal_id=task.goal_id or "", result=result))
        if result.uncertain:
            return Deferred(handle=f"reconcile:{task.id}", event_type=RECONCILE, metadata={"keys": result.uncertain})
        if not result.ok:
            raise NodeFailed(f"{result.kind}: {result.message}")
        out: dict[str, Any] = {"tool": task.capability, "result": result.preview}
        if result.writes or result.write:
            out["writes"] = len(result.writes) or 1
            out["dry_run"] = self.action.dry_run
        return out

    # ── judging: fan-out, shards, join, writes ───────────────────────────────
    def _flow(self, skill: str, only: set[str] | None = None) -> Flow:
        """The skill's flow over the mailbox copy, seen through the run's scope (a watcher run) and, for a hybrid
        search, through its candidates."""
        scope = set(self.ctx.only_threads) if self.ctx.only_threads is not None else None
        if only is not None:
            scope = only if scope is None else scope & only
        return FLOWS[skill](self.mirror if scope is None else ScopedMailbox(self.mirror, sorted(scope)), self.ctx)

    async def _hybrid(self, args: JudgeThreadsInput) -> tuple[set[str], dict[str, Any]] | None:
        """Hybrid search (Stage 9): the full-text matches (every word, as before) plus the conversations closest in
        meaning (search.select_candidates: within a margin of the best score, at most N). The shards judge every
        candidate."""
        search = self.action.mail_search
        if search is None or not args.search:
            return None
        try:
            ranked = await search.rank(args.search, [m.id for m in self.ctx.mailboxes], bonus=0.0,
                                       only=set(self.ctx.only_threads) if self.ctx.only_threads is not None else None)
        except Exception as e:  # noqa: BLE001 — fall back to full text for this task
            self.store.record_event("search_fallback", None, {"reason": f"hybrid search failed, full text only: "
                                                                        f"{type(e).__name__}: {e}"[:300]})
            return None
        by_text, by_meaning = select_candidates(ranked, self.vector_margin, self.vector_candidates)
        return by_text | by_meaning, {"search_mode": "hybrid", "full_text_matches": len(by_text),
                                           "added_by_meaning": len(by_meaning)}

    async def judge_threads(self, task: TaskSpec) -> dict[str, Any] | FanOut:
        args = JudgeThreadsInput.model_validate(task.input)
        await self.action._ensure_synced()
        hybrid = await self._hybrid(args)
        if hybrid is None:
            ids = self._flow(args.skill).select(args)    # a watcher run's flow sees only its conversation(s)
        else:                                            # the candidates replace the word filter
            ids = self._flow(args.skill, hybrid[0]).select(args.model_copy(update={"search": None}))
        total = self.mirror.conversation_count([m.id for m in self.ctx.mailboxes])
        if self.ctx.only_threads is not None:        # say so, or the answer counts the whole mailbox as checked
            total = len(self.ctx.only_threads)
        if not ids:          # a finished outcome with its write result (0), so the critic can see nothing was due
            return {"skill": args.skill, "candidates": 0, "conversations_in_mailboxes": total, "search": args.search,
                    **({"limited_to": "the conversation(s) of the event that started this run"}
                       if self.ctx.only_threads is not None else {}),
                    "written": 0, "writes_planned": 0, "dry_run": self.writes.dry_run,
                    "outcome": "no conversation matches this skill's selection, so nothing was judged and nothing "
                               "needed writing: this is the goal's final write result",
                    **(hybrid[1] if hybrid else {})}
        chunks = [ids[i:i + SHARD_SIZE] for i in range(0, len(ids), SHARD_SIZE)]
        fit = self._shards_that_fit()
        if fit < 1:
            raise NodeFailed(f"{len(ids)} conversation(s) to judge, but this run's budget has no room left for any "
                             "judging call: answer with what is known, or ask the person to narrow the request")
        not_judged = sum(len(c) for c in chunks[fit:])
        chunks = chunks[:fit]                         # the newest first: selection lists the newest real message first
        shards = [TaskSpec(id=f"{task.id}.s{n:03d}", capability="judge_shard", goal_id=task.goal_id,
                           input={"skill": args.skill, "thread_ids": chunk}, wakes_planner=False,
                           timeout_s=SHARD_TIMEOUT_S) for n, chunk in enumerate(chunks, start=1)]
        join = TaskSpec(id=f"{task.id}.join", capability="join_verdicts", goal_id=task.goal_id, wakes_planner=False,
                        input={"args": args.model_dump(mode="json"), "shards": [s.id for s in shards]})
        check = TaskSpec(id=f"{task.id}.check", capability="validate_verdicts", goal_id=task.goal_id,
                         wakes_planner=False, timeout_s=SHARD_TIMEOUT_S,
                         input={"join": join.id, "shards": [s.id for s in shards]})
        write = TaskSpec(id=f"{task.id}.write", capability="apply_writes", goal_id=task.goal_id,
                         input={"join": join.id, "check": check.id}, timeout_s=WRITES_TIMEOUT_S)
        left_out = {"not_judged": not_judged,
                    "note": f"{not_judged} older conversation(s) were not judged: this run's budget fits {fit} group(s) "
                            f"of {SHARD_SIZE}. Say so in the answer; the person can ask again with a narrower search "
                            "(a party, a subject word) for the rest."} if not_judged else {}
        return FanOut(result={"skill": args.skill, "candidates": len(ids), "shards": len(shards), **left_out,
                              **(hybrid[1] if hybrid else {}),
                              "conversations_in_mailboxes": total,
                              **({"limited_to": "the conversation(s) of the event that started this run"}
                                 if self.ctx.only_threads is not None else {})},
                      patch=GraphPatch(add=[*shards, join, check, write],
                                       connect=[(s.id, join.id) for s in shards] + [(join.id, check.id),
                                                                                    (check.id, write.id)],
                                       reason=f"{args.skill}: {len(ids)} candidates in {len(shards)} shard(s)",
                                       metadata={"final": write.id}))

    def _room(self, name: str) -> float:
        try:
            b = self.store.budget(name)
        except KeyError:
            return float("inf")
        return b.limit - b.spent

    def _shards_that_fit(self) -> int:
        """How many judging shards this run can still pay for (Revision 17: a 10,000-conversation sort fanned out 500
        shards, used up the budgets at shard ~78 and ended with every goal open). With the second model on, each shard's
        verdicts may need one more call to be checked."""
        per_shard = 2 if self.validate and self.route is not None else 1
        room = min(self._room("nodes") - ROOM_NODES, (self._room("llm_calls") - ROOM_CALLS) // per_shard)
        return int(min(room, 10**6))

    async def _judge(self, flow: Flow, ids: list[str], goal_id: str | None, node_id: str, llm: Llm,
                     layer: str) -> tuple[list[Verdict], str | None]:
        """One schema-bound judging call for these conversations (ids checked, one repair). Shards and the validator
        both use it, so the two models see exactly the same prompt."""
        spec = self.skills.get(flow.skill)
        digests = await flow.digests(ids, self.fetch)
        goal = self.goals().get(goal_id or "")
        text = "\n\n".join([
            f"RUN FACTS:\n{json.dumps(run_facts(self.ctx), indent=1)}",
            f"GOAL: {goal.text if goal else ''}",
            f"SKILL '{flow.skill}' — INSTRUCTIONS:\n{spec.instructions}",
            f"CONVERSATIONS ({len(digests)}):\n{json.dumps(digests, ensure_ascii=False, indent=1)}",
            f"Return exactly one verdict for each of these thread ids: {json.dumps(ids)}"])
        messages = [ChatMessage(role="user", text=text)]
        for attempt in range(2):
            req = LlmRequest(purpose="validator" if layer == "validator" else "judge",
                             system=JUDGE_SYSTEM + house_rules(self.ctx), messages=messages, response_schema=flow.shard.model_json_schema(), temperature=0.0)
            reply = await self.chat(req, node_id, layer, llm)
            try:
                out = flow.shard.model_validate_json(reply.text)
                got = [v.thread_id for v in out.verdicts]
                missing, extra = set(ids) - set(got), set(got) - set(ids)
                if missing or extra or len(got) != len(set(got)):
                    raise ValueError(f"verdict ids must be exactly the given ones: missing {sorted(missing)}, "
                                     f"unknown {sorted(extra)}, repeated {len(got) - len(set(got))}")
            except (ValidationError, ValueError) as e:
                self.log.step(LlmStep(iter=self.round, layer=layer, node_id=node_id, request=req, reply=reply,
                                      valid=False, error=str(e)[:2000]))
                if attempt:
                    raise
                messages = messages + [ChatMessage(role="model", text=reply.text or "(empty)"),
                                       ChatMessage(role="user", text=f"That reply was rejected: {e}. Return the "
                                                                     "corrected JSON.")]
                continue
            self.log.step(LlmStep(iter=self.round, layer=layer, node_id=node_id, request=req, reply=reply, valid=True))
            return list(out.verdicts), reply.model
        raise RuntimeError("unreachable")

    async def judge_shard(self, task: TaskSpec) -> ShardResult:
        skill, ids = task.input["skill"], list(task.input["thread_ids"])
        flow = self._flow(skill)
        spec = self.skills.get(skill)
        hashes = self.mirror.content_hashes(ids)
        day = self.ctx.today.isoformat() if flow.date_bound else None
        prompt = _hash(JUDGE_SYSTEM + house_rules(self.ctx))
        keys = {t: verdict_key(t, hashes.get(t, ""), skill, _hash(spec.instructions), prompt, day) for t in ids}
        cached = self.cache.get_many(keys) if self.cache else {}
        if cached:
            self.store.record_event("cache_hit", task.id, {"verdicts": len(cached)})
        todo = [t for t in ids if t not in cached]
        judged: list[dict[str, Any]] = []
        model = next((m for _, m in cached.values() if m), None)
        if todo:
            verdicts, model = await self._judge(flow, todo, task.goal_id, task.id, self.llm, "judge")
            judged = [v.model_dump(mode="json") for v in verdicts]
            if self.cache:
                self.cache.put_many([(keys[v["thread_id"]], v["thread_id"], skill, v) for v in judged], model)
        return ShardResult(skill=skill, verdicts=[cached[t][0] for t in ids if t in cached] + judged,
                           cached=len(cached), judged=len(judged), model=model)

    async def join_verdicts(self, task: TaskSpec) -> JoinResult:
        args = JudgeThreadsInput.model_validate(task.input["args"])
        flow, model = self._flow(args.skill), VERDICTS[args.skill]
        verdicts, problems, seen = [], [], set()
        for sid in task.input["shards"]:
            node = self.store.node(sid)
            allowed = set(node.input["thread_ids"])
            for raw in (node.result or {}).get("verdicts", []):
                try:
                    v = model.model_validate(raw)
                except ValidationError as e:
                    problems.append(f"a verdict failed its contract: {str(e)[:160]}")
                    continue
                if v.thread_id not in allowed or v.thread_id in seen:
                    problems.append(f"{v.thread_id}: not a candidate of its shard, or judged twice")
                    continue
                seen.add(v.thread_id)
                verdicts.append(v)
        plan = await flow.plan(verdicts, args, self.fetch)
        seen_writes: set[tuple[str, str]] = set()
        unique = []
        for w in plan.writes:                        # code check: never the same write twice in one plan
            if (w.tool, w.thread_id) in seen_writes:
                plan.problems.append(f"{w.thread_id}: {w.tool} planned twice; kept once")
                continue
            seen_writes.add((w.tool, w.thread_id))
            unique.append(w)
        plan.writes = unique
        about = {o.thread_id: {"subject": o.subject, "counterpart": o.counterpart}
                 for _, o in self.mirror.overviews([m.id for m in self.ctx.mailboxes], ids=seen)}
        return JoinResult(skill=args.skill, candidates=len(seen), counts=flow.counts(verdicts),
                          writes_planned=len(plan.writes), problems=problems + plan.problems,
                          verdicts=[about.get(v.thread_id, {}) | v.model_dump(mode="json", exclude_none=True)
                                    for v in verdicts], plan=plan)

    async def validate_verdicts(self, task: TaskSpec) -> ValidatorReport:
        """Another model re-judges every verdict that would cause a write, and a few of the others. Only the writes
        both models agree on go ahead; the disputed ones are held and reported (S17 `validate_work`: fresh context,
        a different model, read-only, no power to fix what it checks)."""
        join = JoinResult.model_validate(self.store.node(task.input["join"]).result)
        flow, model = self._flow(join.skill), VERDICTS[join.skill]
        shards = [ShardResult.model_validate(self.store.node(s).result) for s in task.input["shards"]]
        judge_model = next((s.model for s in shards if s.model), None)
        report = ValidatorReport(skill=join.skill, judge_model=judge_model, plan=join.plan)
        if not self.validate:
            return report.model_copy(update={"skipped": "turned off (VALIDATE_VERDICTS=false)"})
        if not flow.checkable:
            return report.model_copy(update={"skipped": "free text: checked by code only"})
        other = self.route.without_model(judge_model) if self.route is not None and judge_model else None
        if other is None:
            return report.model_copy(update={"skipped": "no second model configured"})
        llm = self.llm.sharing(other) if isinstance(self.llm, LimitedLlm) else other
        verdicts = {v["thread_id"]: model.model_validate({k: v[k] for k in v if k not in ("subject", "counterpart")})
                    for v in join.verdicts}
        positives = sorted(t for t, v in verdicts.items() if flow.positive(v))      # every one: none goes out unchecked
        others = sorted((t for t in verdicts if t not in positives),
                        key=lambda t: _hash(self.ctx.run_id + t))[:VALIDATE_SAMPLE]
        ids = positives + others
        if not ids:
            return report.model_copy(update={"skipped": "nothing to check"})
        second: dict[str, Verdict] = {}
        validator_model = None
        unchecked: list[str] = []
        for i in range(0, len(ids), 20):
            try:
                got, validator_model = await self._judge(flow, ids[i:i + 20], task.goal_id, task.id, llm, "validator")
            except BudgetExceeded:
                unchecked = [t for t in ids[i:] if t in positives]   # held, never written without the second look
                break
            second.update({v.thread_id: v for v in got})
        subjects = {v["thread_id"]: v.get("subject") for v in join.verdicts}
        held: list[HeldVerdict] = []
        misses: list[HeldVerdict] = []
        agreed = 0
        for t in ids:
            if t not in second:
                continue
            a, b = verdicts[t], second[t]
            if flow.agree(a, b):
                agreed += 1
                continue
            item = HeldVerdict(thread_id=t, subject=subjects.get(t), judge=a.model_dump(mode="json", exclude_none=True),
                               validator=b.model_dump(mode="json", exclude_none=True))
            (held if t in positives else misses).append(item)
        held += [HeldVerdict(thread_id=t, subject=subjects.get(t), judge=verdicts[t].model_dump(mode="json", exclude_none=True),
                             validator={"not_checked": "the model-call budget ran out before the second model saw it"})
                 for t in unchecked]
        held_ids = {h.thread_id for h in held}
        plan = join.plan.model_copy(update={"writes": [w for w in join.plan.writes if w.thread_id not in held_ids]})
        self.store.record_event("validator_checked", task.id, {
            "skill": join.skill, "judge_model": judge_model, "validator_model": validator_model, "checked": len(ids),
            "agreed": agreed, "held": sorted(held_ids), "possible_misses": [m.thread_id for m in misses]})
        return ValidatorReport(skill=join.skill, judge_model=judge_model, validator_model=validator_model,
                               checked=len(ids), agreed=agreed, held=held, possible_misses=misses, plan=plan)

    async def apply_writes(self, task: TaskSpec) -> dict[str, Any] | Deferred:
        join = JoinResult.model_validate(self.store.node(task.input["join"]).result)
        plan: WritePlan = join.plan
        if task.input.get("check"):                  # the validator's plan: held conversations taken out
            plan = ValidatorReport.model_validate(self.store.node(task.input["check"]).result).plan
        if plan.writes:
            wait = self._approval(task, [{"tool": w.tool, "thread_id": w.thread_id, "why": w.why,
                                          "fields": {k: v for k, v in w.fields.items()
                                                     if k not in ("id", "content", "company_id")}}
                                         for w in plan.writes])
            if wait is not None:
                return wait
        outcomes = await self.writes.write_many([(w.tool, w.fields) for w in plan.writes]) if plan.writes else []
        records = [o.record for o in outcomes if o.record]
        self.log.step(ActionStep(iter=self.round, goal_id=task.goal_id or "", result=ActionResult(
            tool="apply_writes", arguments={"join": task.input["join"]}, kind="ok", ok=True, writes=records,
            preview=json.dumps({"done": sum(o.ok for o in outcomes), "failed": sum(not o.ok for o in outcomes),
                                "already_right": len(plan.unchanged)}))))
        uncertain = [o.uncertain for o in outcomes if o.uncertain]
        if uncertain:
            return Deferred(handle=f"reconcile:{task.id}", event_type=RECONCILE, metadata={"keys": uncertain})
        failed = [{"thread_id": w.thread_id, "tool": w.tool, "error": o.error}
                  for w, o in zip(plan.writes, outcomes) if not o.ok]
        return {"skill": join.skill, "written": sum(o.ok for o in outcomes), "failed": failed,
                "already_right": len(plan.unchanged), "dry_run": self.writes.dry_run,
                "writes": [{"thread_id": w.thread_id, "tool": w.tool,
                            "fields": {k: v for k, v in w.fields.items() if k not in ("id", "content", "company_id")},
                            "why": w.why} for w, o in zip(plan.writes, outcomes) if o.ok]}

    # ── ending a goal ────────────────────────────────────────────────────────
    def _evidence(self, goal_id: str, exclude: str) -> list[dict[str, Any]]:
        evidence = []
        for n in self.store.snapshot().nodes.values():
            if n.goal_id != goal_id or n.id == exclude or n.capability in ("judge_shard", "answer"):
                continue
            item: dict[str, Any] = {"task": n.id, "capability": n.capability, "state": n.state, "input": n.input}
            if n.result is not None:
                item["result"] = json.dumps(n.result, ensure_ascii=False, default=str)[:EVIDENCE_CHARS]
            if n.error is not None:
                item["error"] = n.error.message[:500]
            evidence.append(item)
        return evidence

    async def _critic(self, task: TaskSpec, ending: str, evidence: list[dict[str, Any]]) -> None:
        """S17's evidence-readiness critic, run as the goal is about to end. Not ready → the node fails with what is
        missing, so the planner adds work. After CRITIC_OVERRULE_AFTER rejections of the same goal the gap counts as
        unavailable and the goal may end (S17's rule against endless searching)."""
        goal = self.goals()[task.goal_id or ""]
        spec = self.skills.get(goal.skill) if goal.skill else None
        earlier = [e for e in self.store.events() if e.kind == "critic_reviewed"
                   and e.payload.get("goal_id") == goal.id and not e.payload.get("ready")]
        text = "\n\n".join([
            f"GOAL ({goal.id}): {goal.text}",
            f"ABOUT TO END AS: {ending}",
            *([DRY_RUN_NOTE] if self.writes.dry_run else []),
            "SKILL RULES:\n" + (spec.instructions if spec else "(none)"),
            f"EARLIER GAPS REPORTED FOR THIS GOAL: {json.dumps([e.payload.get('missing') for e in earlier])}",
            f"EVIDENCE:\n{json.dumps(evidence, ensure_ascii=False, indent=1)}"])
        req = LlmRequest(purpose="critic", system=CRITIC_SYSTEM, messages=[ChatMessage(role="user", text=text)],
                         response_schema=CriticVerdict.model_json_schema(), temperature=0.0)
        reply = await self.chat(req, task.id, "critic")
        try:
            verdict = CriticVerdict.model_validate_json(reply.text)
        except ValidationError as e:
            self.log.step(LlmStep(iter=self.round, layer="critic", node_id=task.id, request=req, reply=reply,
                                  valid=False, error=str(e)[:1000]))
            return                                   # a broken critic reply never blocks the goal
        overruled = not verdict.ready and len(earlier) >= CRITIC_OVERRULE_AFTER
        self.log.step(LlmStep(iter=self.round, layer="critic", node_id=task.id, request=req, reply=reply, valid=True,
                              error=None if verdict.ready else f"not ready: {verdict.missing}"
                                                               + (" (overruled: same gap again)" if overruled else "")))
        self.store.record_event("critic_reviewed", task.id, {"goal_id": goal.id, "ending": ending, "ready": verdict.ready,
                                                             "missing": verdict.missing, "reason": verdict.reason,
                                                             "overruled": overruled})
        if not verdict.ready and not overruled:
            raise NodeFailed(f"the evidence critic says goal {goal.id} is not ready to end as {ending}: missing "
                             f"{verdict.missing}. {verdict.reason}")

    async def answer(self, task: TaskSpec) -> dict[str, Any]:
        goal = self.goals()[task.goal_id or ""]
        spec = self.skills.get(goal.skill) if goal.skill else None
        open_work = [n.id for n in self.store.snapshot().nodes.values() if n.goal_id == goal.id and n.id != task.id
                     and n.state in ("pending", "running", "waiting")]
        if open_work:
            raise NodeFailed(f"goal {goal.id} still has open work ({', '.join(open_work)}): answer it after that")
        evidence = self._evidence(goal.id, task.id)
        await self._critic(task, "an answer", evidence)
        text = "\n\n".join([
            f"RUN FACTS:\n{json.dumps(run_facts(self.ctx), indent=1)}",
            f"REQUEST:\n{self.ctx.request}",
            f"GOAL ({goal.id}): {goal.text}",
            "WHAT TO REPORT:\n" + (section(spec.instructions, "What to report") if spec else "(no skill)"),
            f"EVIDENCE:\n{json.dumps(evidence, ensure_ascii=False, indent=1)}"])
        req = LlmRequest(purpose="answer", system=ANSWER_SYSTEM + house_rules(self.ctx),
                         messages=[ChatMessage(role="user", text=text)], temperature=0.2)
        reply = await self.chat(req, task.id, "answer")
        ok = bool(reply.text.strip())
        self.log.step(LlmStep(iter=self.round, layer="answer", node_id=task.id, request=req, reply=reply, valid=ok,
                              error=None if ok else "empty answer"))
        if not ok:
            raise NodeFailed("the model returned an empty answer")
        score = await self._verify(task, goal.text, evidence, reply.text.strip())
        return {"goal_id": goal.id, "text": reply.text.strip(), **({"verifier": score.model_dump()} if score else {})}

    async def _verify(self, task: TaskSpec, goal_text: str, evidence: list[dict[str, Any]],
                      answer: str) -> VerifierScore | None:
        """S17's verifier: a score and a critique for the answer, kept as evidence (it never changes the answer)."""
        text = "\n\n".join([f"GOAL: {goal_text}", f"EVIDENCE:\n{json.dumps(evidence, ensure_ascii=False)[:30000]}",
                             f"ANSWER:\n{answer}"])
        req = LlmRequest(purpose="critic", system=VERIFIER_SYSTEM, messages=[ChatMessage(role="user", text=text)],
                         response_schema=VerifierScore.model_json_schema(), temperature=0.0)
        try:
            reply = await self.chat(req, task.id, "critic")
            score = VerifierScore.model_validate_json(reply.text)
        except Exception as e:  # noqa: BLE001 — evidence only: a failed grade never fails the answer
            self.store.record_event("answer_scored", task.id, {"error": f"{type(e).__name__}: {e}"[:300]})
            return None
        self.log.step(LlmStep(iter=self.round, layer="critic", node_id=task.id, request=req, reply=reply, valid=True,
                              error=f"verifier score {score.score}"))
        self.store.record_event("answer_scored", task.id, {"score": score.score, "issues": score.issues})
        return score

    async def refuse(self, task: TaskSpec) -> dict[str, Any]:
        r = RefuseGoalInput.model_validate(task.input)
        if r.reason in ("no_evidence", "unknown_record"):     # a refusal that rests on evidence goes past the critic
            await self._critic(task, f"a refusal ({r.reason}): {r.explanation}", self._evidence(task.goal_id or "", task.id))
        return {"goal_id": task.goal_id, "reason": r.reason, "explanation": r.explanation}


def _summary(out: Any) -> str:
    if isinstance(out, FanOut):
        return f"fan-out: {out.patch.reason}"
    if isinstance(out, Deferred):
        if out.event_type == APPROVAL:
            return f"waiting for your approval of {len(out.metadata.get('writes') or [])} write(s)"
        return f"waiting for {out.event_type}"
    data = out.model_dump(mode="json") if isinstance(out, BaseModel) else out
    if not isinstance(data, dict):
        return ""
    if "text" in data:
        return f"answer, {len(data['text'])} characters"
    if "reason" in data and "explanation" in data:
        return f"refused ({data['reason']})"
    keys = ("candidates", "counts", "written", "already_right", "failed", "cached", "judged", "writes_planned", "problems",
            "checked", "agreed", "held", "possible_misses", "skipped")
    parts = []
    for k in keys:
        if k in data:
            v = data[k]
            parts.append(f"{k}={len(v) if isinstance(v, list) else v}")
    return ", ".join(parts) or (str(data.get("result", ""))[:120])
