# Known issues: platform bugs, our own bugs, and limits

## 1. Problems outside our code, and how we work around them

### The AgentSwitch platform

| # | The problem | How we found it | What our code does about it | Where | Reported? |
|---|---|---|---|---|---|
| P1 | A conversation field can be **set** through MCP but never **cleared**: the tool refuses `null` (REST accepts it) | undo could not put back an empty flag (Stage 3) | a write that clears a field is sent through REST `PUT /api/EmailThread/<id>` | `email_agent/platform/writes.py` (`_send`) | not yet reported |
| P2 | Listing agent memories **without** `is_active` also returns switched-off ones (the documented default is ignored) | memory checks (Stage 3; confirmed live in Stage 7) | the memory read copy uses this on purpose (it sees the whole lifecycle); every check of "what we remember" filters on active rows itself | `email_agent/memory/sync.py`, `email_agent/memory/store.py` | draft ready (C2) |
| P3 | `is_active` on memories comes back as `0` / `1` (or `"0"` / `"1"`), not true / false | Stage 3, Stage 7 | the row contracts read them as booleans | `email_agent/contracts/platform.py` | maybe (C3) |
| P4 | One MCP call got **no answer for 5 minutes**, and the transport's own timeout ended the whole session | live price run, Keystone (Stage 4) | every MCP call has a 90-second limit; a write with no answer is marked "uncertain" and later settled by reading the live row (reconcile) | `email_agent/platform/mcp_session.py`, `email_agent/graph/reconcile.py` | no (seen once; C4) |
| P5 | The server answers our MCP calls **one at a time**, about 1 second each whatever their size | live measurement (Stage 0) | read in big pages (1,000 rows), keep a local copy of the mailbox, at most 2 calls at once | `email_agent/mailbox/sync.py`, `email_agent/config.py` (`mcp_concurrency`) | no (a fact to design for; C5) |
| P6 | A conversation's `updated_at` can be a few milliseconds **older** than its newest message | mailbox-copy check (Stage 2) | messages are synced on their own, not only through their conversation | `email_agent/mailbox/sync.py` | no (C6) |
| P7 | The **"who changed it" fields cannot tell the agent from a person**: the agent and the person share one login, and on Keystone even the inbound customer mail was created under it | inbox watcher (Stage 8) | the watcher keeps its own record of every write the agent sent and every undo step; a change within 5 minutes of one of them counts as ours | `email_agent/watch/store.py` (`our_writes`), `email_agent/watch/governor.py` | no (a shared-login fact) |
| P8 | Text fields arrive in three shapes; booleans as `0/1`, counts as decimals, times without a zone (older finding, still handled) | Revisions 2–4 | the row contracts accept every shape and turn it into one | `email_agent/contracts/platform.py` (`WORKAROUND(BUG-001)`, `WORKAROUND(BUG-008)`) | filed (BUG-001, BUG-008) |
| P9 | A conversation's summary and counter fields are not kept up to date (older finding) | Revision 4 | facts are always worked out from the messages, never from those fields | `email_agent/mailbox/conversation.py` (BUG-005) | filed (BUG-005) |
| P10 | The pre-loaded Suryodaya mail has "mirror copies": a message repeated word for word as if we had replied (older finding) | Revision 4 | such copies do not count as replies | `email_agent/mailbox/conversation.py` (`WORKAROUND(BUG-022)`) | filed (BUG-022) |
| P11 | Searching agent memory by its text never matches (older finding) | Revision 3 | memory is searched in our own local copy (full text, and by meaning in hybrid mode); price memories carry a machine-readable first line so checks find them by run | `email_agent/memory/store.py`, `email_agent/graph/flows.py` | filed (BUG-014) |
| P12 | Some list results include rows from mailboxes that are not ours (older finding) | Revision 5 | those rows are left out before the model sees them | `email_agent/platform/action.py` | filed (BUG-019, BUG-026) |

### The model providers (Gemini, W&B)

| # | The problem | What our code does about it | Where |
|---|---|---|---|
| M1 | Gemini key #1 used up its **daily chat quota**, and **key #2 is invalid** (401 "invalid authentication credentials", checked 2026-10-04) | the route rests a key out of quota until the next day, drops a refused key, and moves on to the W&B models; every reply records who answered | `email_agent/llm/route.py` |
| M2 | On W&B, DeepSeek sometimes **could not finish** within its time or token limit; single calls then took 2–8 minutes | the route falls back to the next model (GLM, then Qwen); a reply cut off by the token limit is retried once with double the budget | `email_agent/llm/route.py` |
| M3 | Gemini's free tier counts **every text in an embedding batch as one request**, 100 a minute, so one mailbox used the whole minute | the embedder paces itself to 90 texts a minute and waits 15 / 30 / 60 s on a 429 | `email_agent/mailbox/search.py` (`Embedder._pace`) |
| M4 | Gemini **refuses an empty text** (400), which made it refuse a whole batch | empty messages get no vector; a 400 is reported at once with its own message, not hidden behind another key's error | `email_agent/mailbox/search.py` |
| M5 | `gemini-embedding-2` turns a **list of texts into one vector** (it merges them) | we use `gemini-embedding-001`, which gives one vector per text | `email_agent/config.py` (`embed_model`) |
| M6 | The Gemini SDK switches on "automatic function calling" by default and **warns on every call** | it is switched off explicitly (we offer no tools) | `email_agent/llm/route.py` (`GeminiClient._config`) |
| M7 | Opening and closing an MCP session had **no overall time limit**: a slow server stalled a harness batch for 8+ minutes between two tasks | opening is limited to 60 s (the token check runs in a thread, so the limit can fire), closing to 20 s, `list_tools` to 90 s | `email_agent/platform/mcp_session.py` |

