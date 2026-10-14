"""Mandatory tagging policy. Configurable via a JSON file; defaults to the four v1 tags."""

from __future__ import annotations

import json
from pathlib import Path

from pydantic import BaseModel, ConfigDict, field_validator


class RequiredTag(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    key: str
    description: str = ""
    # Optional case-insensitive allow-list. None means any non-empty value is acceptable.
    allowed_values: list[str] | None = None

    @field_validator("key")
    @classmethod
    def _key_not_blank(cls, v: str) -> str:
        if not v.strip():
            raise ValueError("tag key must not be blank")
        return v.strip()


class TagPolicy(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    required_tags: list[RequiredTag]

    @field_validator("required_tags")
    @classmethod
    def _unique_keys(cls, v: list[RequiredTag]) -> list[RequiredTag]:
        if not v:
            raise ValueError("policy must require at least one tag")
        lowered = [t.key.lower() for t in v]
        if len(set(lowered)) != len(lowered):
            raise ValueError("required tag keys must be unique (case-insensitive)")
        return v

    @property
    def keys(self) -> list[str]:
        return [t.key for t in self.required_tags]

    @classmethod
    def load(cls, path: str | Path) -> TagPolicy:
        return cls.model_validate(json.loads(Path(path).read_text(encoding="utf-8")))


DEFAULT_POLICY = TagPolicy(
    required_tags=[
        RequiredTag(key="Environment", description="Deployment environment"),
        RequiredTag(key="Project", description="Project the resource belongs to"),
        RequiredTag(key="Owner", description="Responsible person or team"),
        RequiredTag(key="CostCenter", description="Cost allocation category"),
    ]
)
