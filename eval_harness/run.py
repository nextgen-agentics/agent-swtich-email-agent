"""Run every task and save each run to disk. No scoring here.

    uv run python -m eval_harness.run [--tasks eval_harness/tasks.yaml] [--only <task-id>] [--instance keystone]
                                 [--provider openai --model zai-org/GLM-5.3-Flash] [--dry-run]
                                 [--quiet | --verbose]

Writes eval_runs/<batch>/<task-id>.json (SavedRun: the task, the run id, the database flags
seen just before the run, how the run stopped) and the agent's own run logs (with report.md)
under eval_runs/<batch>/runs/. A crashed or interrupted run is still saved, with its run folder.
Then score it:  uv run python -m eval_harness.score eval_runs/<batch>
"""

from __future__ import annotations

import argparse
import asyncio
from datetime import date, datetime, timezone
from pathlib import Path

import yaml
from rich.console import Console

from email_agent.agent import run as run_agent
from email_agent.common.errors import describe
from email_agent.config import INSTANCES, PROJECT_ROOT, Settings, get_settings, with_model
from email_agent.llm.route import Llm, plan_route, route_text
from email_agent.record.console import ConsoleView, setup_logging
from email_agent.record.jsonio import dump_json
from eval_harness.contracts import SavedRun, Task, TaskFile
from eval_harness.db import server_clock_and_me, thread_flags

BATCHES = PROJECT_ROOT / "eval_runs"
TASKS = PROJECT_ROOT / "eval_harness" / "tasks.yaml"


def load_tasks(tasks_path: Path = TASKS) -> list[Task]:
    return TaskFile.model_validate(yaml.safe_load(tasks_path.read_text())).tasks


def new_batch() -> Path:
    batch = BATCHES / f"{datetime.now(timezone.utc):%Y%m%dT%H%M%SZ}"
    batch.mkdir(parents=True)
    return batch


async def run_task(task: Task, settings: Settings, batch: Path, *, dry_run: bool = False, llm: Llm | None = None,
                   view: ConsoleView | None = None, time_limit_s: float | None = None) -> SavedRun:
    """Run one task and save it as <batch>/<task-id>.json, whatever happens. `time_limit_s`: stop the agent run after
    this long (the official run's time guard); it is then saved as interrupted. `llm`: a given model (tests)."""
    today = task.as_of or date.today()
    saved = SavedRun(task=task, today=today, provider=settings.provider,
                     model=llm.model if llm else next((m for _, m, _ in plan_route(settings)), None),
                     route=f"{llm.model} (given)" if llm else route_text(settings), dry_run=dry_run)
    try:
        saved.before = await thread_flags(settings, task.instance)
        saved.started_at_server, saved.me_id = await server_clock_and_me(settings, task.instance)
        async with asyncio.timeout(time_limit_s):
            outcome = await run_agent(task.prompt, task.instance, settings, today=today, llm=llm,
                                      runs_dir=batch / "runs", dry_run=dry_run, mailboxes=task.mailboxes, view=view)
        saved.run_id, saved.run_dir, saved.stopped = outcome.run_id, outcome.run_dir, outcome.final.stopped
        saved.served_by = outcome.served_by
        err = outcome.final.error
        if outcome.final.stopped == "crashed" and err:   # the agent caught it and saved its files
            saved.error = f"crashed in {err.where} at step {err.iter}: {err.type}: {err.message}"[:2000]
        print(f"{task.id} [{', '.join(outcome.served_by) or 'no LLM answer'}]: {outcome.final.stopped}, "
              f"{len(outcome.writes)} writes → {outcome.run_dir}/report.md")
    except TimeoutError as e:                       # the time guard stopped the agent run (it saved its own files)
        if time_limit_s is None:
            saved.error = describe(e)
        else:
            saved.stopped, saved.error = "interrupted", f"stopped by the time limit ({time_limit_s:.0f} s)"
        print(f"{task.id}: STOPPED {saved.error}")
    except Exception as e:                          # failed before the agent ran (e.g. reading the flags)
        saved.error = describe(e)
        print(f"{task.id}: CRASHED {saved.error}")
    except BaseException:                           # Ctrl-C: the agent wrote its own files; save ours, then stop
        saved.stopped, saved.error = "interrupted", "interrupted (Ctrl-C) before the run finished"
        raise
    finally:                                        # saved before any scoring, whatever happened
        dump_json(batch / f"{task.id}.json", saved)
    return saved


async def run_all(tasks_path: Path, only: str | None, dry_run: bool = False, instance: str | None = None,
                  provider: str | None = None, model: str | None = None, console: Console | None = None,
                  verbose: bool = False, fallback: bool | None = None) -> Path:
    """`console`: print each task's steps live (None = quiet)."""
    settings = get_settings()
    settings = with_model(settings, provider, model, fallback)   # compare models without touching .env
    batch = new_batch()
    for task in load_tasks(tasks_path):
        if (only and task.id not in only.split(",")) or (instance and task.instance != instance):
            continue
        view = ConsoleView(console, verbose=verbose, prefix=f"{task.id} ") if console else None
        await run_task(task, settings, batch, dry_run=dry_run, view=view)
    return batch


def main() -> None:
    ap = argparse.ArgumentParser(description="Run the test-suite tasks (no scoring)")
    ap.add_argument("--tasks", type=Path, default=TASKS)
    ap.add_argument("--only", help="run just these task ids (comma-separated)")
    ap.add_argument("--dry-run", action="store_true", help="agent changes nothing (scores unevaluated)")
    ap.add_argument("--instance", choices=sorted(INSTANCES), help="run only this instance's tasks")
    ap.add_argument("--provider", choices=["gemini", "openai"], help="which provider goes first, for this batch")
    ap.add_argument("--model", help="the first provider's first model, for this batch")
    ap.add_argument("--no-fallback", action="store_true", help="use only the first option of the route (bake-offs)")
    shown = ap.add_mutually_exclusive_group()
    shown.add_argument("--quiet", action="store_true", help="no progress lines, only one line per task")
    shown.add_argument("--verbose", action="store_true", help="also tool arguments, results, memory and prompt sizes")
    args = ap.parse_args()
    console = Console(stderr=True, highlight=False)
    setup_logging(console, verbose=args.verbose, quiet=args.quiet)
    batch = asyncio.run(run_all(args.tasks, args.only, args.dry_run, args.instance, args.provider, args.model,
                                console=None if args.quiet else console, verbose=args.verbose,
                                fallback=False if args.no_fallback else None))
    print(f"saved {batch.relative_to(PROJECT_ROOT)} — score it with: uv run python -m eval_harness.score {batch.relative_to(PROJECT_ROOT)}")


if __name__ == "__main__":
    main()