## 2. Our own bugs, found and fixed

Every row was found by a run, a drill or a check, fixed, and checked again.

| Stage | What went wrong | The fix | Proof after the fix |
|---|---|---|---|
| 1 | (planted on purpose) an outbox that always sends would write a row twice | the crash drill fails when any row is written twice | fake drill: the planted bug is caught |
| 3–4 | **summaries-suryodaya:** the answer ran before the parallel judging finished, ended the goal, and the shards were cancelled | anything waiting on a fan-out also waits for its final step; an answer waits for its goal's open work | summaries-keystone approves live |
| 3–4 | **mixed-reply-and-salary-keystone:** code refused the salary goal, then the planner added a second refusal with another reason, and the second won | only code ends a goal with no skill; one ending per goal; the first to finish counts | refusal approves |
| 3–4 | **remember-keystone:** the planner saw only 2 of 5 parties (results clipped to 1,800 characters) and chose a person, not the company | list results reach the planner as compact rows (up to 4,000 characters); the skill searches the full name first | memory saved on Cardinal Tillage Works |
| 3–4 | **needs-reply-suryodaya:** a recommendation that asked nothing was flagged as needing a reply | the triage skill says a recommendation with no question, decision or deadline only informs | dry re-run agrees with the answer key |
| 3–4 | undo went oldest first; the terminal view crashed when parallel calls each opened a "waiting" line; a reused write receipt could add a second writes.jsonl line | undo goes newest first; one shared waiting line; a write is recorded once | re-runs |
| 5 | **follow-ups-suryodaya:** nothing to follow up (right), but there was no write step, the evidence critic kept asking for one, and the planner then waited on a task it had dropped as a repeat; the run ended in "error" | a selection with 0 conversations carries its own "0 written, final" result; the critic accepts it; a task waiting on a dropped repeat waits on the task it repeats | rerun done in 2 steps (found in Stage 7) |
| 6 | the critic read "dry run" as "not written yet", and the planner repeated 4 writes | the critic and planner are told when a run is a dry run; the drill fails any extra write | agent crash drill 8/8 |
| 6 | the critic asked to settle a conversation the two models disagreed on, and the planner flagged it with a plain tool call, around the hold | disputed conversations are a finished outcome ("Needs your check"); code refuses any write to them | drill |
| 6 | after `--reject`, the answer waited on the declined write task and could never run | tasks that wait on a failed or cancelled task are cancelled and planned around | reject drill ends done, 0 writes |
| 6 | `report.md` crashed on a run waiting for approval | icon added | drill |
| 7 | the planner added `remember_fact` with no arguments, "after" the recall it wanted to see, three times | the rejection says to add a task once its arguments are known | next run: no rejection (7 calls instead of 10) |
| 7 | remember-suryodaya saved "requires an 8D report within 7 days" as a **fact** (the check accepts only instruction or preference) | the skill and the tool say anything a party wants from us is a preference or an instruction | 2 reruns: instruction |
| 7 | **refuse-price-not-agreed-suryodaya** was answered instead of refused: the planner added the answer in the same round as the search, so it never saw the result | an answer added in the same round as its goal's new work is dropped; the ending is decided once the result is in | both price refusals approve |
| 7 | artifact files were numbered `art-001…`, and a resumed run would start again at 001 and overwrite | artifacts are named by their content | — |
| 8 | the watcher's second poll raised 56 old changes (this morning's undo) | the first poll marks what the next poll re-reads as seen | a fresh watcher: first poll sets the cursor, second says "no change" |
| 8 | a watcher run limited to one conversation answered "checked 20 conversations" | the judging step's whole view of the mailbox follows the run's limit | "checked 1 conversation" |
| 9 | hybrid search added a fixed top 10 by meaning: half of Suryodaya's mailbox, and triage then listed 3 off-topic conversations | only candidates within 0.04 of the best score (at most 10) | every expected conversation kept, 0–0.6 wrong extras per query |
| 10 | trace spans left open at a crash were overwritten by the retry after a resume | at each resume, open spans end at the last event before the stop, marked unfinished | all 154 runs on disk trace correctly |
| 10 | a write task parked for approval and then declined got two spans | the decline closes the same span | same check |
| 17 (tests) | two `recall_memory` tasks in one round, or `remember_fact` next to `recall_memory`, failed one node: "cannot start a transaction within a transaction" (one SQLite connection, two threads) | every transaction holds its connection's lock (`common/sqlite_db.py` `LockedConnection`) | `test_concurrency.py`, 25 runs in a row |
| 17 (tests) | a write's update of the local copy, while a sync rebuilt in a thread, failed and cancelled the sibling writes | the same lock; a failed local update after a completed write is logged, never raised | `test_concurrency.py` |
| 17 (tests) | a node that timed out during the first sync left its worker thread in a transaction; the next node's sync collided with it | one shielded sync per run, shared by every node; the judging step's time limit covers a cold sync | `test_concurrency.py` |
| 17 (tests) | one failed poll ended the watcher process | `Watcher.poll_safely`: the failure is reported, the next poll tries again | `test_concurrency.py` |
| 17 (load test) | past ~1,450 candidates the shards used up the node and model-call budgets: "planner failed", every goal open | the fan-out is sized to the budgets left; the rest is reported as `not_judged`; a planner out of model calls ends as `max_steps` | `test_scale.py` sort of 10,000; `test_large_inbox.py` |
| 17 (load test) | only the first 40 write-causing verdicts were re-checked by the second model, yet all were written | every one is checked; any left unchecked (budget) is held | `test_large_inbox.py` (60 conversations) |
| 17 (load test) | per shard and per verdict, flows re-read the whole mailbox (a sort of 10,000 re-read it 10,000 times) | reads limited by id in SQL; the thread map built once per flow | sort of 10,000: 25.1 s → 5.6 s |
| 17 (load test) | rebuilding facts deleted full-text rows by an unindexable column: quadratic in mailbox size | full-text rows mapped by number (`text_rows`); facts committed 200 conversations at a time | 2,000 conversations: 0.64 s → 0.24 s |
| 17 (tests) | a full sync could delete a live row that changed during the pass (offset paging shifts) | rows not seen are read again before deleting | `test_concurrency.py` |
| 17 (tests) | follow-up dedup and reconcile read only the first 1,000 rows: a duplicate reminder past that | every page is read | `test_concurrency.py` |
| 17 (tests) | runs started together by the watcher could spend past the daily model-call ceiling (each admitted before any had spent) | each run reserves its share at admission and gets it as its budget; the rest is given back | `test_concurrency.py`, `test_watcher.py` |
| 17 (tests) | a REST write after the token expired failed with 401 | log in again once, then repeat the call | `test_concurrency.py` |
| 17 (load test) | the watcher's first poll stores the last 10 minutes as history, and that history counted toward the flood limit: the first real mail was refused | history is dated when it happened, so it never counts as an arrival | `test_watcher.py`, `test_scale.py` |

