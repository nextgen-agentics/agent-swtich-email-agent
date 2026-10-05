"""Score a saved batch: read the database now, run each task's predicate, write the report.

    uv run python -m harness.score harness_runs/<batch>

Writes <batch>/report.json (ScoreReport), <batch>/report.md and <batch>/view.html (the batch page, Revision 15).
Each task's checks run against one database snapshot per instance; the task's verdict is the worst of its checks.
A check that raises is recorded as `unevaluated` — never a pass.
"""

from __future__ import annotations

import argparse
import asyncio
from pathlib import Path

from email_agent.config import PROJECT_ROOT, get_settings
from email_agent.record.jsonio import dump_json
from harness.contracts import SavedRun, ScoreReport, Verdict
from harness.db import DbSnapshot, snapshot
from harness.predicates import check_all, worst
from harness.view import write_batch_view

ICON = {"approve": "✅", "revise": "❌", "unevaluated": "⚪"}


async def score(batch: Path) -> ScoreReport:
    settings = get_settings()
    verdicts: list[Verdict] = []
    snaps: dict[str, DbSnapshot] = {}
    for path in sorted(batch.glob("*.json")):
        if path.name == "report.json":
            continue
        saved = SavedRun.model_validate_json(path.read_text())
        base = {"task_id": saved.task.id, "instance": saved.task.instance, "run_id": saved.run_id,
                "run_stopped": saved.stopped or ("crashed" if saved.error else None),
                "served_by": ", ".join(f"{k} ×{n}" for k, n in saved.served_by.items()) or None}
        try:
            if saved.task.instance not in snaps:
                snaps[saved.task.instance] = await snapshot(settings, saved.task.instance)
            checks = check_all(saved, snaps[saved.task.instance])
        except Exception as e:
            verdicts.append(Verdict(**base, status="unevaluated", reason=f"scoring raised {type(e).__name__}: {e}"))
            continue
        status = worst(checks)
        reason = checks[0].reason if len(checks) == 1 else " · ".join(f"{c.name}: {c.status}" for c in checks)
        details = {f"{c.name} — {k}" if len(checks) > 1 else k: v for c in checks for k, v in c.details.items()}
        verdicts.append(Verdict(**base, status=status, reason=reason, details=details, checks=checks))
    return ScoreReport(batch=str(batch.relative_to(PROJECT_ROOT)), verdicts=verdicts)


def render(report: ScoreReport) -> str:
    c = report.counts
    lines = [f"# Harness report — {report.batch}", "",
             f"{c['approve']} approve · {c['revise']} revise · {c['unevaluated']} unevaluated "
             "(unevaluated is never a pass)", "",
             "| task | instance | verdict | reason | run stopped | served by | run |", "|---|---|---|---|---|---|---|"]
    for v in report.verdicts:
        lines.append(f"| {v.task_id} | {v.instance} | {ICON[v.status]} {v.status} | {v.reason} | "
                     f"{v.run_stopped or '—'} | {v.served_by or '—'} | {v.run_id or '—'} |")
    for v in report.verdicts:
        if len(v.checks) > 1:
            lines.append(f"\n**{v.task_id}** — " + "; ".join(f"{ICON[c.status]} {c.name}: {c.reason}" for c in v.checks))
        for key, ids in v.details.items():
            lines.append(f"\n**{v.task_id} — {key}:** " + ", ".join(ids))
    return "\n".join(lines) + "\n"


def main() -> None:
    ap = argparse.ArgumentParser(description="Score a saved harness batch")
    ap.add_argument("batch", type=Path)
    args = ap.parse_args()
    batch = args.batch if args.batch.is_absolute() else PROJECT_ROOT / args.batch
    report = asyncio.run(score(batch))
    dump_json(batch / "report.json", report)
    (batch / "report.md").write_text(render(report))
    page = write_batch_view(batch, report)
    print(render(report))
    print(f"Page: {page.resolve().as_uri()}")


if __name__ == "__main__":
    main()
