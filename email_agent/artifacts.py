"""Run-local artifact store (the S7 idea): a tool result too large for the model's context is
saved here, and the model gets a preview plus the artifact id instead."""

from __future__ import annotations

from pathlib import Path


class Artifacts:
    def __init__(self, run_dir: Path):
        self.dir = run_dir / "artifacts"
        self._n = 0

    def put(self, text: str, source: str) -> str:
        self.dir.mkdir(parents=True, exist_ok=True)
        self._n += 1
        art_id = f"art-{self._n:03d}"
        (self.dir / f"{art_id}.txt").write_text(f"# source: {source}\n{text}")
        return art_id

    def get(self, art_id: str) -> str:
        return (self.dir / f"{art_id}.txt").read_text()