## 3. Still open

| What | Why it is open | Where it is tracked |
|---|---|---|
| Price verdicts on the Keystone "PO … order confirmation" conversations change between runs ("not a price" vs "agreed") | the validator holds the disputed ones (9 of 15), but which is right needs your price answer key | open |
| A replayed planner round after a resume charges the planner-round budget once more | the reply itself is reused for free; the round counter is not | open |
| Hybrid search can still add one off-topic conversation, and the planner searches with a single word | hybrid search is off by default until your confirmed queries say otherwise | open |
| The follow-ups answer says "not reported" for the conversations that are not waiting on anyone | wording only | open |
| The first meaning-based (hybrid) embedding of a 50,000-message mailbox takes about 9 hours at 90 texts a minute, and a node time limit throws its progress away | hybrid search is off by default; the fix (save each batch, embed in the background) waits until hybrid becomes the default | open |
| The MCP session keeps the token it opened with; a session that outlives the token fails its calls | REST logs in again on 401 (fixed); for MCP the token lifetime has not been measured live | open |
| Two inbound messages on one conversation in one watcher poll start two runs on the same conversation | each run has its own row locks; today's subscription is a dry run | open |
| The executor starts no new node while the planner is thinking | throughput only; nothing is lost | open |
| Our live checks (live harness, live drill, live watcher check) | they write to the shared books, so they are yours to run | open |

## 4. Every `WORKAROUND` tag in the code

A workaround for a platform bug is tagged in the code as `WORKAROUND(BUG-…)` so it can be removed once the platform
is fixed.

| Tag | File | What it works around |
|---|---|---|
| `WORKAROUND(BUG-001)` | `email_agent/contracts/platform.py` | text fields arriving in three shapes |
| `WORKAROUND(BUG-008)` | `email_agent/contracts/platform.py` | booleans as 0/1, counts as decimals, times without a zone |
| `WORKAROUND(BUG-022)` | `email_agent/mailbox/conversation.py` | word-for-word mirror copies in the pre-loaded Suryodaya mail |
