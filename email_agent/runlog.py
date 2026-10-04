"""Everything a run does, written to disk as it happens: runs/<run_id>/

    request.json   RunRequest: the request and options (written first, before any network call)
    context.json   RunContext (who, where, today, our mailboxes)
    steps.jsonl    one typed step per line (llm, goals, decision, action, memory; error if it crashed)
    writes.jsonl   one WriteRecord per change made to platform data
    final.json     FinalAnswer
    outcome.json   RunOutcome (final + writes + token usage)
    report.md      the same, readable (run_report.py)
    artifacts/     large tool results (see artifacts.py)

final.json, outcome.json and report.md are written on every exit: done, crashed or interrupted.
`on_step` (optional) sees every step as it is written; the terminal view (console.py) uses it.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Callable

from pydantic import BaseModel

from email_agent.contracts.agent import RunContext, RunRequest, WriteRecord
from email_agent.contracts.runlog import LlmStep
from email_agent.jsonio import dump_json


class RunLog:
    def __init__(self, run_dir: Path, on_step: Callable[[BaseModel], None] | None = None):
        self.dir = run_dir
        self.dir.mkdir(parents=True, exist_ok=False)
        self.writes: list[WriteRecord] = []
        self.on_step = on_step

    def write(self, name: str, model: BaseModel) -> None:
        dump_json(self.dir / name, model)

    def step(self, step: BaseModel) -> None:
        with (self.dir / "steps.jsonl").open("a") as f:
            f.write(step.model_dump_json() + "\n")
        if self.on_step:
            try:
                self.on_step(step)
            except Exception:                        # a display problem must never stop the run
                logging.getLogger(__name__).exception("the terminal view failed on a %s step", type(step).__name__)

    def record_write(self, record: WriteRecord) -> None:
        self.writes.append(record)
        with (self.dir / "writes.jsonl").open("a") as f:
            f.write(record.model_dump_json() + "\n")

    def begin(self, request: RunRequest) -> None:
        self.write("request.json", request)
        (self.dir / "steps.jsonl").touch()
        (self.dir / "writes.jsonl").touch()

    def start(self, ctx: RunContext) -> None:
        self.write("context.json", ctx)
        (self.dir / "steps.jsonl").touch()
        (self.dir / "writes.jsonl").touch()

    @staticmethod
    def llm_steps(run_dir: Path) -> list[LlmStep]:
        out = []
        for line in (run_dir / "steps.jsonl").read_text().splitlines():
            if '"kind":"llm"' in line.replace(" ", ""):
                out.append(LlmStep.model_validate_json(line))
        return out
