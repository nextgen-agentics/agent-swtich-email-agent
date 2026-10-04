"""Run every task and save each run to disk. No scoring here.

    uv run python -m harness.run [--tasks harness/tasks.yaml] [--only <task-id>] [--instance keystone]
                                 [--provider openai --model zai-org/GLM-5.3-Flash] [--dry-run]
                                 [--quiet | --verbose]

Writes harness_runs/<batch>/<task-id>.json (SavedRun: the task, the run id, the database flags
seen just before the run, how the run stopped) and the agent's own run logs (with report.md)
under harness_runs/<batch>/runs/. A crashed or interrupted run is still saved, with its run folder.
Then score it:  uv run python -m harness.score harness_runs/<batch>
"""

from __future__ import annotations

import argparse
import asyncio
from datetime import date, datetime, timezone
from pathlib import Path

import yaml
from rich.console import Console

from email_agent.agent import run as run_agent
from email_agent.config import INSTANCES, PROJECT_ROOT, get_settings, with_model
from email_agent.llm import plan_route, route_text
from email_agent.console import ConsoleView, setup_logging
from email_agent.errors import describe
from email_agent.jsonio import dump_json
from harness.contracts import SavedRun, TaskFile
from harness.db import server_clock_and_me, thread_flags

BATCHES = PROJECT_ROOT / "harness_runs"


async def run_all(tasks_path: Path, only: str | None, dry_run: bool = False, instance: str | None = None,
                  provider: str | None = None, model: str | None = None, console: Console | None = None,
                  verbose: bool = False, fallback: bool | None = None) -> Path:
    """`console`: print each task's steps live (None = quiet)."""
    settings = get_settings()
    settings = with_model(settings, provider, model, fallback)   # compare models without touching .env
    tasks = TaskFile.model_validate(yaml.safe_load(tasks_path.read_text())).tasks
    batch = BATCHES / f"{datetime.now(timezone.utc):%Y%m%dT%H%M%SZ}"
    batch.mkdir(parents=True)
    for task in tasks:
        if (only and task.id not in only.split(",")) or (instance and task.instance != instance):
            continue
        today = task.as_of or date.today()
        saved = SavedRun(task=task, today=today, provider=settings.provider,
                         model=next((m for _, m, _ in plan_route(settings)), None),
                         route=route_text(settings), dry_run=dry_run)
        view = ConsoleView(console, verbose=verbose, prefix=f"{task.id} ") if console else None
        try:
            saved.before = await thread_flags(settings, task.instance)
            saved.started_at_server, saved.me_id = await server_clock_and_me(settings, task.instance)
            outcome = await run_agent(task.prompt, task.instance, settings, today=today, runs_dir=batch / "runs",
                                      dry_run=dry_run, mailboxes=task.mailboxes, view=view)
            saved.run_id, saved.run_dir, saved.stopped = outcome.run_id, outcome.run_dir, outcome.final.stopped
            saved.served_by = outcome.served_by
            err = outcome.final.error
            if outcome.final.stopped == "crashed" and err:   # the agent caught it and saved its files
                saved.error = f"crashed in {err.where} at step {err.iter}: {err.type}: {err.message}"[:2000]
            print(f"{task.id} [{', '.join(outcome.served_by) or 'no LLM answer'}]: {outcome.final.stopped}, "
                  f"{len(outcome.writes)} writes → {outcome.run_dir}/report.md")
        except Exception as e:                      # failed before the agent ran (e.g. reading the flags)
            saved.error = describe(e)
            print(f"{task.id}: CRASHED {saved.error}")
        except BaseException:                       # Ctrl-C: the agent wrote its own files; save ours, then stop
            saved.stopped, saved.error = "interrupted", "interrupted (Ctrl-C) before the run finished"
            raise
        finally:                                    # saved before any scoring, whatever happened
            dump_json(batch / f"{task.id}.json", saved)
    return batch


def main() -> None:
    ap = argparse.ArgumentParser(description="Run the harness tasks (no scoring)")
    ap.add_argument("--tasks", type=Path, default=PROJECT_ROOT / "harness" / "tasks.yaml")
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
    print(f"saved {batch.relative_to(PROJECT_ROOT)} — score it with: uv run python -m harness.score {batch.relative_to(PROJECT_ROOT)}")


if __name__ == "__main__":
    main()
