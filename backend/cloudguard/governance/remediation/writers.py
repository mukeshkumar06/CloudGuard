"""Fixture-only resource state and tag writing (Checkpoint 4.3).

FixtureAccount is an in-memory simulation of an AWS account's Lambda functions, DynamoDB tables,
S3 buckets and EC2 instances. FixtureWriter applies approved tag changes to it, and only to it.

There is deliberately no live writer, no boto3 import and no switch (flag, environment variable or
configuration) that turns either into a real AWS client. Live writers are out of scope until an
explicitly approved later checkpoint.
"""

from __future__ import annotations

import json
import os
import tempfile
import threading
import uuid
from dataclasses import dataclass, field
from enum import StrEnum
from pathlib import Path

from cloudguard.governance.custom_tagging.models import ChangeAction
from cloudguard.governance.models import DiscoveredResource, DiscoveryResult
from cloudguard.governance.remediation.execution_models import ApprovedChange

FIXTURE_DISCOVERED_AT = "2026-10-14T06:00:00Z"


class FixtureError(Exception):
    """Base class for simulated AWS errors."""

    category = "UNKNOWN"


class FixtureThrottled(FixtureError):
    category = "THROTTLED"


class FixtureTransient(FixtureError):
    category = "TRANSIENT"


class FixtureAccessDenied(FixtureError):
    category = "ACCESS_DENIED"


class FixtureNotFound(FixtureError):
    category = "NOT_FOUND"


class SimulatedCrash(BaseException):
    """Simulates the executor process dying. BaseException, so ordinary error handling can't swallow it."""


class FaultKind(StrEnum):
    THROTTLE = "THROTTLE"
    TRANSIENT = "TRANSIENT"
    ACCESS_DENIED = "ACCESS_DENIED"
    SILENT_NO_OP = "SILENT_NO_OP"  # write reports success but changes nothing (verification must catch it)
    STALE_READ = "STALE_READ"  # verification read returns the pre-write tags


class ReadPurpose(StrEnum):
    REVALIDATE = "revalidate"
    PREWRITE = "prewrite"
    VERIFY = "verify"


@dataclass
class _Fault:
    arn: str
    op: str  # "read:<purpose>" | "write"
    kind: FaultKind
    remaining: int


@dataclass
class FixtureResource:
    arn: str
    resource_type: str
    account_id: str
    region: str
    name: str
    uid: str | None  # immutable identity: TableId / instance id / bucket creation date; None for Lambda
    uid_kind: str | None
    tags: dict[str, str] = field(default_factory=dict)
    generation: int = 1


@dataclass(frozen=True)
class ResourceSnapshot:
    """What a read returns. The same shape a future live reader would have to produce."""

    exists: bool
    arn: str
    resource_type: str | None = None
    account_id: str | None = None
    region: str | None = None
    uid: str | None = None
    uid_kind: str | None = None
    tags: dict[str, str] | None = None
    request_id: str | None = None


@dataclass(frozen=True)
class WriteResult:
    arn: str
    written_keys: list[str]
    request_id: str


def _uid_for(resource_type: str, arn: str, generation: int) -> tuple[str | None, str | None]:
    if resource_type == "AWS::DynamoDB::Table":
        return str(uuid.uuid5(uuid.NAMESPACE_URL, f"{arn}#generation{generation}")), "TableId"
    if resource_type == "AWS::S3::Bucket":
        return f"2026-01-{generation:02d}T00:00:00Z", "CreationDate"
    if resource_type == "AWS::EC2::Instance":
        instance_id = arn.rsplit("/", 1)[-1]
        return (instance_id if generation == 1 else f"{instance_id}-gen{generation}"), "InstanceId"
    return None, None  # Lambda: no universal immutable identifier established


