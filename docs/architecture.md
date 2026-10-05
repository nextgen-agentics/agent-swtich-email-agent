# Architecture — how the pieces fit

## One picture

```
                        your laptop                                     AgentSwitch (FastAPI)
 ┌──────────────────────────────────────────────────────────┐        ┌──────────────────────────┐
 │  harness/  (evaluation)                                   │        │                          │
 │   tasks.yaml → run.py ──calls──► email_agent.agent.run()  │        │                          │
 │                 │ saves runs to disk                       │        │                          │
 │   score.py ◄────┘ reads disk + DB ──────────────MCP──────────────► │  POST /api/mcp  (tools)  │
 │                                                           │        │                          │
 │  email_agent/  (the agent + building blocks)              │        │                          │
 │   agent.py  live graph: planner → parallel nodes ─────MCP────────►   │  POST /api/mcp           │
 │   llm.py    Gemini / W&B (OpenAI-compatible)  ──► model APIs        │                          │
 │   mcp_session.py  official `mcp` SDK client ──────────────────►    │  POST /api/mcp           │
 │   rest.py   httpx2: login, me, locale, schemas ───REST──────────►  │  /api/auth/*, /api/…     │
 │   contracts/  the agent's pydantic models                 │        │                          │
 │               (harness/ keeps its own)                    │        │                          │
 └──────────────────────────────────────────────────────────┘        └──────────────────────────┘
        you ──────────────────────── browser ──────────────────────────► Web UI (same data)
```

- **MCP** carries everything the agent does to business data (threads, messages, reminders, todos, memory).
- **REST** carries only what MCP does not offer: login, who-am-I, the locale, schemas, bug reports.
- **Web UI** is for you, never for code.

## The agent: a live graph (Revision 12, adapted from S17)

```
context (no LLM: me, locale, today, our mailboxes)
  └─ RunStore runs/<run_id>/run.sqlite  (the graph, a numbered journal, budgets, the write outbox)
  └─ GraphExecutor: ready nodes run in parallel; each outcome wakes the planner (unless code can react alone)
       planner (LLM) ─ round 1: goals, each with ONE skill (or a refusal reason) ─ then: the next tasks (≤ 4)
         ├─ judge_threads(skill)  bulk work: select candidates (SQL on the local copy) ─► shards (LLM, parallel,
         │                        ≤ 20 conversations each, cached) ─► join (code checks, write plan) ─► write step
         ├─ tool capabilities     one MCP / local tool call through Action (named records, the memory skill)
         ├─ answer                one LLM call per goal, from what the graph found
         └─ refuse                ends a goal with a reason (no LLM)
       every write: WritePath (lock · guard · dry run · outbox · MCP or REST · writes.jsonl · local copy)
  └─ FinalAnswer ─► runs/<run_id>/ (request, context, steps.jsonl, writes.jsonl, final, outcome, report.md)
     every exit writes final, outcome and report.md — a crash (stopped: crashed) and Ctrl-C (interrupted) too
```

- **The planner** (`graph/planner.py`) replaces Perception and Decision. Its first call sets the goals (the old
  Perception rules); each later call adds only the next tasks. Code checks every proposal — offered for that goal's
  skill, valid arguments, no repeated work, known dependencies — and sends a rejected one back with the reason (≤ 3
  times). The run finishes by code once every goal has a succeeded `answer` or `refuse`.
- **Fan-out for scale**: whole-mailbox work is one `judge_threads` task that code splits into parallel shards, so the
  number of planner calls depends on the request, not on the size of the mailbox. The LLM only judges; selecting,
  paging, copying figures and writing are code (`flows.py`).
- **Skills** (`email_agent/skills/<name>/SKILL.md`) name the capabilities a goal may use and say how to decide; the
  planner gets the instructions of the goals' skills, the shards get their skill's rules. At start-up every tool a
  skill names is checked against the live tool list.
- **The local mailbox copy** (`mailbox_store.py`, `sync.py`): reads come from `state/<instance>/mailbox.sqlite`,
  synced incrementally once per run (≈ 1 call per table when nothing changed); facts per conversation are worked out
  when it changes, so candidate selection is SQL.
- **Contracts at every hand-off**: the planner's replies must pass `GoalsOutput` / `PlannerOutput`, the shards'
  their verdict models (ids checked), tool arguments the generated models; every node outcome, journal event, write
  and final answer is a model written to disk.
