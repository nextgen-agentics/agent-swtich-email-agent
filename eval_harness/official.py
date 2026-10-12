"""The official run: the command the AgentSwitch runner calls ("Our harness → Submit for a run", Revisions 18–19).

    uv run python -m eval_harness.official                     # connection checks, then our tasks for the instance
    uv run python -m eval_harness.official --ready-only        # … only the tasks whose answer keys are decided
    uv run python -m eval_harness.official --check             # connection checks only (no agent run, nothing written)
    uv run python -m eval_harness.official --only id1,id2      # only these tasks of the instance
    uv run python -m eval_harness.official --list              # which tasks would run, and why others are left out
    uv run python -m eval_harness.official --validate results.json

The AgentSwitch runner (agentswitch-harness.toml) runs this once per instance, against a fresh copy of that instance,
with six variables and nothing else: AGENTSWITCH_BASE_URL, AGENTSWITCH_TOKEN, AGENTSWITCH_INSTANCE, OPENAI_BASE_URL,
OPENAI_API_KEY, OPENAI_MODEL. It then reads results.json. What this does:

1. Two connection checks: the platform (who we are, by REST, and every MCP tool our skills need, with the seat token)
   and the model (one schema-bound JSON reply from OPENAI_MODEL).
2. Every task of eval_harness/tasks.yaml for AGENTSWITCH_INSTANCE, in file order (with --ready-only, only those whose
   answer keys are decided), each scored right after its run with our own checks (score.py): approve → passed;
   revise and unevaluated → not passed.
3. results.json is rewritten after every row, so a run stopped at the time limit still leaves a valid file. Each task
   has its own time cap (default 8 minutes, `max_minutes:` in tasks.yaml) inside the overall budget (27 minutes); a
   task that no longer fits is written as not run. Anything unexpected becomes a `runner-crash` row, and the last row
   (`result-contract`) checks the rows themselves. The exit code is always 0: the runner scores results.json.

It reads no .env, and uses only the runner's model (provider "openai", no fallback, full-text search), so a local
rehearsal behaves like the official run. The seat token, the model key and both base URLs never appear in a row or in
the printed output. Locally the books are the live shared ones, not fresh copies: take the writes back with
`scripts/agent/undo_run.py eval_runs/<batch>`. How to run and submit: docs/project/official-runs.md.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlparse

from pydantic import ValidationError

from email_agent.common.errors import describe
from email_agent.common.skill_registry import SkillRegistry
from email_agent.config import INSTANCES, PROJECT_ROOT, Settings
from email_agent.contracts.llm import ChatMessage, LlmRequest
from email_agent.graph.capabilities import GRAPH_CAPABILITIES
from email_agent.llm.route import Llm, make_llm
from email_agent.platform.action import LOCAL_TOOLS
from email_agent.platform.mcp_session import McpSession
from email_agent.platform.rest import RestClient
from email_agent.record.jsonio import dump_json
from eval_harness.contracts import ResultsFile, ResultTask, ScoreReport, Task, Verdict
from eval_harness.db import snapshot
from eval_harness.predicates import answer_key_gap
from eval_harness.run import load_tasks, new_batch, run_task
from eval_harness.score import render, verdict_of
from eval_harness.view import write_batch_view

BUDGET_MIN = 27.0           # of the runner's 30-minute limit: the rest is for scoring and writing the last rows
TASK_MAX_MIN = 8.0          # one task's cap unless tasks.yaml says otherwise (live runs of the ready tasks: < 4 min)
MIN_TASK_S = 60.0           # a task is started only with at least this much time left
SCORE_RESERVE_S = 45.0      # kept back from a task's run for reading the database and scoring it
CHECK_TIMEOUT_S = 120.0     # each connection check
MODEL_CHECK_SCHEMA = {"title": "ModelCheck", "type": "object", "properties": {"ok": {"type": "boolean"}},
                      "required": ["ok"]}


def missing_settings(settings: Settings) -> list[str]:
    """The runner's variables this run lacks (names only, never values). OPENAI_BASE_URL has a default (W&B)."""
    wanted = {"AGENTSWITCH_BASE_URL": settings.agentswitch_base_url, "AGENTSWITCH_TOKEN": settings.agentswitch_token,
              "AGENTSWITCH_INSTANCE": settings.agentswitch_instance, "OPENAI_API_KEY": settings.openai_api_key,
              "OPENAI_MODEL": settings.openai_model}
    return [name for name, value in wanted.items() if not value]


