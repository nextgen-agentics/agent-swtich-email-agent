"""Reading what a run left in its folder, the way an operator would after the fact."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from email_agent.contracts.agent import FinalAnswer, WriteRecord
from email_agent.contracts.graph import JournalEvent, NodeRecord
from email_agent.contracts.mirror import SyncReport
from email_agent.graph.store import RUN_FILE, RunStore


def only_run(runs_dir: Path) -> Path:
    found = [p for p in runs_dir.iterdir() if (p / "request.json").exists()]
    assert len(found) == 1, found
    return found[0]


def writes(run_dir: str | Path) -> list[WriteRecord]:
    path = Path(run_dir) / "writes.jsonl"
    if not path.exists():
        return []
    return [WriteRecord.model_validate_json(x) for x in path.read_text().splitlines() if x.strip()]


def final(run_dir: str | Path) -> FinalAnswer:
    return FinalAnswer.model_validate_json((Path(run_dir) / "final.json").read_text())


def journal(run_dir: str | Path) -> list[JournalEvent]:
    store = RunStore(Path(run_dir) / RUN_FILE)
    try:
        return store.events()
    finally:
        store.close()


def nodes(run_dir: str | Path) -> dict[str, NodeRecord]:
    store = RunStore(Path(run_dir) / RUN_FILE)
    try:
        return dict(store.snapshot().nodes)
    finally:
        store.close()


def kinds(run_dir: str | Path) -> list[str]:
    return [e.kind for e in journal(run_dir)]


def budget_spent(run_dir: str | Path) -> dict[str, float]:
    store = RunStore(Path(run_dir) / RUN_FILE)
    try:
        return {b.name: b.spent for b in store.budgets()}
    finally:
        store.close()


def spans(run_dir: str | Path) -> list[dict[str, Any]]:
    return [json.loads(x) for x in (Path(run_dir) / "spans.jsonl").read_text().splitlines() if x.strip()]


def syncs(run_dir: str | Path) -> list[SyncReport]:
    lines = [json.loads(x) for x in (Path(run_dir) / "steps.jsonl").read_text().splitlines() if x.strip()]
    return [SyncReport.model_validate(x["report"]) for x in lines if x.get("kind") == "sync"]
