"""A web page for one scored harness batch: harness_runs/<batch>/view.html (Revision 15).

A table of the batch's tasks (verdict, why, how the run ended, who answered), each linking to its run's own page
(runs/<run_id>/view.html, written here when it is missing). harness.score writes it after report.md; to (re)make it:

    uv run python -m harness.view harness_runs/<batch> [--open]
"""

from __future__ import annotations

import argparse
import webbrowser
from datetime import datetime, timezone
from pathlib import Path

from email_agent.contracts.view import BatchRow, BatchView
from email_agent.record.run_view import PAGE, page_at_run_end, render_page
from harness.contracts import ScoreReport


def build_batch_view(batch: Path, report: ScoreReport) -> BatchView:
    rows = []
    for v in report.verdicts:
        run_dir = batch / "runs" / v.run_id if v.run_id else None
        if run_dir is not None and run_dir.is_dir() and not (run_dir / PAGE).exists():
            page_at_run_end(run_dir)
        has_page = run_dir is not None and (run_dir / PAGE).exists()
        rows.append(BatchRow(task_id=v.task_id, instance=v.instance, status=v.status, reason=v.reason,
                             run_stopped=v.run_stopped, served_by=v.served_by, run_id=v.run_id,
                             page=f"runs/{v.run_id}/{PAGE}" if has_page else None,
                             checks=[c.model_dump(mode="json") for c in v.checks]))
    return BatchView(generated_at=datetime.now(timezone.utc), batch=report.batch, scored_at=report.scored_at,
                     counts=report.counts, rows=rows)


def write_batch_view(batch: Path, report: ScoreReport) -> Path:
    path = batch / PAGE
    path.write_text(render_page(build_batch_view(batch, report), f"Harness batch {report.batch}"))
    return path


def main() -> None:
    ap = argparse.ArgumentParser(description="Write view.html for a scored harness batch (and its runs' pages)")
    ap.add_argument("batch", type=Path)
    ap.add_argument("--open", action="store_true", help="open the page in the browser")
    args = ap.parse_args()
    report = ScoreReport.model_validate_json((args.batch / "report.json").read_text())
    path = write_batch_view(args.batch, report)
    print(path.resolve().as_uri())
    if args.open:
        webbrowser.open(path.resolve().as_uri())


if __name__ == "__main__":
    main()