def official_settings(settings: Settings) -> Settings:
    """Only the runner's model: no Gemini, no W&B key or project, no fallback, full-text search."""
    return settings.model_copy(update={"provider": "openai", "model": None, "fallback": False, "search": "fts",
                                       "wandb_api_key": None, "wandb_project": None})


class Scrubber:
    """Replaces the seat token, the model key and both base URLs (and their host names) with placeholders."""

    def __init__(self, settings: Settings | None):
        found: dict[str, str] = {}
        if settings is not None:
            token, key = settings.agentswitch_token, settings.openai_api_key
            for name, text in (("AGENTSWITCH_TOKEN", token.get_secret_value() if token else None),
                               ("OPENAI_API_KEY", key.get_secret_value() if key else None),
                               ("AGENTSWITCH_BASE_URL", settings.agentswitch_base_url),
                               ("OPENAI_BASE_URL", settings.openai_base_url)):
                if text and len(text) >= 6:
                    found[text.rstrip("/")] = f"<{name}>"
                    if name.endswith("_URL") and (netloc := urlparse(text).netloc):
                        found.setdefault(netloc, f"<{name}>")
        self.secrets = sorted(found.items(), key=lambda kv: -len(kv[0]))   # whole URLs before their host names

    def __call__(self, text: str) -> str:
        for value, placeholder in self.secrets:
            text = text.replace(value, placeholder)
        return text


class _ScrubLogs(logging.Filter):
    def __init__(self, scrub: Scrubber):
        super().__init__()
        self.scrub = scrub

    def filter(self, record: logging.LogRecord) -> bool:
        record.msg, record.args = self.scrub(record.getMessage()), None
        return True


class Results:
    """The rows so far; every add rewrites results.json (temp file, then rename: never half a file)."""

    def __init__(self, path: Path, instance: str, scrub: Scrubber | None = None):
        self.path, self.instance = path, instance
        self.scrub = scrub or Scrubber(None)
        self.rows: list[ResultTask] = []
        self.left_out = 0                              # tasks not run because their answer key is not decided

    def add(self, task_id: str, title: str, passed: bool, evidence: str, seconds: float | None = None) -> ResultTask:
        evidence = self.scrub(evidence) + (f" [{seconds:.1f} s]" if seconds is not None else "")
        if len(evidence) > 500:
            evidence = evidence[:480] + " … " + evidence[-15:]   # keep the seconds at the end
        row = ResultTask(id=f"{self.instance}:{task_id}", title=self.scrub(title)[:200], passed=passed,
                         score=1.0 if passed else 0.0, evidence=evidence)
        self.rows.append(row)
        self.write()
        print(f"[{'PASS' if passed else 'FAIL'}] {row.id}: {row.evidence[:200]}", flush=True)
        return row

    def summary(self) -> str:
        passed = sum(r.passed for r in self.rows)
        not_run = sum(r.evidence.startswith("not run") for r in self.rows)
        return (f"{self.instance}: {passed}/{len(self.rows)} passed (Team 10 Email agent; our checks read the "
                f"database)" + (f"; {not_run} not run" if not_run else "")
                + (f"; {self.left_out} left out until their answer keys are decided" if self.left_out else ""))

    def write(self) -> None:
        body = ResultsFile(tasks=self.rows, summary=self.summary()).model_dump_json(indent=2, exclude_none=True)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_name(self.path.name + ".tmp")
        tmp.write_text(body)
        os.replace(tmp, self.path)

    def contract_problems(self) -> list[str]:
        """What is wrong with the rows so far, counting the contract row about to be added."""
        problems = []
        if not 1 <= len(self.rows) + 1 <= 200:
            problems.append(f"{len(self.rows) + 1} rows (1 to 200 allowed)")
        ids = [r.id for r in self.rows]
        if len(set(ids)) != len(ids):
            problems.append("duplicate ids: " + ", ".join(sorted({i for i in ids if ids.count(i) > 1})))
        problems += [f"row {r.id} has no title or evidence" for r in self.rows if not r.title or not r.evidence]
        try:
            ResultsFile(tasks=self.rows or [ResultTask(id="x", title="x", passed=False)], summary=self.summary())
        except ValidationError as e:
            problems.append(f"format: {e.error_count()} error(s)")
        return problems


