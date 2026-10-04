"""Load every email_agent/skills/<name>/SKILL.md and check it.

A skill names the tools it needs; nothing else is ever offered to the model for that skill's
goals. `validate_tools` fails at start-up if a skill names a tool the seat does not have.
"""

from __future__ import annotations

from pathlib import Path

import yaml
from pydantic import ValidationError

from email_agent.contracts.skills import SkillCatalogEntry, SkillSpec

SKILLS_DIR = Path(__file__).parent / "skills"


class SkillError(Exception):
    pass


def parse_skill(path: Path) -> SkillSpec:
    text = path.read_text()
    if not text.startswith("---"):
        raise SkillError(f"{path}: must start with a '---' YAML front-matter block")
    try:
        _, front, body = text.split("---", 2)
        meta = yaml.safe_load(front) or {}
        return SkillSpec.model_validate({**meta, "instructions": body.strip(), "path": str(path)})
    except (ValueError, yaml.YAMLError, ValidationError) as e:
        raise SkillError(f"{path}: {e}") from e


class SkillRegistry:
    def __init__(self, skills_dir: Path = SKILLS_DIR):
        self._skills: dict[str, SkillSpec] = {}
        for path in sorted(skills_dir.glob("*/SKILL.md")):
            spec = parse_skill(path)
            if spec.name != path.parent.name:
                raise SkillError(f"{path}: name {spec.name!r} must match its folder {path.parent.name!r}")
            self._skills[spec.name] = spec
        if not self._skills:
            raise SkillError(f"no skills found under {skills_dir}")

    def get(self, name: str) -> SkillSpec:
        if name not in self._skills:
            raise SkillError(f"unknown skill {name!r}; known: {sorted(self._skills)}")
        return self._skills[name]

    def names(self) -> list[str]:
        return list(self._skills)

    def catalogue(self) -> list[SkillCatalogEntry]:
        return [SkillCatalogEntry(name=s.name, description=s.description) for s in self._skills.values()]

    def all_tools(self) -> set[str]:
        return {t for s in self._skills.values() for t in s.tools}

    def validate_tools(self, seat_tools: set[str], local_tools: set[str]) -> None:
        """Every tool a skill names must be a seat tool (tools/list) or one of our local tools."""
        problems = [f"{s.name}: {t}" for s in self._skills.values() for t in s.tools
                    if t not in seat_tools and t not in local_tools]
        if problems:
            raise SkillError("skills name tools this seat does not have: " + ", ".join(problems))
