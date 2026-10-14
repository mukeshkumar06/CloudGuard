"""Governance tagging policy: account-level defaults plus resource-type-specific overrides.

A PolicyDocument is pure configuration. It is validated in full before it can be saved, and every
saved version is immutable and identified by (policy_id, version, content_hash). The compliance
engine only ever sees the effective rule list for one resource type, from `rules_for()`.
"""

from __future__ import annotations

import hashlib
import json
from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator, model_validator

from cloudguard.governance.models import SUPPORTED_RESOURCE_TYPES, PolicyRef

# AWS tag limits shared by Lambda, DynamoDB, S3 and EC2.
MAX_KEY_LEN = 128
MAX_VALUE_LEN = 256


class _Config(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")


def _dupes(values: list[str]) -> list[str]:
    seen: dict[str, str] = {}
    dupes = []
    for v in values:
        low = v.lower()
        if low in seen:
            dupes.append(v)
        seen.setdefault(low, v)
    return dupes


class TagRule(_Config):
    key: str
    description: str = ""
    # False makes the tag optional: absence is fine, but a present value must still be valid.
    required: bool = True
    # Canonical values, matched case-insensitively. Empty means any non-empty value is accepted.
    allowed_values: list[str] = Field(default_factory=list)
    # alias -> canonical value, e.g. {"prod": "Production"}. Aliases pass validation but are
    # reported as NON_CANONICAL_VALUE notices so the user can be offered the canonical value.
    aliases: dict[str, str] = Field(default_factory=dict)

    @field_validator("key")
    @classmethod
    def _valid_key(cls, v: str) -> str:
        v = v.strip()
        if not v:
            raise ValueError("tag key must not be blank")
        if len(v) > MAX_KEY_LEN:
            raise ValueError(f"tag key exceeds {MAX_KEY_LEN} characters")
        if v.lower().startswith("aws:"):
            raise ValueError("tag keys starting with 'aws:' are reserved by AWS")
        return v

    @field_validator("allowed_values")
    @classmethod
    def _valid_allowed(cls, v: list[str]) -> list[str]:
        v = [x.strip() for x in v]
        if any(not x for x in v):
            raise ValueError("allowed values must not be blank")
        if any(len(x) > MAX_VALUE_LEN for x in v):
            raise ValueError(f"allowed values must be at most {MAX_VALUE_LEN} characters")
        if d := _dupes(v):
            raise ValueError(f"duplicate allowed values (case-insensitive): {d}")
        return v

    @model_validator(mode="after")
    def _valid_aliases(self) -> TagRule:
        if not self.aliases:
            return self
        if not self.allowed_values:
            raise ValueError(f"{self.key}: aliases require allowed_values")
        canonical = {a.lower(): a for a in self.allowed_values}
        if d := _dupes(list(self.aliases)):
            raise ValueError(f"{self.key}: duplicate aliases (case-insensitive): {d}")
        normalized: dict[str, str] = {}
        for alias, target in self.aliases.items():
            alias = alias.strip()
            if not alias:
                raise ValueError(f"{self.key}: alias must not be blank")
            if alias.lower() in canonical:
                raise ValueError(f"{self.key}: alias '{alias}' collides with an allowed value")
            if target.strip().lower() not in canonical:
                raise ValueError(f"{self.key}: alias '{alias}' targets '{target}', which is not an allowed value")
            normalized[alias] = canonical[target.strip().lower()]
        object.__setattr__(self, "aliases", normalized)  # store targets in canonical spelling
        return self

    def canonical_value(self, value: str) -> str | None:
        """Canonical spelling for a value, or None if the value isn't allowed. Never mutates tags."""
        v = value.strip()
        if not self.allowed_values:
            return v
        low = v.lower()
        for allowed in self.allowed_values:
            if allowed.lower() == low:
                return allowed
        for alias, target in self.aliases.items():
            if alias.lower() == low:
                return target
        return None


class ResourceOverride(_Config):
    # Rules to add for this resource type, or to replace a default rule with the same key.
    rules: list[TagRule] = Field(default_factory=list)
    # Default rule keys that don't apply to this resource type.
    remove: list[str] = Field(default_factory=list)


class PolicyDocument(_Config):
    defaults: list[TagRule]
    overrides: dict[str, ResourceOverride] = Field(default_factory=dict)

    @model_validator(mode="after")
    def _validate(self) -> PolicyDocument:
        if not self.defaults:
            raise ValueError("defaults must define at least one tag rule")
        if d := _dupes([r.key for r in self.defaults]):
            raise ValueError(f"duplicate default rule keys (case-insensitive): {d}")
        default_keys = {r.key.lower() for r in self.defaults}
        for rtype, ov in self.overrides.items():
            if rtype not in SUPPORTED_RESOURCE_TYPES:
                raise ValueError(f"unsupported resource type '{rtype}'; expected one of {list(SUPPORTED_RESOURCE_TYPES)}")
            if d := _dupes([r.key for r in ov.rules]):
                raise ValueError(f"{rtype}: duplicate override rule keys (case-insensitive): {d}")
            if d := _dupes(ov.remove):
                raise ValueError(f"{rtype}: duplicate removed keys: {d}")
            unknown = [k for k in ov.remove if k.lower() not in default_keys]
            if unknown:
                raise ValueError(f"{rtype}: cannot remove keys that aren't in defaults: {unknown}")
            both = {r.key.lower() for r in ov.rules} & {k.lower() for k in ov.remove}
            if both:
                raise ValueError(f"{rtype}: keys both overridden and removed: {sorted(both)}")
            if not any(r.required for r in self._effective(rtype)):
                raise ValueError(f"{rtype}: override leaves no mandatory tags")
        return self

    def _effective(self, resource_type: str) -> list[TagRule]:
        ov = self.overrides.get(resource_type)
        if ov is None:
            return list(self.defaults)
        removed = {k.lower() for k in ov.remove}
        replaced = {r.key.lower(): r for r in ov.rules}
        rules = [replaced.pop(r.key.lower(), r) for r in self.defaults if r.key.lower() not in removed]
        return rules + list(replaced.values())

    def rules_for(self, resource_type: str) -> list[TagRule]:
        """Effective rules for a resource type: defaults, minus removals, with overrides applied."""
        return self._effective(resource_type)

    def content_hash(self) -> str:
        """SHA-256 over canonical JSON. Fields left at their defaults are excluded, so adding a new
        defaulted field to the schema later doesn't change the hash of existing stored versions."""
        canonical = json.dumps(
            self.model_dump(mode="json", exclude_defaults=True), sort_keys=True, separators=(",", ":")
        )
        return "sha256:" + hashlib.sha256(canonical.encode()).hexdigest()


class PolicyVersion(_Config):
    """An immutable, saved policy version."""

    policy_id: str
    version: int = Field(ge=1)
    document: PolicyDocument
    content_hash: str
    created_at: datetime
    created_by: str
    note: str = ""
    restored_from: int | None = None  # set when this version re-activates an older document

    @model_validator(mode="after")
    def _hash_matches(self) -> PolicyVersion:
        if self.content_hash != self.document.content_hash():
            raise ValueError("content_hash does not match the policy document")
        return self

    @property
    def ref(self) -> PolicyRef:
        return PolicyRef(policy_id=self.policy_id, version=self.version, content_hash=self.content_hash)


class PolicyIntegrityError(RuntimeError):
    """A stored policy version doesn't match its recorded hash, or doesn't match a scan's reference."""


class PolicyValidationError(ValueError):
    def __init__(self, errors: list[str]):
        self.errors = errors
        super().__init__("invalid policy: " + "; ".join(errors))


def parse_policy(data: dict | str) -> PolicyDocument:
    """Validate raw policy configuration (dict or JSON text) and return a PolicyDocument."""
    try:
        if isinstance(data, str):
            return PolicyDocument.model_validate_json(data)
        return PolicyDocument.model_validate(data)
    except ValidationError as exc:
        raise PolicyValidationError(
            [f"{'.'.join(str(p) for p in e['loc']) or 'policy'}: {e['msg']}" for e in exc.errors()]
        ) from None


DEFAULT_POLICY_ID = "default"

DEFAULT_POLICY_DOCUMENT = PolicyDocument(
    defaults=[
        TagRule(
            key="Environment",
            description="Deployment environment",
            allowed_values=["Development", "Staging", "Production"],
            aliases={"dev": "Development", "stage": "Staging", "prod": "Production"},
        ),
        TagRule(key="Project", description="Project the resource belongs to"),
        TagRule(key="Owner", description="Responsible person or team"),
        TagRule(key="CostCenter", description="Cost allocation category"),
    ]
)
