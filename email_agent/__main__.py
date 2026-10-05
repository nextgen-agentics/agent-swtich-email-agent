"""Run the agent once.

    uv run python -m email_agent "What needs my reply today?" --instance suryodaya [--as-of 2026-09-29]
    uv run python -m email_agent "What needs my reply today?" --instance keystone --provider openai --dry-run
    uv run python -m email_agent "Flag what needs my reply" --approve-writes      # stops before writing; then:
    uv run python -m email_agent --resume runs/<run_id> --approve                 # (or --reject)
    uv run python -m email_agent --resume runs/<run_id>                           # continue a crashed/interrupted run

Progress goes to stderr, one line per step (--quiet: none; --verbose: arguments, results, memory, prompt
sizes). The answer and its status lines go to stdout. Exit code 1 when the run crashed.
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from datetime import date
from pathlib import Path

from rich.console import Console

from email_agent.agent import Decision, resume, run
from email_agent.config import get_settings, with_model
from email_agent.record.console import ConsoleView, setup_logging


def main() -> None:
    ap = argparse.ArgumentParser(description="Team 10 email agent")
    ap.add_argument("request", nargs="?", help="what to do (not with --resume)")
    ap.add_argument("--resume", metavar="RUN", help="continue a stopped run: its folder (runs/<run_id>) or run id; "
                    "it keeps its own request and options")
    answer = ap.add_mutually_exclusive_group()
    answer.add_argument("--approve", action="store_true", help="with --resume: send the writes waiting for approval")
    answer.add_argument("--reject", action="store_true", help="with --resume: decline them (nothing is written)")
    ap.add_argument("--approve-writes", action="store_true",
                    help="stop before any platform write and wait for --resume … --approve (a dry run stops "
                         "before recording what it would write)")
    ap.add_argument("--instance", default="suryodaya")
    ap.add_argument("--as-of", type=date.fromisoformat, default=None, help="treat this date as today")
    ap.add_argument("--dry-run", action="store_true", help="do everything except change platform data")
    ap.add_argument("--mailbox", action="append", help="address to work in (repeatable); default: the instance's")
    ap.add_argument("--provider", choices=["gemini", "openai"], help="which provider goes first (overrides PROVIDER)")
    ap.add_argument("--model", help="the first provider's first model (overrides MODEL)")
    ap.add_argument("--no-fallback", action="store_true", help="use only the first option of the route")
    ap.add_argument("--no-cache", action="store_true",
                    help="judge every conversation again (by default unchanged conversations reuse saved verdicts)")
    ap.add_argument("--no-history", action="store_true",
                    help="do not show the planner the last runs on these mailboxes (memory.sqlite episodes)")
    ap.add_argument("--search", choices=["fts", "hybrid"],
                    help="hybrid = meaning-based search with full text (Gemini embeddings + FAISS); default: SEARCH "
                         "setting (fts)")
    ap.add_argument("--full-sync", action="store_true",
                    help="re-read the whole mailbox into the local copy (state/<instance>/mailbox.sqlite), not only changes")
    shown = ap.add_mutually_exclusive_group()
    shown.add_argument("--quiet", action="store_true", help="no progress lines, only the answer")
    shown.add_argument("--verbose", action="store_true", help="also tool arguments, results, memory and prompt sizes")
    args = ap.parse_args()
    if bool(args.request) == bool(args.resume):
        ap.error("give a request, or --resume RUN (not both)")
    if (args.approve or args.reject) and not args.resume:
        ap.error("--approve / --reject answer a waiting run: use them with --resume RUN")
    settings = get_settings()
    settings = with_model(settings, args.provider, args.model, fallback=False if args.no_fallback else None)
    console = Console(stderr=True, highlight=False)
    setup_logging(console, verbose=args.verbose, quiet=args.quiet)
    view = None if args.quiet else ConsoleView(console, verbose=args.verbose)
    try:
        if args.resume:
            decision: Decision | None = "approve" if args.approve else "reject" if args.reject else None
            outcome = asyncio.run(resume(args.resume, settings, view=view, decision=decision))
        else:
            outcome = asyncio.run(run(args.request, args.instance, settings, today=args.as_of,
                                      dry_run=args.dry_run, mailboxes=args.mailbox, view=view,
                                      full_sync=args.full_sync, cache=not args.no_cache,
                                      approve_writes=args.approve_writes, history=not args.no_history,
                                      search=args.search))
    except KeyboardInterrupt as e:   # asyncio turns Ctrl-C into a cancel inside the run, which writes its files first
        notes = " ".join(getattr(e, "__notes__", [])) or "The run's files were written: see the newest folder in runs/."
        print(f"\n— interrupted (Ctrl-C). {notes}", file=sys.stderr)
        sys.exit(130)
    f = outcome.final
    print(f.answer)
    print(f"\n— stopped: {f.stopped} · iterations: {outcome.iterations} · writes: {len(outcome.writes)} · "
          f"tokens in/out: {outcome.usage.input_tokens}/{outcome.usage.output_tokens}")
    if f.error and f.stopped == "crashed":
        print(f"— crashed in {f.error.where} at step {f.error.iter}: {f.error.type}: {f.error.message}")
    served = ", ".join(f"{k} ×{n}" for k, n in outcome.served_by.items()) or "no LLM call answered"
    print(f"— served by: {served} · run log: {outcome.run_dir} · report: {outcome.run_dir}/report.md")
    page = Path(outcome.run_dir) / "view.html"
    if page.exists():
        print(f"— page: {page.resolve().as_uri()}  (open it in a browser: the graph, every model call, the writes)")
    if f.stopped == "waiting" and f.reason:
        print(f"— {f.reason}")
    if f.stopped == "crashed":
        sys.exit(1)


if __name__ == "__main__":
    main()
