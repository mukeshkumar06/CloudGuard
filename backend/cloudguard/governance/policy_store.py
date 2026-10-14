"""Versioned policy persistence.

Single-table DynamoDB layout (see docs/governance-persistence.md):
    PK = POLICY#<policy_id>   SK = VERSION#<10-digit version>   immutable policy version
    PK = POLICY#<policy_id>   SK = ACTIVE                       pointer to the active version
Saving writes the new version and moves the ACTIVE pointer in one transaction, conditioned on the
version the caller last saw (optimistic concurrency). Versions are never updated or deleted.
"""

from __future__ import annotations

import json
import threading
from datetime import UTC, datetime
from pathlib import Path
from typing import Protocol

from botocore.exceptions import ClientError
from pydantic import ValidationError

from cloudguard.governance.models import PolicyRef
from cloudguard.governance.policy import (
    DEFAULT_POLICY_DOCUMENT,
    DEFAULT_POLICY_ID,
    PolicyDocument,
    PolicyIntegrityError,
    PolicyVersion,
)


class PolicyConflictError(RuntimeError):
    """The active version changed since the caller read it, or the version already exists."""


class PolicyNotFoundError(LookupError):
    pass


class PolicyStore(Protocol):
    def active_version(self, policy_id: str) -> int | None: ...
    def get_version(self, policy_id: str, version: int) -> PolicyVersion | None: ...
    def list_versions(self, policy_id: str) -> list[PolicyVersion]: ...
    def commit(self, new: PolicyVersion, expected_active: int | None) -> None: ...


# ---------------------------------------------------------------- local (fixture mode / tests)


class LocalPolicyStore:
    """In-memory store, optionally persisted to one JSON file per policy under `directory`."""

    def __init__(self, directory: str | Path | None = None):
        self.directory = Path(directory) if directory else None
        self._lock = threading.Lock()
        self._data: dict[str, dict] = {}

    def _load(self, policy_id: str) -> dict:
        if policy_id not in self._data:
            path = self._path(policy_id)
            if path and path.exists():
                raw = json.loads(path.read_text(encoding="utf-8"))
                try:
                    raw["versions"] = {int(k): PolicyVersion.model_validate(v) for k, v in raw["versions"].items()}
                except ValidationError as exc:
                    raise PolicyIntegrityError(f"stored policy file {path} failed verification: {exc}") from None
                self._data[policy_id] = raw
            else:
                self._data[policy_id] = {"active": None, "versions": {}}
        return self._data[policy_id]

    def _path(self, policy_id: str) -> Path | None:
        if not self.directory:
            return None
        safe = "".join(c if c.isalnum() or c in "-_." else "_" for c in policy_id)
        return self.directory / f"policy-{safe}.json"

    def active_version(self, policy_id: str) -> int | None:
        return self._load(policy_id)["active"]

    def get_version(self, policy_id: str, version: int) -> PolicyVersion | None:
        return self._load(policy_id)["versions"].get(version)

    def list_versions(self, policy_id: str) -> list[PolicyVersion]:
        versions = self._load(policy_id)["versions"]
        return [versions[k] for k in sorted(versions)]

    def commit(self, new: PolicyVersion, expected_active: int | None) -> None:
        with self._lock:
            data = self._load(new.policy_id)
            if data["active"] != expected_active:
                raise PolicyConflictError(
                    f"policy '{new.policy_id}' active version is {data['active']}, expected {expected_active}"
                )
            if new.version in data["versions"]:
                raise PolicyConflictError(f"policy '{new.policy_id}' version {new.version} already exists")
            data["versions"][new.version] = new
            data["active"] = new.version
            path = self._path(new.policy_id)
            if path:
                path.parent.mkdir(parents=True, exist_ok=True)
                out = {
                    "active": data["active"],
                    "versions": {str(k): v.model_dump(mode="json") for k, v in data["versions"].items()},
                }
                path.write_text(json.dumps(out, indent=2), encoding="utf-8")


