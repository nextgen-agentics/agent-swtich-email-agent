# Team 10: Email seat agent and harness (AgentSwitch capstone)

An agent for the **Email seat** of AgentSwitch, and a harness that proves what it does by reading the database.

The seat's request: **"What needs my reply today, and find the mail where they agreed the price."** The agent
answers it against live, shared data on both company books:
- **Suryodaya**: India, INR, GST;
- **Keystone**: US, USD, sales tax.

It does a few more inbox jobs from our [gap report](docs/gap-report.md). It refuses what it must not do,
and says why.

## What the agent does

| Skill | Ask it | What it changes |
|---|---|---|
| `triage-replies` | "What needs my reply today?" | Flags each conversation that needs a reply, due today |
| `find-price-agreement` | "Find the mail where they agreed the price." | Saves each agreed price as an agent-memory fact, and stars the conversation |
| `summarize-threads` | "Summarise each conversation in my mailbox." | Fills each conversation's summary |
| `follow-up-reminders` | "Remind me to follow up wherever I am waiting on a reply." | Creates "remind me if no reply" reminders |
| `remember-about-customer` | "Remember that Cardinal wants every quote in USD." | Saves an agent-memory row linked to that party |
| `sort-inbox` | "Sort my inbox." | Sets importance and the category tab on each conversation |

**It refuses**, changing nothing, when a request:
- needs another app's data (salaries);
- is about a mailbox that is not ours;
- asks for something it must not do (send mail, delete shared mail);
- has no support in the mail (a price nobody agreed, an RFQ that does not exist).

The refusal and its reason are saved as data, so a check can verify them.

**How it works.** It is a loop of four layers (the course's S7 design):
- **Perception:** goals, one skill each.
- **Decision:** one tool call or one answer, using only that skill's tools.
- **Action:** checks, a safety guard, the platform call, and a record of the write.
- **Memory:** what has happened so far.

Each skill is one `SKILL.md` file. Every hand-off between layers is checked by a pydantic model. Writes are
allowed only on rows in our own mailboxes, and `--dry-run` records writes without sending them. Every run is
saved to `runs/<run_id>/`, including a readable `report.md`, even when it crashes.

## The harness
- [`harness/tasks.yaml`](harness/tasks.yaml) has 28 tasks on both instances: the seat request, each skill, and
  14 refusal tasks.
- **`harness.run`** runs the agent on each task. It saves the run and the database state before scoring
  anything.
- **`harness.score`** then reads the **database** and compares it with an answer key you decided by hand
  (`harness/ground_truth/`). The checks never read the agent's answer text.
- **Verdicts:**
  - `approve`: checked and right;
  - `revise`: checked and wrong;
  - `unevaluated`: not checked (an undecided answer key, a dry run or a crash). Unevaluated is never a pass.

## Setup
```bash
uv sync
cp .env.example .env      # then fill in the two instance passwords and at least one LLM key (Gemini keys 1–5, W&B)
```

## Run it
```bash
# the agent: start with --dry-run (it changes nothing)
uv run python -m email_agent "What needs my reply today, and find the mail where they agreed the price." \
    --instance keystone --dry-run
#   --instance suryodaya · --as-of 2026-09-29 · --mailbox <address>
#   --provider gemini|openai (which goes first) · --model <id> (its first model) · --no-fallback (pin one model)
#   --quiet (answer only) · --verbose (arguments, results, memory, prompt sizes)

# the harness: run, then score (two steps, so every run is saved before it is scored)
uv run python -m harness.run --instance keystone          # or --only <task-id>,<task-id> · --dry-run
uv run python -m harness.score harness_runs/<batch>        # → report.md with a verdict per task

```

## Repository map

| Folder | What is in it |
|---|---|
| `email_agent/` | The agent: loop, skills, prompts, MCP and REST clients, its pydantic contracts |
| `harness/` | Tasks, checks (predicates), answer keys, runner and scorer |
| `docs/` | Gap Report |
| `tests/` | Hand-written tests |
