"""Run-local artifact store (the S7 idea): a tool result too large for the model's context is
saved here, and the model gets a preview plus the artifact id instead.

The id is made from the content (`art-<sha256[:12]>`, Stage 7), so the same result is saved once and a resumed run
never overwrites an earlier artifact (a counter would start again at 001 after a resume)."""

from __future__ import annotations

import hashlib
from pathlib import Path


class Artifacts:
    def __init__(self, run_dir: Path):
        self.dir = run_dir / "artifacts"

    def put(self, text: str, source: str) -> str:
        self.dir.mkdir(parents=True, exist_ok=True)
        art_id = f"art-{hashlib.sha256(text.encode()).hexdigest()[:12]}"
        path = self.dir / f"{art_id}.txt"
        if not path.exists():
            path.write_text(f"# source: {source}\n{text}")
        return art_id

    def get(self, art_id: str) -> str:
        return (self.dir / f"{art_id}.txt").read_text()