class FixtureAccount:
    """In-memory simulated account. Also a DiscoverySource, so proposals and executions see one world."""

    mode = "fixture"

    def __init__(self, account_id: str, regions: list[str]):
        self.account_id = account_id
        self.regions = list(regions)
        self.resources: dict[str, FixtureResource] = {}
        self._faults: list[_Fault] = []
        self._lock = threading.RLock()
        self._request_seq = 0
        self.read_log: list[tuple[str, str]] = []
        self.write_log: list[tuple[str, dict[str, str]]] = []

    # ---------------------------------------------------------------- construction / persistence

    @classmethod
    def from_discovery(cls, discovery: DiscoveryResult) -> FixtureAccount:
        account = cls(discovery.account_id, discovery.regions)
        for r in discovery.resources:
            uid, kind = _uid_for(r.resource_type, r.resource_arn, 1)
            account.resources[r.resource_arn] = FixtureResource(
                arn=r.resource_arn, resource_type=r.resource_type, account_id=r.account_id, region=r.region,
                name=r.resource_name, uid=uid, uid_kind=kind, tags=dict(r.tags) if r.tags is not None else {})
            if r.tags is None:  # tags weren't readable in the source data: keep them unreadable
                account.inject(r.resource_arn, "read:*", FaultKind.ACCESS_DENIED, times=10**9)
        return account

    def to_json(self) -> str:
        with self._lock:
            return json.dumps({
                "account_id": self.account_id, "regions": self.regions,
                "resources": [vars(r) for r in self.resources.values()],
                "faults": [vars(f) | {"kind": f.kind.value} for f in self._faults],
            }, indent=2, sort_keys=True)

    @classmethod
    def from_json(cls, text: str) -> FixtureAccount:
        data = json.loads(text)
        account = cls(data["account_id"], data["regions"])
        for r in data["resources"]:
            account.resources[r["arn"]] = FixtureResource(**r)
        account._faults = [_Fault(f["arn"], f["op"], FaultKind(f["kind"]), f["remaining"]) for f in data["faults"]]
        return account

    def save(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=".tmp-", suffix=".json")
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(self.to_json())
        os.replace(tmp, path)

    @classmethod
    def load(cls, path: Path) -> FixtureAccount:
        return cls.from_json(path.read_text(encoding="utf-8"))

    # ---------------------------------------------------------------- discovery (read-only view)

    def discover(self) -> DiscoveryResult:
        resources = []
        for r in self.resources.values():
            denied = self._has_fault(r.arn, "read:revalidate", FaultKind.ACCESS_DENIED)
            resources.append(DiscoveredResource(
                account_id=r.account_id, resource_type=r.resource_type, service=r.resource_type.split("::")[1].lower(),
                resource_id=r.arn.rsplit("/", 1)[-1].rsplit(":", 1)[-1] if r.resource_type != "AWS::S3::Bucket" else r.name,
                resource_name=r.name, resource_arn=r.arn, region=r.region, discovered_at=FIXTURE_DISCOVERED_AT,
                tags=None if denied else dict(r.tags), tag_source="fixture",
                tag_error={"kind": "ACCESS_DENIED", "code": "AccessDenied", "message": "simulated", "operation": "fixture"}
                if denied else None,
            ))
        return DiscoveryResult(account_id=self.account_id, regions=self.regions, resources=resources)

    def identities(self, arns: list[str] | None = None) -> dict[str, str]:
        return {a: r.uid for a, r in self.resources.items() if r.uid and (arns is None or a in arns)}

    # ---------------------------------------------------------------- simulation controls (tests / demo)

    def inject(self, arn: str, op: str, kind: FaultKind, times: int = 1) -> None:
        """op: 'write', 'read:revalidate' | 'read:prewrite' | 'read:verify', or 'read:*'."""
        with self._lock:
            self._faults.append(_Fault(arn, op, kind, times))

    def delete_resource(self, arn: str) -> None:
        with self._lock:
            self.resources.pop(arn)

    def recreate_resource(self, arn: str, tags: dict[str, str] | None = None) -> None:
        """Replace the resource under the same ARN: new immutable identity (where one exists)."""
        with self._lock:
            old = self.resources[arn]
            uid, kind = _uid_for(old.resource_type, arn, old.generation + 1)
            self.resources[arn] = FixtureResource(arn=arn, resource_type=old.resource_type, account_id=old.account_id,
                                                  region=old.region, name=old.name, uid=uid, uid_kind=kind,
                                                  tags=dict(tags if tags is not None else old.tags),
                                                  generation=old.generation + 1)

    def external_tag_change(self, arn: str, key: str, value: str | None) -> None:
        """Another actor changes tags outside CloudGuard (value None removes the key)."""
        with self._lock:
            tags = self.resources[arn].tags
            if value is None:
                tags.pop(key, None)
            else:
                tags[key] = value

    # ---------------------------------------------------------------- reads and writes

    def _next_request_id(self) -> str:
        self._request_seq += 1
        return f"fixture-req-{self._request_seq:06d}"

    def _has_fault(self, arn: str, op: str, kind: FaultKind) -> bool:
        return any(f.arn == arn and f.kind == kind and f.remaining > 0 and (f.op == op or
                   (f.op == "read:*" and op.startswith("read:"))) for f in self._faults)

    def _take_fault(self, arn: str, op: str) -> FaultKind | None:
        for f in self._faults:
            if f.arn == arn and f.remaining > 0 and (f.op == op or (f.op == "read:*" and op.startswith("read:"))):
                f.remaining -= 1
                return f.kind
        return None

    def read(self, arn: str, purpose: ReadPurpose, stale_tags: dict[str, str] | None = None) -> ResourceSnapshot:
        with self._lock:
            self.read_log.append((arn, purpose.value))
            fault = self._take_fault(arn, f"read:{purpose.value}")
            if fault == FaultKind.THROTTLE:
                raise FixtureThrottled(f"simulated throttling reading {arn}")
            if fault == FaultKind.TRANSIENT:
                raise FixtureTransient(f"simulated server error reading {arn}")
            if fault == FaultKind.ACCESS_DENIED:
                raise FixtureAccessDenied(f"simulated AccessDenied reading {arn}")
            r = self.resources.get(arn)
            if r is None:
                return ResourceSnapshot(exists=False, arn=arn, request_id=self._next_request_id())
            tags = dict(stale_tags) if fault == FaultKind.STALE_READ and stale_tags is not None else dict(r.tags)
            return ResourceSnapshot(exists=True, arn=r.arn, resource_type=r.resource_type, account_id=r.account_id,
                                    region=r.region, uid=r.uid, uid_kind=r.uid_kind, tags=tags,
                                    request_id=self._next_request_id())

    def _apply_tags(self, arn: str, tags: dict[str, str]) -> WriteResult:
        """TagResource semantics: add or overwrite the given keys; never remove any key."""
        with self._lock:
            fault = self._take_fault(arn, "write")
            if fault == FaultKind.THROTTLE:
                raise FixtureThrottled(f"simulated throttling writing {arn}")
            if fault == FaultKind.TRANSIENT:
                raise FixtureTransient(f"simulated server error writing {arn}")
            if fault == FaultKind.ACCESS_DENIED:
                raise FixtureAccessDenied(f"simulated AccessDenied writing {arn}")
            r = self.resources.get(arn)
            if r is None:
                raise FixtureNotFound(f"{arn} does not exist")
            if fault != FaultKind.SILENT_NO_OP:
                r.tags.update(tags)
            self.write_log.append((arn, dict(tags)))
            return WriteResult(arn=arn, written_keys=sorted(tags), request_id=self._next_request_id())


@dataclass(frozen=True)
class ApprovedWrite:
    """A server-generated, revalidated write. Built only by the executor from the stored proposal."""

    resource_arn: str
    changes: tuple[ApprovedChange, ...]

    def __post_init__(self):
        keys = [c.key for c in self.changes]
        if not self.changes or len(set(keys)) != len(keys):
            raise ValueError("an approved write needs one or more distinct changes")


class FixtureWriter:
    """Applies approved changes to a FixtureAccount. Cannot target anything else."""

    def __init__(self, account: FixtureAccount):
        if type(account) is not FixtureAccount:
            raise TypeError("FixtureWriter only writes to a FixtureAccount")
        self.account = account

    def apply(self, write: ApprovedWrite) -> WriteResult:
        if not isinstance(write, ApprovedWrite):
            raise TypeError("FixtureWriter accepts only ApprovedWrite instances")
        for c in write.changes:
            if c.action not in (ChangeAction.ADD, ChangeAction.OVERWRITE):
                raise ValueError(f"unsupported change action {c.action}")
        # Only the approved keys, only add/overwrite: unrelated tags are untouched and nothing is removed.
        return self.account._apply_tags(write.resource_arn, {c.key: c.new_value for c in write.changes})