# ── connection checks ─────────────────────────────────────────────────────────

async def check_platform(settings: Settings, instance: str) -> tuple[bool, str]:
    """Who we are (REST /api/auth/me) and every MCP tool our skills need, with the seat token."""
    try:
        async with asyncio.timeout(CHECK_TIMEOUT_S):
            me = await asyncio.to_thread(RestClient(settings, instance).me)
            async with McpSession(settings, instance) as mcp:
                tools = {t.name for t in await mcp.list_tools()}
        skills = SkillRegistry()
        skills.validate_tools(tools, set(LOCAL_TOOLS) | GRAPH_CAPABILITIES)
    except Exception as e:  # noqa: BLE001 — reported as the check's evidence
        return False, describe(e)
    needed = len(skills.all_tools() - set(LOCAL_TOOLS) - GRAPH_CAPABILITIES)
    return True, f"signed in as {me.email} on {instance}; {len(tools)} MCP tools, all {needed} our skills need are there"


async def check_model(settings: Settings, llm: Llm | None) -> tuple[bool, str]:
    """One schema-bound JSON reply (every role of our agent answers this way)."""
    request = LlmRequest(purpose="answer", system="You check that a model answers in JSON. Reply with JSON only.",
                         messages=[ChatMessage(role="user", text='Is 2 + 2 equal to 4? Reply {"ok": true} or '
                                                                 '{"ok": false}.')],
                         response_schema=MODEL_CHECK_SCHEMA, temperature=0.0)
    try:
        async with asyncio.timeout(CHECK_TIMEOUT_S):
            reply = await (llm or make_llm(settings)).chat(request)
        ok = json.loads(reply.text).get("ok") is True
    except Exception as e:  # noqa: BLE001 — reported as the check's evidence
        return False, f"{settings.openai_model}: {describe(e)}"
    return ok, (f"{reply.model} answered {reply.text[:60]!r} ({reply.usage.input_tokens} in / "
                f"{reply.usage.output_tokens} out tokens)")


# ── tasks ─────────────────────────────────────────────────────────────────────

def title_of(task: Task) -> str:
    checks = ", ".join(p.name for p in task.predicates)
    return f"{task.prompt} [{checks}]"


def evidence_of(verdict: Verdict) -> str:
    """Our verdict and its reason, the first details, and the run (for finding it in eval_runs/ locally)."""
    parts = [f"{verdict.status}: {verdict.reason}"]
    parts += [f"{key}: {', '.join(ids[:5])}{' …' if len(ids) > 5 else ''}" for key, ids in verdict.details.items()]
    parts.append(f"run {verdict.run_id or '—'} ({verdict.run_stopped or 'no run'})")
    return " · ".join(parts)


def choose_tasks(instance: str, *, only: list[str] | None = None, ready_only: bool = False,
                 tasks: list[Task] | None = None) -> tuple[list[Task], list[tuple[Task, str]]]:
    """(the tasks to run, in file order; the tasks left out with the reason)."""
    mine = [t for t in (tasks if tasks is not None else load_tasks())
            if t.instance == instance and (not only or t.id in only)]
    if not ready_only:
        return mine, []
    gaps = [(t, answer_key_gap(t)) for t in mine]
    return [t for t, gap in gaps if gap is None], [(t, gap) for t, gap in gaps if gap is not None]


