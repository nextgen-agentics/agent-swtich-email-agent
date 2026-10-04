"""Run the agent once.

    uv run python -m email_agent "What needs my reply today?" --instance suryodaya [--as-of 2026-09-29]
    uv run python -m email_agent "What needs my reply today?" --instance keystone --provider openai --dry-run

Progress goes to stderr, one line per step (--quiet: none; --verbose: arguments, results, memory, prompt
sizes). The answer and its status lines go to stdout. Exit code 1 when the run crashed.
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from datetime import date

from rich.console import Console

from email_agent.agent import run
from email_agent.config import get_settings, with_model
from email_agent.console import ConsoleView, setup_logging


def main() -> None:
    ap = argparse.ArgumentParser(description="Team 10 email agent")
    ap.add_argument("request")
    ap.add_argument("--instance", default="suryodaya")
    ap.add_argument("--as-of", type=date.fromisoformat, default=None, help="treat this date as today")
    ap.add_argument("--dry-run", action="store_true", help="do everything except change platform data")
    ap.add_argument("--mailbox", action="append", help="address to work in (repeatable); default: the instance's")
    ap.add_argument("--provider", choices=["gemini", "openai"], help="which provider goes first (overrides PROVIDER)")
    ap.add_argument("--model", help="the first provider's first model (overrides MODEL)")
    ap.add_argument("--no-fallback", action="store_true", help="use only the first option of the route")
    shown = ap.add_mutually_exclusive_group()
    shown.add_argument("--quiet", action="store_true", help="no progress lines, only the answer")
    shown.add_argument("--verbose", action="store_true", help="also tool arguments, results, memory and prompt sizes")
    args = ap.parse_args()
    settings = get_settings()
    settings = with_model(settings, args.provider, args.model, fallback=False if args.no_fallback else None)
    console = Console(stderr=True, highlight=False)
    setup_logging(console, verbose=args.verbose, quiet=args.quiet)
    view = None if args.quiet else ConsoleView(console, verbose=args.verbose)
    try:
        outcome = asyncio.run(run(args.request, args.instance, settings, today=args.as_of,
                                  dry_run=args.dry_run, mailboxes=args.mailbox, view=view))
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
    if f.stopped == "crashed":
        sys.exit(1)


if __name__ == "__main__":
    main()
