"""Persistence and lifecycle models for tagging proposals (Phase 4, Checkpoint 4.1).

Three kinds of record, deliberately separate:
- StoredProposal: the immutable, validated TaggingProposal body. Written once, never modified.
- LifecycleEvent: append-only history. The authoritative record of every state change.
- ProposalHead: a materialised view of the current state, derivable from the events.

Actors: SYSTEM (CloudGuard itself), UNAUTHENTICATED (a caller-supplied label, never identity),
DEVELOPMENT (fixture-mode identity, never authenticated) and AWS_PRINCIPAL (verified through
sts:GetCallerIdentity by an IdentityProvider - Checkpoint 4.2).
"""

from __future__ import annotations

import hashlib
import json
import re
from datetime import datetime
from enum import StrEnum
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator, model_serializer, model_validator

from cloudguard.governance.custom_tagging.models import PROPOSAL_SCHEMA, TaggingProposal
from cloudguard.governance.models import PolicyRef

PROPOSAL_ID_PATTERN = re.compile(r"^proposal-[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$")
IDEMPOTENCY_KEY_PATTERN = re.compile(r"^[A-Za-z0-9._:-]{8,128}$")


class _Frozen(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")


def _utc(value: datetime) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("timestamps must be timezone-aware UTC")
    if value.utcoffset().total_seconds() != 0:
        raise ValueError("timestamps must be UTC")
    return value


def canonical_digest(data: Any) -> str:
    return "sha256:" + hashlib.sha256(json.dumps(data, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


class ProposalIntegrityError(RuntimeError):
    """Stored proposal data failed validation (tampering, corruption or an inconsistent record)."""


class ProposalState(StrEnum):
    PENDING = "PENDING"
    APPROVED = "APPROVED"
    REJECTED = "REJECTED"
    REVOKED = "REVOKED"
    EXPIRED = "EXPIRED"
    STALE = "STALE"
    EXECUTING = "EXECUTING"
    SUCCEEDED = "SUCCEEDED"
    PARTIAL = "PARTIAL"
    FAILED = "FAILED"
    ABORTED = "ABORTED"  # an EXECUTING proposal stopped by an ABORT decision (Batch 2 / M1)


class EventType(StrEnum):
    PROPOSAL_CREATED = "PROPOSAL_CREATED"
    PROPOSAL_EXPIRED = "PROPOSAL_EXPIRED"
    MARKED_STALE = "MARKED_STALE"
    # Defined for the state machine; no operation emits these in Checkpoint 4.1.
    APPROVED = "APPROVED"
    REJECTED = "REJECTED"
    REVOKED = "REVOKED"
    EXECUTION_STARTED = "EXECUTION_STARTED"
    EXECUTION_SUCCEEDED = "EXECUTION_SUCCEEDED"
    EXECUTION_PARTIAL = "EXECUTION_PARTIAL"
    EXECUTION_FAILED = "EXECUTION_FAILED"
    EXECUTION_ABORTED = "EXECUTION_ABORTED"  # Batch 2 (M1): ABORT decision on an EXECUTING proposal


class ActorType(StrEnum):
    SYSTEM = "SYSTEM"  # CloudGuard itself (e.g. lazy expiry)
    UNAUTHENTICATED = "UNAUTHENTICATED"  # a caller-supplied label; NOT an authenticated identity
    DEVELOPMENT = "DEVELOPMENT"  # fixture-mode identity; never authenticated, never valid for live AWS
    AWS_PRINCIPAL = "AWS_PRINCIPAL"  # canonical IAM principal verified via sts:GetCallerIdentity


class Actor(_Frozen):
    actor_type: ActorType
    id: str = Field(min_length=1, max_length=256)

    @field_validator("id")
    @classmethod
    def _printable(cls, v: str) -> str:
        v = v.strip()
        if not v or any(ord(c) < 32 for c in v):
            raise ValueError("actor id must be non-blank printable text")
        return v

    @property
    def authenticated(self) -> bool:
        return self.actor_type == ActorType.AWS_PRINCIPAL


SYSTEM_EXPIRY_ACTOR = Actor(actor_type=ActorType.SYSTEM, id="cloudguard-lifecycle")


def binding_digest(proposal: TaggingProposal, resource_identities: dict[str, str] | None = None) -> str:
    """Digest of exactly what an approval binds to: approval_binding() plus, when captured at
    creation, the immutable identity of each target resource (Checkpoint 4.3). Tampering with
    either changes the digest and breaks the stored binding."""
    binding = proposal.approval_binding()
    if resource_identities:
        binding = {**binding, "resource_identities": dict(sorted(resource_identities.items()))}
    return canonical_digest(binding)


def request_fingerprint(proposal: TaggingProposal) -> str:
    """What the client asked for, independent of when it was asked or what was discovered.
    Used for idempotency: the same key with a different fingerprint is a conflict."""
    return canonical_digest({
        "operation": proposal.operation.value,
        "target": proposal.request.target.model_dump(mode="json"),
        "tags": proposal.request.tags,
        "account_id": proposal.account_id,
        "policy": proposal.policy.model_dump(mode="json"),
    })


PERSON_ID_PATTERN = re.compile(r"^[a-z0-9][a-z0-9._-]{0,62}$")
BindingKind = Literal["iam_user", "single_person_role", "session_attributed_role", "development"]


class IdentityEvidence(_Frozen):
    """Who acted, as a PERSON (M3): the person a principal resolved to through the allow-list's
    person bindings, plus the raw evidence the resolution rested on. Separation of duties compares
    person_id; the rest is audit evidence."""

    person_id: str
    binding_kind: BindingKind
    principal_arn: str | None  # exact STS caller ARN (None for development identities)
    sts_user_id: str | None  # STS GetCallerIdentity UserId (None for development identities)
    authz_version: int = Field(ge=1)  # allow-list version the binding was read from
    authz_hash: str

    @model_validator(mode="after")
    def _shape(self) -> IdentityEvidence:
        if not PERSON_ID_PATTERN.match(self.person_id):
            raise ValueError("person ids are 1-63 chars of [a-z0-9._-]")
        if (self.binding_kind == "development") != (self.principal_arn is None and self.sts_user_id is None):
            raise ValueError("exactly development identities carry no principal ARN or STS UserId")
        return self


class StoredProposal(_Frozen):
    """The immutable proposal body plus the metadata needed to verify it on every read."""

    proposal_id: str
    content_hash: str
    binding_digest: str
    schema_version: str
    account_id: str
    policy: PolicyRef
    proposal_expires_at: datetime  # business-level expiry; never used as a DynamoDB TTL attribute
    stored_at: datetime
    created_by: Actor
    idempotency_key: str
    request_fingerprint: str
    proposal_json: str  # verbatim, validated TaggingProposal JSON
    # Immutable resource identities captured at creation (ARN -> TableId / instance id / bucket
    # creation date). Lambda has no universal immutable identifier and is never listed.
    resource_identities: dict[str, str] = Field(default_factory=dict)
    # M3: the requesting person. Absent only for storage-only writes (no DecisionService); LIVE
    # approvals refuse proposals without it.
    requester_identity: IdentityEvidence | None = None

    @field_validator("proposal_expires_at", "stored_at")
    @classmethod
    def _utc_only(cls, v: datetime) -> datetime:
        return _utc(v)

    @field_validator("proposal_id")
    @classmethod
    def _valid_id(cls, v: str) -> str:
        if not PROPOSAL_ID_PATTERN.match(v):
            raise ValueError("invalid proposal id")
        return v

    @classmethod
    def from_proposal(cls, proposal: TaggingProposal, *, created_by: Actor, idempotency_key: str,
                      stored_at: datetime, resource_identities: dict[str, str] | None = None,
                      requester_identity: IdentityEvidence | None = None) -> StoredProposal:
        identities = dict(resource_identities or {})
        changing = {r.resource_arn for r in proposal.records if r.changes}
        if not set(identities) <= changing:
            raise ValueError("resource identities may only be recorded for resources the proposal changes")
        return cls(
            proposal_id=proposal.proposal_id,
            content_hash=proposal.content_hash,
            binding_digest=binding_digest(proposal, identities),
            resource_identities=identities,
            schema_version=proposal.schema_version,
            account_id=proposal.account_id,
            policy=proposal.policy,
            proposal_expires_at=proposal.expires_at,
            stored_at=stored_at,
            created_by=created_by,
            idempotency_key=idempotency_key,
            request_fingerprint=request_fingerprint(proposal),
            proposal_json=proposal.model_dump_json(),
            requester_identity=requester_identity,
        )

    def load(self) -> TaggingProposal:
        """Parse and verify the stored body. Raises ProposalIntegrityError on any mismatch."""
        try:
            proposal = TaggingProposal.model_validate_json(self.proposal_json)  # re-checks content_hash
        except ValidationError as exc:
            raise ProposalIntegrityError(f"stored proposal {self.proposal_id} failed validation: {exc}") from None
        checks = {
            "proposal_id": (proposal.proposal_id, self.proposal_id),
            "content_hash": (proposal.content_hash, self.content_hash),
            "schema_version": (proposal.schema_version, self.schema_version),
            "account_id": (proposal.account_id, self.account_id),
            "policy": (proposal.policy, self.policy),
            "proposal_expires_at": (proposal.expires_at, self.proposal_expires_at),
            "binding_digest": (binding_digest(proposal, self.resource_identities), self.binding_digest),
            "request_fingerprint": (request_fingerprint(proposal), self.request_fingerprint),
        }
        for name, (actual, recorded) in checks.items():
            if actual != recorded:
                raise ProposalIntegrityError(f"stored proposal {self.proposal_id}: {name} does not match its metadata")
        if self.schema_version != PROPOSAL_SCHEMA:
            raise ProposalIntegrityError(f"stored proposal {self.proposal_id}: unsupported schema {self.schema_version!r}")
        if not proposal.is_approvable:
            raise ProposalIntegrityError(f"stored proposal {self.proposal_id} is not approvable; it should never have been stored")
        return proposal


class LifecycleEvent(_Frozen):
    proposal_id: str
    seq: int = Field(ge=1)
    event_type: EventType
    from_state: ProposalState | None  # None only for creation
    to_state: ProposalState
    actor: Actor
    at: datetime
    reason: str
    content_hash: str
    details: dict[str, str | int | bool | None] = Field(default_factory=dict)

    @field_validator("at")
    @classmethod
    def _utc_only(cls, v: datetime) -> datetime:
        return _utc(v)

    @model_validator(mode="after")
    def _legal(self) -> LifecycleEvent:
        from cloudguard.governance.remediation.lifecycle import event_type_for  # pure; avoids import cycle

        if (self.seq == 1) != (self.from_state is None):
            raise ValueError("only the first event (seq 1) has no previous state")
        if event_type_for(self.from_state, self.to_state) != self.event_type:
            raise ValueError(f"event type {self.event_type} does not match {self.from_state} -> {self.to_state}")
        return self


class ProposalHead(_Frozen):
    """Materialised current state. Authoritative history is the event stream."""

    proposal_id: str
    state: ProposalState
    seq: int = Field(ge=1)  # seq of the last applied event; the optimistic-concurrency token
    content_hash: str
    proposal_expires_at: datetime
    updated_at: datetime
    # Set by the APPROVED event (Checkpoint 4.2); kept afterwards as history.
    approval_id: str | None = None
    approval_expires_at: datetime | None = None
    execution_id: str | None = None  # set by EXECUTION_STARTED (Checkpoint 4.3)

    @field_validator("proposal_expires_at", "updated_at")
    @classmethod
    def _utc_only(cls, v: datetime) -> datetime:
        return _utc(v)

    @field_validator("approval_expires_at")
    @classmethod
    def _utc_optional(cls, v: datetime | None) -> datetime | None:
        return None if v is None else _utc(v)

    def expiry_deadline(self) -> datetime | None:
        """When the current state lapses: proposal expiry while PENDING, the earlier of proposal and
        approval expiry while APPROVED, otherwise never."""
        if self.state == ProposalState.PENDING:
            return self.proposal_expires_at
        if self.state == ProposalState.APPROVED:
            return min(self.proposal_expires_at, self.approval_expires_at or self.proposal_expires_at)
        return None


class IdempotencyRecord(_Frozen):
    """Maps a client idempotency key to the proposal it created. Carries ttl_epoch as a retention hint."""

    scope: str
    key: str
    request_fingerprint: str
    proposal_id: str
    created_at: datetime
    ttl_epoch: int  # retention hint (epoch seconds); NOT a DynamoDB TTL - the governance table has no TTL
    decision_id: str | None = None  # set for decision (approve/reject/revoke) idempotency records
    execution_id: str | None = None  # set for execution idempotency records (Checkpoint 4.3)

    @field_validator("key")
    @classmethod
    def _valid_key(cls, v: str) -> str:
        if not IDEMPOTENCY_KEY_PATTERN.match(v):
            raise ValueError("idempotency key must be 8-128 characters of [A-Za-z0-9._:-]")
        return v


class ProposalView(_Frozen):
    proposal: TaggingProposal
    head: ProposalHead
    created_by: Actor


class CreateResult(_Frozen):
    proposal_id: str
    created: bool  # False when an identical earlier request was replayed
    head: ProposalHead


# ===================================================================== decisions (Checkpoint 4.2)


NONCE_PATTERN = re.compile(r"^[A-Za-z0-9_-]{22,128}$")


class OperatingMode(StrEnum):
    FIXTURE = "FIXTURE"  # synthetic data; development identities allowed; never authorises AWS writes
    LIVE = "LIVE"  # real AWS account; authenticated AWS principals only


class DecisionType(StrEnum):
    APPROVE = "APPROVE"
    REJECT = "REJECT"
    REVOKE = "REVOKE"
    ABORT = "ABORT"  # stop an EXECUTING proposal (Batch 2 / M1); governed by Permission.REJECT


class Permission(StrEnum):
    CREATE = "create"
    APPROVE = "approve"
    REJECT = "reject"  # also governs REVOKE (stopping an approved proposal) and ABORT (stopping an execution)
    EXECUTE = "execute"  # request execution of an approved proposal (Checkpoint 4.3)
    ADMIN = "admin"  # change the authorization allow-list itself (Batch 1 / H1)


class AuthzRef(_Frozen):
    config_id: str
    version: int = Field(ge=1)
    content_hash: str


class AuthorizationBasis(_Frozen):
    """Why a principal was allowed: the exact allow-list version and entry that matched."""

    ref: AuthzRef
    account_id: str
    permission: Permission
    matched_entry: str


class DecisionRecord(_Frozen):
    """Append-only, server-side record of an approval, rejection or revocation."""

    decision_id: str
    decision: DecisionType
    proposal_id: str
    content_hash: str
    binding_digest: str
    principal: Actor
    principal_arn: str | None  # raw caller ARN for AWS principals; None for development identities
    authorization: AuthorizationBasis
    mode: OperatingMode
    overwrite_acknowledged: bool
    nonce: str
    idempotency_key: str
    request_fingerprint: str
    decided_at: datetime
    approval_expires_at: datetime | None  # APPROVE only
    revokes_decision_id: str | None = None  # REVOKE only
    aborts_execution_id: str | None = None  # ABORT only
    identity: IdentityEvidence  # M3: the person who decided
    reason: str = Field(default="", max_length=1000)
    # Set only when a PersonalAccountWaiver let the requester approve (personal voice tagging). Omitted from
    # serialization when unset, so every other decision's stored form and digest are unchanged.
    separation_waived: str | None = None

    @model_serializer(mode="wrap")
    def _omit_unset_waiver(self, handler):
        data = handler(self)
        if data.get("separation_waived") is None:
            data.pop("separation_waived", None)
        return data

    @field_validator("decided_at", "approval_expires_at")
    @classmethod
    def _utc_only(cls, v: datetime | None) -> datetime | None:
        return None if v is None else _utc(v)

    @field_validator("nonce")
    @classmethod
    def _valid_nonce(cls, v: str) -> str:
        if not NONCE_PATTERN.match(v):
            raise ValueError("nonce must be 22-128 URL-safe characters")
        return v

    @field_validator("idempotency_key")
    @classmethod
    def _valid_key(cls, v: str) -> str:
        if not IDEMPOTENCY_KEY_PATTERN.match(v):
            raise ValueError("idempotency key must be 8-128 characters of [A-Za-z0-9._:-]")
        return v

    @model_validator(mode="after")
    def _shape(self) -> DecisionRecord:
        if (self.decision == DecisionType.APPROVE) != (self.approval_expires_at is not None):
            raise ValueError("only APPROVE decisions carry approval_expires_at")
        if (self.decision == DecisionType.REVOKE) != (self.revokes_decision_id is not None):
            raise ValueError("only REVOKE decisions reference the approval they revoke")
        if (self.decision == DecisionType.ABORT) != (self.aborts_execution_id is not None):
            raise ValueError("only ABORT decisions reference the execution they abort")
        if self.principal.actor_type not in (ActorType.AWS_PRINCIPAL, ActorType.DEVELOPMENT):
            raise ValueError("decisions require an identity from an IdentityProvider")
        if self.identity.principal_arn != self.principal_arn:
            raise ValueError("identity evidence must describe the deciding principal")
        if (self.mode == OperatingMode.LIVE) != (self.identity.binding_kind != "development"):
            raise ValueError("LIVE decisions are made by bound AWS principals; FIXTURE ones by development identities")
        if self.mode == OperatingMode.LIVE and not self.principal.authenticated:
            raise ValueError("live decisions require an authenticated AWS principal")
        if (self.principal.actor_type == ActorType.AWS_PRINCIPAL) != (self.principal_arn is not None):
            raise ValueError("principal_arn is recorded exactly for AWS principals")
        expected_permission = {DecisionType.APPROVE: Permission.APPROVE, DecisionType.REJECT: Permission.REJECT,
                               DecisionType.REVOKE: Permission.REJECT, DecisionType.ABORT: Permission.REJECT}[self.decision]
        if self.authorization.permission != expected_permission:
            raise ValueError("authorization basis does not grant this decision")
        return self


class NonceRecord(_Frozen):
    """Single-use marker; carries ttl_epoch as a retention hint that outlives the approval it protects."""

    nonce: str
    proposal_id: str
    decision_id: str
    used_at: datetime
    ttl_epoch: int