async def official_run(settings: Settings, results_path: Path, *, check_only: bool = False, only: list[str] | None = None,
                       ready_only: bool = False, budget_min: float = BUDGET_MIN, task_max_min: float = TASK_MAX_MIN,
                       llm: Llm | None = None, tasks: list[Task] | None = None) -> Results:
    """Never raises for a failure: each one becomes a row. `llm`, `tasks`: given ones (tests)."""
    results = Results(results_path, settings.agentswitch_instance or "unknown", Scrubber(settings))
    try:
        await _run(settings, results, check_only=check_only, only=only, ready_only=ready_only, budget_min=budget_min,
                   task_max_min=task_max_min, llm=llm, tasks=tasks)
    except Exception as e:  # noqa: BLE001 — every failure has to end up as a row
        results.add("runner-crash", "The official run finished without an unexpected error", False, describe(e))
    except BaseException as e:   # Ctrl-C or a cancelled task: say so in the file, then stop as asked
        results.add("runner-crash", "The official run finished without an unexpected error", False,
                    f"stopped: {type(e).__name__}")
        raise
    finally:
        problems = results.contract_problems()
        results.add("result-contract", "results.json rows are well-formed", not problems,
                    "; ".join(problems) or f"{len(results.rows)} earlier rows: unique ids, true/false, evidence present")
    return results


async def _run(settings: Settings, results: Results, *, check_only: bool, only: list[str] | None, ready_only: bool,
               budget_min: float, task_max_min: float, llm: Llm | None, tasks: list[Task] | None) -> None:
    deadline = time.monotonic() + budget_min * 60
    instance = results.instance
    missing = missing_settings(settings)
    if missing or instance not in INSTANCES:
        why = (f"missing {', '.join(missing)}" if missing else
               f"unknown instance {instance!r}; ours are {sorted(INSTANCES)}")
        results.add("setup", "The AgentSwitch runner's variables are all set", False, why)
        return
    settings = official_settings(settings)
    print(f"official run on {instance}: model {settings.openai_model}, budget {budget_min:.0f} min, "
          f"{task_max_min:.0f} min per task", flush=True)

    started = time.monotonic()
    platform_ok, evidence = await check_platform(settings, instance)
    results.add("platform", "MCP and REST work with the seat token; every tool our skills need is there",
                platform_ok, evidence, time.monotonic() - started)
    started = time.monotonic()
    model_ok, evidence = await check_model(settings, llm)
    results.add("model", "The runner's model answers in schema-bound JSON", model_ok, evidence,
                time.monotonic() - started)
    if check_only:
        return

    chosen, left_out = choose_tasks(instance, only=only, ready_only=ready_only, tasks=tasks)
    results.left_out = len(left_out)
    for task, gap in left_out:
        print(f"[LEFT OUT] {instance}:{task.id}: {gap}", flush=True)
    blocked = None if platform_ok and model_ok else \
        ("the platform check failed" if not platform_ok else "the model check failed")
    batch = new_batch()
    verdicts: list[Verdict] = []
    for task in chosen:
        left = deadline - time.monotonic()
        if blocked or left < MIN_TASK_S:
            results.add(task.id, title_of(task), False, f"not run: {blocked or 'time budget'}")
            continue
        started = time.monotonic()
        cap = min(left - SCORE_RESERVE_S, (task.max_minutes or task_max_min) * 60)
        saved = await run_task(task, settings, batch, llm=llm, time_limit_s=max(1.0, cap))
        try:
            async with asyncio.timeout(SCORE_RESERVE_S * 2):
                snap, snap_error = await snapshot(settings, instance), None
        except Exception as e:  # noqa: BLE001 — the verdict says it could not be scored
            snap, snap_error = None, describe(e)
        verdict = verdict_of(saved, snap, snap_error)
        verdicts.append(verdict)
        results.add(task.id, title_of(task), verdict.status == "approve", evidence_of(verdict),
                    time.monotonic() - started)
    if verdicts:
        _save_batch_report(batch, verdicts)


