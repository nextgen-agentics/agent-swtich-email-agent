# Team 10: an email assistant for AgentSwitch, and the checker that proves it works

This repository holds two things:

1. **An email agent**: a program that reads a company mailbox, works out what needs doing, and does it (flags mail
   that needs a reply, finds agreed prices, writes summaries, sets reminders, remembers things about customers, sorts
   the inbox). It uses AI models to read and judge mail, and ordinary code for everything that must be exact.
2. **A harness**: a set of 28 test tasks and the code that runs the agent on them and then checks the **database**
   (not the agent's own words) to decide whether it did the job.

It was built for the AgentSwitch capstone course, for the **Email seat** (one "seat" = one job in a simulated
company). AgentSwitch is a shared business platform used by the whole class: email, customers, deals, quotations,
invoices, calendar and more, in one database. Our agent works on two of its company "books" (two separate companies
with their own data):

| Book | Company | Country, money, tax |
|---|---|---|
| **Suryodaya** | Suryodaya Precision Works | India, rupees, GST |
| **Keystone** | Keystone Precision Works LLC | United States, dollars, sales tax |

The course's own request for this seat is: **"What needs my reply today, and find the mail where they agreed the
price."** The agent answers that, and does several more inbox jobs.

---

## Contents

1. [What the agent can do](#1-what-the-agent-can-do)
2. [What it refuses to do](#2-what-it-refuses-to-do)
3. [What makes it different](#3-what-makes-it-different)
4. [Compared with AI email products](#4-compared-with-ai-email-products)
5. [How a run works, step by step](#5-how-a-run-works-step-by-step)
6. [How it stays safe](#6-how-it-stays-safe)
7. [When something goes wrong](#7-when-something-goes-wrong)
8. [How memory is used](#8-how-memory-is-used)
9. [Searching the mailbox](#9-searching-the-mailbox)
10. [The inbox watcher](#10-the-inbox-watcher)
11. [Traces: seeing what a run did, step by step](#11-traces-seeing-what-a-run-did-step-by-step)
12. [Running it inside AgentSwitch](#12-running-it-inside-agentswitch)
13. [What is saved, and where](#13-what-is-saved-and-where)
14. [The harness: how we prove it works](#14-the-harness-how-we-prove-it-works)
15. [Setting it up](#15-setting-it-up)
16. [Running it](#16-running-it)
17. [Settings](#17-settings)
18. [Libraries and tools used](#18-libraries-and-tools-used)
19. [How the repository is organised](#19-how-the-repository-is-organised)
20. [Where things stand: done, pending, future](#20-where-things-stand-done-pending-future)
21. [Where to read more](#21-where-to-read-more)

---

## 1. What the agent can do

You ask in plain English; the agent picks the right job (a "skill") for each part of what you asked.

| Skill | Ask it | What it changes on the platform |
|---|---|---|
| Triage replies | "What needs my reply today?" | Flags each conversation that needs a reply, due today |
| Find price agreements | "Find the mail where they agreed the price." | Saves each agreed price as a memory note on that customer, and stars the conversation |
| Summarise conversations | "Summarise each conversation in my mailbox." | Writes a short summary on each conversation that has none, or an out-of-date one |
| Follow-up reminders | "Remind me to follow up wherever I am waiting on a reply." | Creates a "remind me if they don't reply" reminder |
| Remember about a customer | "Remember that Cardinal wants every quote in USD." / "What do we remember about Kirloskar?" | Saves (or reads back) a memory note linked to that customer |
| Sort the inbox | "Sort my inbox." | Sets each conversation's importance and category tab |

One request can ask for several things ("What needs my reply today, and find the agreed price"). Each becomes its own
goal, and the goals are worked on at the same time.

Every answer says what was done, what was left alone and why, and anything that **needs your check** (section 6).

## 2. What it refuses to do

It says no, changes nothing, and saves the reason as data (so the harness can check the refusal without reading the
answer) when a request:

- needs another department's data ("What is the plant head's salary?") → *out of seat*;
- is about a mailbox that is not ours → *not our mailbox*;
- asks for something it must not do (send mail, delete shared mail) → *not permitted*;
- has no support in the mail (a price nobody agreed, a quote request that does not exist) → *no evidence* /
  *unknown record*.

## 3. What makes it different

**It does the job, not just help with it.** AI email products make a person faster in their inbox. This agent reads
the mail, decides, and makes the change (the flag, the summary, the reminder, the memory note), then reports what it
did and why.

**It works next to the business records, not just the mail.** AgentSwitch keeps the mail in the same database as the
customers, deals and sales orders, and each conversation is linked to its customer and deal. So when the agent finds
"we are awarding RFQ-2026-0003 per your quote", it follows the link to the customer and the deal, takes the figures
from our own quote line, checks that quantity × unit price = total, and compares them with the deal's line prices.
Then it saves the agreed price as a memory note on the customer, where other agents can read it. A product that sees
only the inbox cannot do that.

**It is careful in ways an inbox add-on does not need to be:**
- **Two different AI models must agree** before anything is written. A second model re-checks every verdict that
  would change data; where they disagree, nothing is written and the conversation is listed under "Needs your
  check".
- **A checker reviews the evidence before every answer**, and a third pass scores the answer from 0 to 100.
- **The AI judges; code decides the facts.** Code picks the conversations, checks the figures, builds every change
  and refuses anything outside our mailboxes.
- **Nothing is ever written twice, even after a crash.** Every write is recorded before it is sent; a stopped run
  can be resumed, and it first reads the live data to see which writes really happened.
- **Every change can be undone**, and is made with the seat's own login, so the platform shows who did it.

**It scales and stays fast.** An AI "planner" breaks the request into goals and next steps, and the steps run at the
same time where they can. Mailbox-wide jobs are split into groups of 20 conversations that the AI judges in parallel,
so the number of planning calls does not grow with the size of the inbox. It is built for mailboxes of tens of
thousands of messages, and keeps its own local copy of the mailbox so it never downloads everything twice.

**It remembers, watches, and can be traced.** It keeps what it knows about customers, what earlier runs did, and your
standing rules for each company. It can watch the inbox and react to new mail by itself, within daily limits, without
reacting to its own changes. Every run is recorded step by step and can be viewed in standard tracing tools.

**One agent for every company on the platform.** It reads each company's country, currency, tax and date format from
the platform, so the same agent works unchanged for the Indian company in rupees and the US company in dollars.
Adding another company is one line of configuration.

**It is checked by the database, not by its own words.** The harness reads the database after each run and compares
it with answer keys decided by a person.

**Our own code throughout.** The loop that runs the agent is written here (adapted from the course's S17 reference
design), not taken from an agent framework.

## 4. Compared with AI email products

Our [gap report](docs/project/gap-report.md) compared AgentSwitch's email app with the leading AI email products
(**Shortwave**, **Superhuman**, **Fyxer**, **Inbox Zero**). Each feature they have, and where this agent stands:

| Feature (who has it) | What this agent does today |
|---|---|
| **"Needs you" list** (Fyxer, Inbox Zero, Shortwave) | ✅ **Built** (*triage replies*). Reads each conversation's own messages (the platform's "last sender" field is stale), decides who owes a reply, flags the ones that need us with today's due date (they show as *Due* in the inbox), and gives the reason for each |
| **Automatic sorting** (Superhuman, Shortwave, Fyxer) | ✅ **Built** (*sort the inbox*). Sets importance and the category tab (Important / Team / VIP / News / Social / Other) on every conversation. Not done: archiving, labels, per-sender rules |
| **Summaries on every conversation** (Shortwave, Superhuman) | ✅ **Built** (*summarise conversations*). Writes the summary field, only where it is missing or older than the newest message |
| **Automatic follow-up reminders** (Superhuman, Fyxer) | ✅ **Built** (*follow-up reminders*). Creates "remind me if no reply" reminders where we are waiting on the other side. Not done: a drafted nudge |
| **Acts on new mail as it arrives** (Shortwave, Inbox Zero) | ✅ **Built** (*inbox watcher*). Checks every 2 minutes (the platform cannot notify us) and starts a small run for each new message, within daily limits; dry by default |
| **Ask questions of the mailbox** (Shortwave, Superhuman) | 🟡 **Partly.** The price question is answered with evidence (mail + customer + deal). "What do we remember about X" reads the memory notes. Search by meaning finds conversations worded differently. Not done: open questions about anything in the mailbox |
| **Rules in plain English** (Shortwave, Inbox Zero) | 🟡 **Partly.** Your house rules for each company (`rules/<book>.md`) are followed in every run, and the watcher's rules say what to do on new mail. Not done: a rule editor in the app, or rules saved as memory per user |
| **Other AI tools can drive the mailbox (MCP)** (Superhuman, Shortwave) | ✅ Already in AgentSwitch: this agent is built entirely on the platform's MCP tools |
| **Replies drafted in your style** (Superhuman, Fyxer, Inbox Zero) | ❌ **Not built.** The agent never writes or sends mail (sending is refused). The sample mailboxes cannot send anyway |
| **Blocking cold email, one-click unsubscribe** (Inbox Zero) | ❌ **Not built** |
| **Finding a file someone sent** | ❌ **Not built.** The sample attachments are not linked to any message |
| **Meetings from mail** (Shortwave, Superhuman, Inbox Zero) | ⛔ **Needs access** the Email seat does not have (the calendar) |
| **Work that belongs to other teams** | 🟡 Refused with the reason *out of seat*. Not done: handing it over as an escalation |

**What no inbox product can do, and this agent does:**
- It ties the mail to the business: it follows each conversation to its customer and deal, checks prices against
  them, and saves what it learns on the customer record for other agents.
- It is checked by the database, against answer keys.
- It works the same for every company on the platform.

The gap report lists the screen changes AgentSwitch would need to show this work well. Examples: a "Needs you" view
with the agent's reasons, summaries in the message list, and undo for what the agent changed.

## 5. How a run works, step by step

```
 you ask ─► 1 who, where, when ─► 2 planner: goals ─► 3 planner: next steps ─► 4 steps run (in parallel)
                                                          ▲                          │
                                                          └──── 5 results ◄──────────┘
                                         ... until every goal is answered or refused ─► 6 answer + files
```

1. **Who, where, when.** Before any AI is used, code collects the facts: who we are, which mailboxes are ours, today's
   date, the company's country and currency, and your standing rules for this book.
2. **Goals.** The planner (an AI model) reads the request and the list of skills. It splits the request into goals,
   one skill each. A goal with no fitting skill is refused straight away.
3. **Next steps.** The planner looks at the goals and everything done so far, and adds the next few steps (at most 4
   at a time). Code checks every step before accepting it:
   - Is it allowed for this goal's skill?
   - Are its details valid?
   - Is it a repeat of work already done?

   A rejected step goes back to the planner with the reason, up to 3 tries.
4. **Steps run.** Ready steps run at the same time, up to 6. A step is either:
   - **one tool call**: read a record, look up a customer, save a memory note; or
   - **a whole-mailbox job** (`judge_threads`), which expands into its own small chain:
     ```
     pick conversations (code) ─► judge in groups of 20 (AI, in parallel) ─► collect + check (code)
         ─► a second model re-checks what would change data ─► write the changes (code)
     ```
5. **Results** go back to the planner, which adds the next steps, or ends a goal with an answer or a refusal. Before a
   goal ends, an AI **checker** looks at the evidence: is every part of the goal backed by real results? If not, the
   planner must do more.
6. **The answer** is written from the evidence only, scored, and saved with everything else (section 13).

The whole run is kept as a **graph**: each step is a box, and an arrow means "this one waits for that one". The graph
and every event in it are saved in a small database file as the run goes. That is what makes resuming and tracing
possible.

**Limits per run:** 12 planning rounds, 80 AI calls, 80 steps, 6 steps at once, 3 AI calls at once, 2 platform calls
at once. All of them are saved, so a resumed run keeps what it has already used. Every platform call also has a
90-second time limit.

**Which AI answers.** Each call goes to the first usable option on one ordered list: Gemini (up to five keys; the extra
keys are used only when the first is out of quota or refused), then three models on W&B Inference (DeepSeek, GLM,
Qwen). An option that is rate-limited, out of quota or failing is rested or dropped automatically, and every reply
records who answered.

## 6. How it stays safe

The platform is shared with another team working on the same rows, so the agent is careful by design:

- **Only our mailboxes.** Rows from other mailboxes are removed from every result before the AI sees them, and code
  refuses to change anything that is not in our mailboxes or created by our login.
- **Dry run.** `--dry-run` does everything except send changes: they are recorded as "would write".
- **Approval gate.** `--approve-writes` stops before any change and lists what it would write. You then answer with
  `--approve` or `--reject`.
- **Two models must agree** before a judged change is written. Disagreements are held for you ("Needs your check").
- **A record before every write.** Each write is recorded, with a key and the old values, before it is sent, so it is
  never sent twice and can always be undone.
- **Undo.** `scripts/agent/undo_run.py` takes back what a run wrote. It does this only where the row still holds what
  the agent wrote, so it never overwrites someone else's later change.
- **Refusals are data**, not just words in the answer.
- **The AI never decides what is allowed.**
  - Which tools a goal may use comes from its skill file.
  - Which rows may change is decided by code.
  - Neither comes from anything the AI or the mail says: mail and results are treated as untrusted data.

## 7. When something goes wrong

- **Crash, Ctrl-C or waiting for approval:** the run's files are still written, and the last line printed gives the
  exact command to continue: `--resume runs/<run_id>`.
- **Resume** continues the same run in its own folder.
  - First it checks every write it was unsure about, by reading the live row: did the write happen, was it never sent,
    or did someone else change the row?
  - Then it carries on.
  - AI replies already received are reused, not paid for again.
- **A platform call with no answer** fails after 90 seconds on its own. If it was a write, it is checked against the
  live row before the run ends. Opening and closing the connection have time limits too (60 and 20 seconds).
- **A failing AI model** (busy, out of quota, a bad key) moves the call to the next option. If every option is
  resting, the run waits a little, then stops cleanly with the reason.
- **Crash drills pass:** a script stops the agent on purpose at 6 different points and resumes it. Each time it checks
  that nothing was lost or written twice (section 20).

## 8. How memory is used

The agent has eight kinds of memory. Each has one place it lives and one way it reaches the AI:

| Memory | What it is | Where it lives | How long |
|---|---|---|---|
| **Standing instructions** | how the agent should behave: its prompts, one instruction file per skill, and **your house rules** for each company (`rules/<book>.md`, optional) | `email_agent/prompts/`, `email_agent/skills/`, `rules/` | until you change them |
| **What we know about customers** | memory notes saved on the platform (e.g. "Cardinal wants quotes in USD"). The agent keeps a local copy, and reads a customer's notes before saving a new one, so it never saves the same note twice | the platform + `state/<book>/memory.sqlite` | until switched off or expired |
| **Saved verdicts** | a conversation that has not changed since it was last judged is not judged again (command-line runs only; the harness always judges afresh) | `state/<book>/memory.sqlite` | until the conversation changes |
| **The run's own working memory** | the graph: every step, its result and its state | `runs/<id>/run.sqlite` | one run |
| **Large results** | a tool result too big for the AI is saved to a file; the AI sees a preview | `runs/<id>/artifacts/` | one run |
| **The to-do list** | the graph's pending steps, shown as a checklist in the terminal and the report | `runs/<id>/run.sqlite` | one run |
| **Past runs** | a one-line summary of each run (what was asked, how it ended, what was written). The planner sees the last 3 on the same mailbox, as history only | `state/<book>/memory.sqlite` | permanent |
| **Keeping the AI's input small** | long results are cut to their key fields; when the graph gets very long, finished work is folded away behind a visible note | — | per planning round |

A memory note always says where it came from: the request, or the email it was taken from. A broad note (about the
whole company) can be used in a narrow request (about one customer), never the other way round, so one customer's
notes are never shown for another.

## 9. Searching the mailbox

- **By words (the default):** a fast full-text index over the local copy of the mailbox.
- **By meaning (optional, `--search hybrid`):**
  - each message is turned into a list of numbers that captures its meaning (Google's embedding model);
  - conversations close in meaning to the request are found even when they use other words: "customer paid less than
    the invoice" finds the "Short payment on INV-…" threads;
  - the results are mixed with the word search, and the AI still judges every candidate.

On 20 test questions written to avoid the conversations' own words, meaning-based search found every expected
conversation in the top 5, where the word search found none. It stays off by default until a person confirms those
test questions (section 20).

## 10. The inbox watcher

`email-watch` (or `python -m email_agent.watch`) checks a book every 2 minutes (the platform cannot notify us). It
turns what changed into events: a new message from someone else, or a conversation that changed. For each event:

1. it is recorded once, so seeing the same change again does nothing;
2. it is refused if **we caused it**: the watcher keeps a list of every write the agent and the undo script sent, so
   the agent never reacts to its own changes;
3. it is refused if one mailbox suddenly floods (more than 30 events a minute);
4. it is matched against your rules in `watch/subscriptions.yaml` (for example: "on new mail, ask: What needs my reply
   today?");
5. a run is started, **limited by code to that one conversation**. It is a **dry run** unless the rule says
   `live: true` *and* the watcher was started with `--live`. Each rule has a daily limit on runs and on AI calls.

Every decision (run started, refused and why, nothing to do) is saved, and `--status` shows them.

## 11. Traces: seeing what a run did, step by step

Every run also writes its timeline as a **trace** (`runs/<id>/spans.jsonl`). The trace holds:
- the run itself, each planning round and each step;
- each AI call, with which model answered and how many tokens it used;
- each write.

Each entry has real start and end times, and says which larger entry it was part of. Nothing new has to be recorded
for this: the trace is built from the run's own event log.

It uses **OpenTelemetry**, the common standard for traces, so a run can be sent to any tracing tool (Jaeger, Grafana,
Honeycomb, …) and viewed as a timeline (`email-trace runs/<id> --otlp <address>`). With the
`OTEL_EXPORTER_OTLP_ENDPOINT` setting, every run sends its trace by itself. No email text or AI prompt goes into a
trace.

## 12. Running it inside AgentSwitch

The agent already runs **against** AgentSwitch from outside: it logs in as the Email seat and uses the same tools a
person in that seat may use. This is how AgentSwitch could run it **as** the Email seat's agent.

**How it plugs in today.**
- It logs in with the seat's account through the platform's web API, then calls the platform's tools over **MCP**
  (the standard way AI agents call tools) at `<book address>/api/mcp`. It needs nothing that the seat does not
  already have.
- **It reads:** conversations, messages, mailboxes, reminders, memory notes, customers and deals.
- **It writes only:**
  - conversations: flag and due date, star, summary, importance and category tab;
  - memory notes;
  - reminders.
- Every change is made with the seat's own login, so the app shows who made it, and each one is listed (with the old
  values) in the run's `writes.jsonl`.

**Three ways the platform could use it:**

1. **On request** — a person asks in the app, the platform runs the agent and shows the answer:
   ```bash
   uv run email-agent "What needs my reply today?" --instance keystone
   ```
   or from Python, which returns the answer, each goal's result and every change made:
   ```python
   import asyncio
   from email_agent.agent import run
   from email_agent.config import get_settings

   outcome = asyncio.run(run("What needs my reply today?", "keystone", get_settings(), dry_run=True))
   print(outcome.final.answer)                 # the answer, for the person
   for goal in outcome.final.goals:            # each goal: answered, or refused with a reason
       print(goal.text, goal.done, goal.refusal)
   print(len(outcome.writes), "changes")       # every change (or "would change" in a dry run)
   ```
2. **In the background** — the inbox watcher as a long-running service, one per company:
   ```bash
   uv run email-watch --instance keystone            # dry runs only
   uv run email-watch --instance keystone --live     # real changes, for rules marked live: true
   ```
   The platform decides what it may do through `watch/subscriptions.yaml` (which events, which request, live or not,
   and a daily cap on runs and on AI calls). `email-watch --status` shows what it did and refused today.
3. **With a person approving** — the agent proposes, a person decides:
   ```bash
   uv run email-agent "Flag what needs my reply" --instance keystone --approve-writes
   # the run stops "waiting"; its report.md lists every change it would make
   uv run email-agent --resume runs/<run_id> --approve    # or --reject
   ```

**Where results show up in the app:**

| Change | Where it shows in AgentSwitch |
|---|---|
| Reply flags with today's due date | the inbox, as *Due* |
| Stars, summaries, importance and category | the conversation, its summary field, and the inbox tabs |
| Memory notes (agreed prices, customer preferences) | the agent-memory records, linked to the customer |
| Follow-up reminders | the conversation's reminders |

**Adding another company book** takes one line in `email_agent/config.py` (`INSTANCES`: its name and web address),
and its password in `.env`. The agent reads that company's mailboxes, country, currency and date format from the
platform itself.

**Watching it work:**
- each run's `report.md` and `spans.jsonl`;
- `email-watch --status`;
- with `OTEL_EXPORTER_OTLP_ENDPOINT` set, every run's trace goes to the platform's own tracing tool.

**What it needs:**
- Python 3.12 and `uv`;
- network access to the book;
- one AI key (Gemini or W&B);
- a writable `state/` folder for its local copies.

**What AgentSwitch would need to add for a smoother fit** (from the gap report):
- an event when new mail arrives, so the watcher does not have to check every 2 minutes;
- a "Needs you" view that shows the agent's reasons;
- summaries shown in the message list;
- an undo button for the agent's changes.

## 13. What is saved, and where

| Where | What | In git? |
|---|---|---|
| `runs/<run_id>/` | one folder per run (see below) | no |
| `harness_runs/<batch>/` | one folder per harness batch: each task's saved run, then `report.md` with a verdict per task | no |
| `state/<book>/mailbox.sqlite` | the local copy of our mailboxes (conversations, messages, worked-out facts, word index, meaning vectors) | no |
| `state/<book>/memory.sqlite` | the local copy of memory notes, past-run summaries, saved verdicts, memory vectors | no |
| `state/<book>/events.sqlite` | the watcher's events, decisions, daily counts, and the list of writes we sent | no |
| `state/<book>/*.faiss` | fast lookup files for meaning-based search (rebuilt from the databases if missing) | no |
| `rules/<book>.md` | your house rules (optional) | yes |
| `watch/subscriptions.yaml` | the watcher's rules | yes |
| `data/` | what the research and check scripts saved (platform schemas, bug probes, drill and search results) | yes |
| `.cache/` | the login token (never printed) | no |

**Inside one run folder** (`runs/<run_id>/`), written even if the run crashes:

| File | What it holds |
|---|---|
| `request.json` | what was asked, and every option (written first, before anything else) |
| `context.json` | who, where, today, our mailboxes, house rules |
| `run.sqlite` | the graph and its event log (every step, AI call and write), saved AI replies, the write record |
| `steps.jsonl` | one line per step, including each full AI request and reply |
| `writes.jsonl` | one line per change made (or that a dry run would make), with the old values; this is what undo reads |
| `final.json`, `outcome.json` | the answer, each goal's result or refusal, tokens used, who answered |
| `report.md` | all of the above, readable |
| `spans.jsonl` | the trace (section 11) |
| `artifacts/` | large results, if any |

## 14. The harness: how we prove it works

- **28 tasks** (`harness/tasks.yaml`) across both books: the seat request, every skill, and 14 tasks where the right
  answer is a refusal.
- **Running and scoring are separate steps.**
  - `harness-run` runs the agent on each task, and saves the run and the database state *before* any scoring.
  - `harness-score` then reads the **database** and compares it with an **answer key** (`harness/ground_truth/`). A
    person decided the answer key by opening each conversation in the web app.
- **The checks never read the agent's answer text.** A flag either is on the row or is not.
- **Three verdicts:**
  - `approve`: checked and right;
  - `revise`: checked and wrong;
  - `unevaluated`: could not be checked (an undecided answer key, a dry run, or a crash). Unevaluated never counts as
    a pass.
- A script proposes answer-key entries (`scripts/agent/propose_ground_truth.py`); a person decides each one.

## 15. Setting it up

You need Python 3.12 or newer, and [uv](https://docs.astral.sh/uv/).

```bash
uv sync                     # installs the agent, the harness and the development tools
cp env.example .env         # then fill in .env: the two book passwords, and at least one AI key (Gemini or W&B)
uv sync --group otel        # optional: only needed to send traces to a tracing tool
```

`.env` is never committed. The terminal and logs name Gemini keys by their slot number (#1, #2…), never the key.

**Checking the code** (the first three must pass before a commit):
```bash
uv run ruff check           # style and mistakes (rules pinned in pyproject.toml)
uv run mypy                 # types
uv run lint-imports         # imports go scripts → harness → email_agent only
uv run pytest               # the hand-written tests in tests/ (to be written by hand: "no tests ran" until then)
```

## 16. Running it

Every command below can also be run in its long form: `uv run email-agent …` is the same as
`uv run python -m email_agent …`.

**Start with `--dry-run`:** the agent does everything except change platform data.

```bash
uv run email-agent "What needs my reply today, and find the mail where they agreed the price." \
    --instance suryodaya --dry-run
```

Remove `--dry-run` to really write (flags, stars, memory notes, reminders). Other requests:

```bash
uv run email-agent "Summarise each conversation in my mailbox." --instance keystone --dry-run
uv run email-agent "Remind me to follow up wherever I am waiting on a reply." --instance keystone --dry-run
uv run email-agent "Sort my inbox." --instance suryodaya --dry-run
uv run email-agent "Remember that Cardinal Tillage Works wants every quote in USD." --instance keystone --dry-run
uv run email-agent "What do we remember about Kirloskar Pumps?" --instance suryodaya
uv run email-agent "What is the plant head's salary?" --instance keystone      # should refuse
```

**Options:**

| Option | What it does |
|---|---|
| `--instance suryodaya` or `keystone` | which company book to work in |
| `--dry-run` | record changes without sending them |
| `--as-of 2026-09-29` | treat that date as today |
| `--mailbox <address>` | work only in this mailbox (can be given more than once); by default, all of ours |
| `--provider gemini` or `openai` | which AI provider goes first (`openai` = W&B Inference) |
| `--model <id>` | that provider's first model |
| `--no-fallback` | use only the first model, never switch |
| `--no-cache` | judge every conversation again (by default, unchanged ones reuse saved verdicts) |
| `--no-history` | the planner does not see the last runs |
| `--search hybrid` | also search by meaning (section 9) |
| `--full-sync` | re-read the whole mailbox into the local copy, not only what changed |
| `--approve-writes` | stop before any change; the run ends "waiting", and `report.md` lists the changes |
| `--resume runs/<run_id>` | continue a crashed, interrupted or waiting run (no request text needed) |
| `--resume … --approve` / `--reject` | send the changes waiting for approval, or decline them |
| `--quiet` / `--verbose` | only the answer, or extra detail |

**House rules** (optional): write plain instructions for one book in `rules/<book>.md`, for example
`rules/keystone.md`. Every run on that book uses them, and `report.md` shows them. Keep them short (4,000 characters
at most):
```markdown
- Name the customer by its full company name in every answer.
- Newsletters and automatic notices never need a reply.
```

**The inbox watcher:**
```bash
uv run email-watch --instance suryodaya            # check every 2 minutes until Ctrl-C
uv run email-watch --instance suryodaya --once     # one check
uv run email-watch --instance suryodaya --status   # today's counts, refusals, last decisions
uv run email-watch --instance suryodaya --replay <message_id>   # react to a recorded message again
```

**Traces and reports:**
```bash
uv run email-trace runs/<run_id> --console                              # print the trace
uv run email-trace runs/<run_id> --otlp http://localhost:4318/v1/traces # send it to a tracing tool
uv run email-report runs/<run_id>                                       # rebuild a run's report.md
```

**The harness** (run, then score):
```bash
uv run harness-run --instance suryodaya                  # every Suryodaya task (writes for real)
uv run harness-run --only refuse-salary-keystone,price-keystone   # chosen tasks
uv run harness-run --dry-run --instance keystone         # changes nothing (verdicts: unevaluated)
uv run harness-score harness_runs/<batch>                # report.md with a verdict per task
```
`harness-run` takes the same `--provider`, `--model`, `--no-fallback`, `--quiet` and `--verbose` options. After a live
batch, undo it: `uv run python scripts/agent/undo_run.py harness_runs/<batch>`.

**Other useful commands:**
```bash
uv run python scripts/agent/undo_run.py runs/<run_id>            # take back one live run's changes (--dry-run to preview)
uv run python scripts/agent/propose_ground_truth.py --instance keystone --feature price   # needs-reply|price|follow-ups|sort
uv run python scripts/agent/eval_search.py --instance suryodaya  # how well each kind of search finds the test questions
uv run python scripts/agent/drill_resume.py --fake               # the crash drill on a toy graph (no platform, no AI)
uv run python scripts/agent/drill_resume.py --agent --instance suryodaya   # the crash drill on the real agent (dry)
uv run python scripts/agent/check_mirror.py --instance suryodaya # does the local mailbox copy match the platform?
uv run python scripts/repo/check_submission.py                   # PASS/FAIL against what the course grades
uv run python scripts/repo/refresh.py                            # regenerate every generated doc and data file
```

## 17. Settings

All settings come from `.env` (template: `env.example`) and have sensible defaults (`email_agent/config.py`). The
ones you are most likely to change:

| Setting | What it controls |
|---|---|
| `AS_EMAIL`, `AS_SURYODAYA_PASSWORD`, `AS_KEYSTONE_PASSWORD` | the platform login |
| `PROVIDER` | which AI provider goes first: `gemini` or `openai` (W&B) |
| `GEMINI_MODEL`, `GEMINI_API_KEY` … `GEMINI_API_KEY_5` | the Gemini model and keys (keys 2–5 only as backup) |
| `WANDB_API_KEY`, `WANDB_MODELS` | the W&B key and its models, in order |
| `MAX_PLANNER_ROUNDS`, `MAX_LLM_CALLS`, `MAX_NODES` | the per-run limits (12, 80, 80) |
| `MAX_WORKERS`, `LLM_CONCURRENCY`, `MCP_CONCURRENCY` | how much runs at once (6 steps, 3 AI calls, 2 platform calls) |
| `VALIDATE_VERDICTS` | the second-model check (on by default) |
| `SEARCH` | `fts` (words, the default) or `hybrid` (words and meaning) |
| `OTEL_EXPORTER_OTLP_ENDPOINT` | send every run's trace to a tracing tool |

## 18. Libraries and tools used

| Library / tool | What we use it for |
|---|---|
| [uv](https://docs.astral.sh/uv/) | installing, locking and running everything |
| `mcp` (the official MCP SDK) | talking to the platform's tools (MCP is the standard way AI agents call tools) |
| `httpx2` | the platform's web API (login, and a few calls MCP cannot do) |
| `google-genai` | Gemini models (answers and embeddings) |
| `openai` | W&B Inference models (they speak the same format) |
| `pydantic`, `pydantic-settings` | a checked data shape at every hand-off, and the settings |
| `tenacity` | retrying failed calls with growing waits |
| `faiss-cpu`, `numpy` | fast "closest meaning" lookup for meaning-based search |
| `opentelemetry-sdk`, `opentelemetry-exporter-otlp-proto-http` | sending traces (optional group) |
| `rich` | the live terminal view |
| `pyyaml` | the harness tasks, answer keys and watcher rules |
| Python's own `asyncio`, `sqlite3`, `graphlib` | running steps at the same time; every local database; checking that the graph has no loops |
| `ruff`, `mypy`, `import-linter` | checking style and mistakes, types, and the one-way imports (development) |
| `pytest`, `pytest-asyncio` | the hand-written tests (development) |
| `datamodel-code-generator` | generating the checked shapes of the platform tools' inputs (development) |

The project is set up the standard way (`pyproject.toml`, following the course's S17 reference): pinned lower
versions for every library, a `dev` group for the checking tools, an optional `otel` group, named commands
(`email-agent`, `email-watch`, `email-trace`, `email-report`, `harness-run`, `harness-score`), and the ruff, mypy,
pytest and import rules written down in the file so every machine checks the same things.

The design of the planner, graph, write record, checker, memory, watcher and traces is adapted from the course's
**S17** reference code (credited in each file). It is rewritten with checked data shapes and a local database instead
of whole JSON files, with fixes for gaps we found in S17.

## 19. How the repository is organised

```
email_agent/           the agent
  __main__.py            the command line (email-agent)
  agent.py               one run from start to finish, and resume
  config.py              settings
  graph/                 the planner, the step runner, the event log, the write record, the steps themselves,
                         the mailbox-wide jobs ("flows"), and the check of unsure writes (reconcile)
  platform/              talking to AgentSwitch: MCP and web clients, the run's facts, tool calls, the write path
  llm/                   the ordered list of AI models and keys, with automatic switching
  mailbox/               the local mailbox copy: storage, syncing, each conversation's facts, search
  memory/                memory notes, past runs, saved verdicts
  watch/                 the inbox watcher (email-watch)
  record/                what a run leaves behind: log, report, terminal view, trace, large results
  common/                small shared helpers
  contracts/             the checked data shapes, by area
  prompts/               the instructions given to each AI role
  skills/                one folder per skill, each with its SKILL.md
harness/               the 28 tasks, the checks, the answer keys, the runner and the scorer
scripts/               run by hand
  agent/                 crash drills, search check, mailbox-copy check, undo, answer-key proposer
  platform/              exploring and probing the platform (read-only unless a probe says otherwise)
  bugs/                  keeping our bug reports in step with the class board
  repo/                  regenerate docs and data, generate data shapes, check the submission
  contracts/             the scripts' own data shapes
docs/                  plans, design, guides, platform notes, bugs (start at docs/README.md)
data/                  what the scripts saved
watch/                 the watcher's rules
pyproject.toml         dependencies, named commands, and the ruff / mypy / pytest / import rules
```

Imports only go one way: `scripts` may use `harness` and `email_agent`, `harness` may use `email_agent`, and the agent
uses neither. `uv run lint-imports` checks this.

## 20. Where things stand: done, pending, future

**Done.** All of it is checked; the evidence for each item is in [docs/project/plan.md](docs/project/plan.md).
- The six skills and the refusals.
- The planner-led graph with parallel judging.
- The second-model check, the evidence checker and the answer score.
- The local mailbox copy: its reads match the platform exactly on both books.
- Resume after a crash, checking unsure writes against the live data, and the approval gate. The crash drill passes at
  every crash point: 8 of 8 on the real agent (dry), 12 of 12 on the toy graph.
- Memory: customer notes, past runs, house rules, saved verdicts.
- The inbox watcher, meaning-based search (off by default), and traces.
- The harness: on the latest dry runs every task finishes, and all 14 refusal checks pass.
- Standard project setup: ruff, mypy and the import rule all pass on the whole codebase.

**Pending: needs a person** (tracked in [docs/project/pending.md](docs/project/pending.md)):
- run the harness **live** on both books, score it, and undo it (live runs change the shared books);
- run the live crash drill and the live watcher check;
- decide the remaining **answer keys** (follow-ups, prices, sorting); until then those tasks score "unevaluated";
- confirm the 20 search test questions, then decide whether meaning-based search becomes the default;
- replace Gemini key #2 (it is invalid);
- write the hand-written tests (the course requires them to be written by hand).

**Known limits** (see [docs/bugs/revision-12-findings.md](docs/bugs/revision-12-findings.md#3-still-open)):
- price verdicts on some purchase-order confirmations differ between runs (the second model holds them for you);
- meaning-based search can still add one off-topic conversation;
- a resumed run counts one planning round twice (the AI reply itself is reused for free).

**Future work** (designed, not built):
- the email features in section 4 marked not built: drafted replies, cold-email blocking, finding files, handing work
  over to other teams;
- a web page that shows a run's graph live;
- a load test on a fake platform with 10,000–50,000 conversations;
- summarising very long event logs with AI;
- picking cheaper or stronger models by role;
- several watchers sharing the work.

**Decided against:**
- rewriting the request before planning (requests are short);
- splitting emails into chunks (emails are short);
- putting email text or prompts into traces (privacy);
- features outside the Email seat (agent-to-agent protocols, chat channels, web search).

## 21. Where to read more

| Document | What it explains |
|---|---|
| [docs/README.md](docs/README.md) | the index of every document |
| [docs/project/gap-report.md](docs/project/gap-report.md) | AgentSwitch's email app compared with AI email products, and which gaps an agent can close |
| [docs/project/orchestrator.md](docs/project/orchestrator.md) | the agent's design in depth, and the list of every feature (built, planned, future, not taken) |
| [docs/project/code-guide.md](docs/project/code-guide.md) | every file, script and data file, one line each |
| [docs/project/plan.md](docs/project/plan.md) | how it was built, stage by stage, with the evidence |
| [docs/project/pending.md](docs/project/pending.md) | what is still open, and suggested test cases |
| [docs/bugs/revision-12-findings.md](docs/bugs/revision-12-findings.md) | every bug and workaround found while building it |
| [docs/bugs/README.md](docs/bugs/README.md) | the platform bugs we found and filed |
| [docs/glossary.md](docs/glossary.md) | every term used in the docs, in plain words |