# ---------------------------------------------------------------- DynamoDB single table


def _pk(policy_id: str) -> str:
    return f"POLICY#{policy_id}"


def _version_sk(version: int) -> str:
    return f"VERSION#{version:010d}"


class DynamoPolicyStore:
    def __init__(self, client, table_name: str):
        self.client = client
        self.table = table_name

    def active_version(self, policy_id: str) -> int | None:
        item = self.client.get_item(
            TableName=self.table,
            Key={"PK": {"S": _pk(policy_id)}, "SK": {"S": "ACTIVE"}},
            ConsistentRead=True,
        ).get("Item")
        return int(item["version"]["N"]) if item else None

    def get_version(self, policy_id: str, version: int) -> PolicyVersion | None:
        item = self.client.get_item(
            TableName=self.table,
            Key={"PK": {"S": _pk(policy_id)}, "SK": {"S": _version_sk(version)}},
            ConsistentRead=True,
        ).get("Item")
        return self._from_item(item) if item else None

    def list_versions(self, policy_id: str) -> list[PolicyVersion]:
        out: list[PolicyVersion] = []
        pages = self.client.get_paginator("query").paginate(
            TableName=self.table,
            KeyConditionExpression="PK = :pk AND begins_with(SK, :v)",
            ExpressionAttributeValues={":pk": {"S": _pk(policy_id)}, ":v": {"S": "VERSION#"}},
            ConsistentRead=True,
        )
        for page in pages:
            out.extend(self._from_item(i) for i in page.get("Items", []))
        return out

    def commit(self, new: PolicyVersion, expected_active: int | None) -> None:
        pk = _pk(new.policy_id)
        version_item = {
            "PK": {"S": pk},
            "SK": {"S": _version_sk(new.version)},
            "entity": {"S": "PolicyVersion"},
            "policy_id": {"S": new.policy_id},
            "version": {"N": str(new.version)},
            "content_hash": {"S": new.content_hash},
            "document": {"S": new.document.model_dump_json()},
            "created_at": {"S": new.created_at.isoformat()},
            "created_by": {"S": new.created_by},
            "note": {"S": new.note},
        }
        if new.restored_from is not None:
            version_item["restored_from"] = {"N": str(new.restored_from)}
        active_item = {
            "PK": {"S": pk},
            "SK": {"S": "ACTIVE"},
            "entity": {"S": "PolicyActive"},
            "version": {"N": str(new.version)},
            "content_hash": {"S": new.content_hash},
            "updated_at": {"S": new.created_at.isoformat()},
        }
        if expected_active is None:
            active_cond = {"ConditionExpression": "attribute_not_exists(PK)"}
        else:
            active_cond = {
                "ConditionExpression": "#v = :expected",
                "ExpressionAttributeNames": {"#v": "version"},
                "ExpressionAttributeValues": {":expected": {"N": str(expected_active)}},
            }
        try:
            self.client.transact_write_items(
                TransactItems=[
                    {"Put": {"TableName": self.table, "Item": version_item, "ConditionExpression": "attribute_not_exists(PK)"}},
                    {"Put": {"TableName": self.table, "Item": active_item, **active_cond}},
                ]
            )
        except ClientError as exc:
            if exc.response.get("Error", {}).get("Code") == "TransactionCanceledException":
                raise PolicyConflictError(
                    f"policy '{new.policy_id}' changed concurrently (expected active version {expected_active})"
                ) from exc
            raise

    @staticmethod
    def _from_item(item: dict) -> PolicyVersion:
        try:
            return PolicyVersion(
                policy_id=item["policy_id"]["S"],
                version=int(item["version"]["N"]),
                document=PolicyDocument.model_validate_json(item["document"]["S"]),
                content_hash=item["content_hash"]["S"],
                created_at=datetime.fromisoformat(item["created_at"]["S"]),
                created_by=item["created_by"]["S"],
                note=item.get("note", {}).get("S", ""),
                restored_from=int(item["restored_from"]["N"]) if "restored_from" in item else None,
            )
        except ValidationError as exc:
            raise PolicyIntegrityError(
                f"stored policy {item.get('PK', {}).get('S')} {item.get('SK', {}).get('S')} failed verification: {exc}"
            ) from None