def _save_batch_report(batch: Path, verdicts: list[Verdict]) -> None:
    """The usual report.json, report.md and view.html in eval_runs/<batch>/ (for a local rehearsal)."""
    label = str(batch.relative_to(PROJECT_ROOT)) if batch.is_relative_to(PROJECT_ROOT) else str(batch)
    report = ScoreReport(batch=label, verdicts=verdicts)
    dump_json(batch / "report.json", report)
    (batch / "report.md").write_text(render(report))
    try:
        write_batch_view(batch, report)
    except Exception as e:  # noqa: BLE001 — the page is a convenience; results.json is what counts
        print(f"the batch page was not written: {describe(e)}", flush=True)


def validate(path: Path) -> int:
    try:
        found = ResultsFile.model_validate_json(path.read_text())
    except (OSError, ValidationError) as e:
        print(f"{path}: NOT valid\n{e}")
        return 1
    passed = sum(t.passed for t in found.tasks)
    print(f"{path}: valid — {len(found.tasks)} tasks, {passed} passed. {found.summary}")
    return 0


def list_tasks(instance: str | None, only: list[str] | None, ready_only: bool) -> int:
    if not instance:
        print("set AGENTSWITCH_INSTANCE (suryodaya or keystone) first")
        return 1
    chosen, left_out = choose_tasks(instance, only=only, ready_only=ready_only)
    for task in chosen:
        print(f"RUN       {task.id}  (cap {task.max_minutes or TASK_MAX_MIN:.0f} min)")
    for task, gap in left_out:
        print(f"LEFT OUT  {task.id}  — {gap}")
    print(f"{instance}: {len(chosen)} to run, {len(left_out)} left out")
    return 0


def main() -> None:
    ap = argparse.ArgumentParser(description="The official run: our tasks for AGENTSWITCH_INSTANCE → results.json")
    ap.add_argument("--results", type=Path, default=Path("results.json"),
                    help="where to write the results (default: results.json in the current folder)")
    ap.add_argument("--check", action="store_true", help="only the two connection checks (no agent run)")
    ap.add_argument("--only", help="only these task ids of the instance (comma-separated)")
    ap.add_argument("--ready-only", action="store_true",
                    help="leave out the tasks whose answer keys are missing or undecided")
    ap.add_argument("--budget-min", type=float, default=float(os.environ.get("OFFICIAL_BUDGET_MIN", BUDGET_MIN)),
                    help=f"stop starting tasks after this many minutes (default {BUDGET_MIN:.0f}, or OFFICIAL_BUDGET_MIN)")
    ap.add_argument("--task-max-min", type=float, default=TASK_MAX_MIN,
                    help=f"one task's time cap unless tasks.yaml sets max_minutes (default {TASK_MAX_MIN:.0f})")
    ap.add_argument("--list", action="store_true", help="only print which tasks would run, and why others are left out")
    ap.add_argument("--validate", type=Path, metavar="FILE", help="only check a results file against the format")
    args = ap.parse_args()
    only = args.only.split(",") if args.only else None
    if args.validate:
        sys.exit(validate(args.validate))
    if args.list:
        sys.exit(list_tasks(os.environ.get("AGENTSWITCH_INSTANCE"), only, args.ready_only))
    started = datetime.now(timezone.utc)
    try:
        settings: Settings | None = Settings(_env_file=None)    # the runner's variables only, never a local .env
    except ValidationError as e:
        settings = None
        results = Results(args.results, os.environ.get("AGENTSWITCH_INSTANCE") or "unknown")
        results.add("setup", "The AgentSwitch runner's variables are all set", False, f"settings refused: {e}"[:400])
    scrub = Scrubber(settings)
    handler = logging.StreamHandler()
    handler.addFilter(_ScrubLogs(scrub))
    logging.basicConfig(level=logging.WARNING, handlers=[handler], force=True)
    if settings is not None:
        results = asyncio.run(official_run(settings, args.results, check_only=args.check, only=only,
                                           ready_only=args.ready_only, budget_min=args.budget_min,
                                           task_max_min=args.task_max_min))
    took = (datetime.now(timezone.utc) - started).total_seconds() / 60
    print(f"{results.summary()} — {took:.1f} min — written to {args.results}", flush=True)
    # always exit 0: the AgentSwitch runner scores results.json, not the exit code


if __name__ == "__main__":
    main()
