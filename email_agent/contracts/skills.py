"""Skill contracts: one SKILL.md = one feature = the tools it may use + how to do the job."""

from __future__ import annotations

import re

from pydantic import BaseModel, Field, field_validator

SKILL_NAME = re.compile(r"^[a-z][a-z0-9-]{1,40}$")


class SkillSpec(BaseModel):
    """Parsed from email_agent/skills/<name>/SKILL.md (YAML front matter + markdown body)."""

    name: str
    description: str = Field(min_length=10, max_length=300)
    tools: list[str] = Field(min_length=1)       # MCP tool names or local tool names
    instructions: str = Field(min_length=20)     # the markdown body, given to Decision for this skill's goals
    path: str                                    # where it was loaded from (for error messages)

    @field_validator("name")
    @classmethod
    def _name_shape(cls, v: str) -> str:
        if not SKILL_NAME.match(v):
            raise ValueError(f"skill name {v!r} must be lower-case letters, digits and dashes")
        return v

    @field_validator("tools")
    @classmethod
    def _unique(cls, v: list[str]) -> list[str]:
        dupes = sorted({t for t in v if v.count(t) > 1})
        if dupes:
            raise ValueError(f"tools listed twice: {dupes}")
        return v


class SkillCatalogEntry(BaseModel):
    """What Perception sees about a skill: never its tools, only what it is for."""

    name: str
    description: str