# ---------------------------------------------------------------- service


class PolicyManager:
    """Validated, versioned policy operations on top of any PolicyStore."""

    def __init__(self, store: PolicyStore):
        self.store = store

    def get_active(self, policy_id: str = DEFAULT_POLICY_ID) -> PolicyVersion:
        version = self.store.active_version(policy_id)
        if version is None:
            raise PolicyNotFoundError(f"policy '{policy_id}' has no saved versions")
        found = self.store.get_version(policy_id, version)
        if found is None:
            raise PolicyNotFoundError(f"policy '{policy_id}' active version {version} is missing")
        return found

    def get_version(self, policy_id: str, version: int) -> PolicyVersion:
        found = self.store.get_version(policy_id, version)
        if found is None:
            raise PolicyNotFoundError(f"policy '{policy_id}' version {version} not found")
        return found

    def resolve(self, ref: PolicyRef) -> PolicyVersion:
        """The exact immutable version a scan used. Fails if it's missing or its hash differs."""
        found = self.get_version(ref.policy_id, ref.version)
        if found.content_hash != ref.content_hash:
            raise PolicyIntegrityError(
                f"policy '{ref.policy_id}' version {ref.version} hash {found.content_hash} "
                f"does not match the scan's recorded hash {ref.content_hash}"
            )
        return found

    def history(self, policy_id: str = DEFAULT_POLICY_ID) -> list[PolicyVersion]:
        return self.store.list_versions(policy_id)

    def save(
        self,
        document: PolicyDocument,
        *,
        expected_version: int | None,
        created_by: str,
        note: str = "",
        policy_id: str = DEFAULT_POLICY_ID,
        restored_from: int | None = None,
    ) -> PolicyVersion:
        """Save a new version. `expected_version` must equal the current active version (None if new)."""
        if not created_by.strip():
            raise ValueError("created_by is required")
        # Re-validate even typed documents: model_copy/construct can bypass validators.
        document = PolicyDocument.model_validate(document.model_dump())
        current = self.store.active_version(policy_id)
        if current is not None:
            active = self.store.get_version(policy_id, current)
            if active and active.content_hash == document.content_hash():
                raise ValueError(f"policy '{policy_id}' is unchanged from active version {current}")
        new = PolicyVersion(
            policy_id=policy_id,
            version=(expected_version or 0) + 1,
            document=document,
            content_hash=document.content_hash(),
            created_at=datetime.now(UTC),
            created_by=created_by.strip(),
            note=note,
            restored_from=restored_from,
        )
        self.store.commit(new, expected_version)
        return new

    def restore(self, version: int, *, expected_version: int, created_by: str, policy_id: str = DEFAULT_POLICY_ID) -> PolicyVersion:
        """Re-activate an old version by saving its document as a new version (history stays linear)."""
        old = self.get_version(policy_id, version)
        return self.save(
            old.document, expected_version=expected_version, created_by=created_by,
            note=f"restored from version {version}", policy_id=policy_id, restored_from=version,
        )

    def ensure_default(self, created_by: str = "cloudguard-bootstrap", policy_id: str = DEFAULT_POLICY_ID) -> PolicyVersion:
        """Return the active policy, creating version 1 from the built-in default if none exists."""
        if self.store.active_version(policy_id) is None:
            try:
                return self.save(DEFAULT_POLICY_DOCUMENT, expected_version=None, created_by=created_by,
                                 note="built-in default policy", policy_id=policy_id)
            except PolicyConflictError:
                pass  # someone else bootstrapped it first
        return self.get_active(policy_id)