- **Parallel, but bounded**: up to 6 nodes at once, 3 model calls, 2 MCP calls (the server answers our calls one at a
  time, about 1 s each — Stage 0), each node with a time limit, each run with saved budgets.
- **Trace and resume**: every change is a journal event in `run.sqlite`; a write is recorded in the outbox before it
  is sent, so a resumed run never sends it twice (resume from the command line comes in Stage 6). `report.md` shows
  the graph as a checklist.
- **The LLM route** (`llm.py`, Revision 11): one ordered list of options — the primary provider's, then the
  other's (Gemini keys 1–5 as failover; W&B DeepSeek → GLM → Qwen). Each error class has its own reaction (rest
  for the server's delay, rest until the daily quota resets, drop a refused key or unknown model, move one call on
  when a reply runs out of tokens), health is shared by the process, and every reply records who answered, so a
  verdict can be tied to the model that produced it.
- **Refusals are data**: when no skill fits, the goals step names why (`out_of_seat`, `not_our_mailbox`,
  `not_permitted`) and code refuses the goal; otherwise the planner adds `refuse(reason, explanation)` (also
  `no_evidence`, `unknown_record`). The goal is marked `refused` with that reason in `final.json`, so the harness can check a
  refusal without reading the answer text.
- **Mailbox scope**: every mailbox our login has (a task can narrow it); rows from other mailboxes are left out of
  every result before the model sees them.
- **Safety guard** (in `writes.py`): reads anything in scope; creates allowed; updates only on rows in our
  mailboxes or created by our login; deletes only rows this run created. `--dry-run` records writes without
  sending them.

## Memory, artifacts and history: where they live, and what the model sees

The eight memory layers and their status are in [orchestrator.md](orchestrator.md#5-memory-target-stage-7). Today:

| What | Where | How long |
|---|---|---|
| The run's graph and journal (also the running-task todo list) | `runs/<id>/run.sqlite`; `report.md` shows it as a checklist | the run folder |
| Local mailbox copy | `state/<instance>/mailbox.sqlite` | kept; synced each run |
| Verdict cache | `state/<instance>/memory.sqlite` | until the conversation changes (off for the harness) |
| Artifacts (a tool result over 40,000 characters) | `runs/<id>/artifacts/`; the model gets a 2,000-character preview + the id | the run folder |
| Platform `AgentMemory` (long-term memory) | AgentSwitch | written by the price and memory skills |

**There is no growing chat.** Every model call is one fresh message:

- **The planner**, once per round: the request, run facts, goals with their status, the capability manifest, the graph
  (each node's result clipped to 1,800 characters; shards left out — their join carries the outcome), the event, and
  the instructions of the open goals' skills.
- **A shard**: run facts, the goal, its skill's instructions, and up to 20 compact conversations.
- **An answer**: run facts, the goal, the skill's "What to report", and that goal's evidence from the graph.

## Agent vs harness

The word *harness* has two meanings, and the brief uses both:

- **Agent harness** (industry usage — e.g. "Claude Code is a harness"): the code *around the model* — the loop
  that sends the prompt, runs the tool the model picked, feeds the result back, manages context, stops. In our
  repo: `email_agent/agent.py`, `llm.py`, `action.py`, `runlog.py`.
- **Evaluation / test harness**: the code that *runs the agent on tasks and grades it* — task set, verifiers,
  results on disk, score. In our repo: `harness/`.

## Rules that shape the code

- **Pydantic at every boundary, owned by whoever uses it** — the agent's models in `email_agent/contracts/`, the
  harness's in `harness/contracts.py`. Imports go one way only
  (harness → agent), checked by `uv run lint-imports`; details in
  [code-guide.md](code-guide.md#where-the-contracts-live). Tool arguments are generated from the platform's own tool
  schemas (`contracts/tool_args.py`), so a bad argument is caught before the platform sees it.
- **Standard libraries first** — `mcp` (official SDK) for MCP, `httpx2` for HTTP, `tenacity` for retries,
  `pydantic-settings` for config, `datamodel-code-generator` for generated models.
- **Re-read before write; append, never overwrite** — team 11 edits the same rows.
- **Workarounds are temporary** — tagged `WORKAROUND(BUG-00N)` and listed in
  [known-issues.md](known-issues.md).
- **Updates send only the fields you set** — `model_dump(exclude_unset=True)`; the update schemas carry
  defaults that would otherwise reset fields (BUG-006).
