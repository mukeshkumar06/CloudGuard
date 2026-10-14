"""Models for custom tagging requests and the proposals built from them.

A proposal is an immutable, read-only plan. Phase 3 never writes to AWS. Phase 4 will bind an
authenticated approval to `TaggingProposal.approval_binding()` (see docs/approval-readiness.md).
Approval state changes (approved, executed, expired) are recorded outside the proposal and never
mutate it.
"""

from __future__ import annotations

import hashlib
import json
from datetime import datetime, timedelta
from enum import StrEnum

from pydantic import BaseModel, ConfigDict, Field, model_validator

from cloudguard.governance.models import ComplianceStatus, InspectionError, PolicyRef

PROPOSAL_SCHEMA = "cloudguard.tagging-proposal/v1"
MAX_PROPOSAL_TTL = timedelta(hours=1)


class _Frozen(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")


class TaggingOperation(StrEnum):
    ADD_TAG = "ADD_TAG"  # one resource; never overwrites
    UPDATE_TAG = "UPDATE_TAG"  # one resource; overwrites an existing key, flagged explicitly
    BULK_ADD_TAG = "BULK_ADD_TAG"  # every resource matching explicit criteria; never overwrites
    BULK_UPDATE_TAG = "BULK_UPDATE_TAG"  # every match that already has the key; overwrites flagged

    @property
    def is_bulk(self) -> bool:
        return self in (TaggingOperation.BULK_ADD_TAG, TaggingOperation.BULK_UPDATE_TAG)

    @property
    def is_update(self) -> bool:
        return self in (TaggingOperation.UPDATE_TAG, TaggingOperation.BULK_UPDATE_TAG)


class TargetCriteria(_Frozen):
    """Exactly what the user said to target. Nothing here is inferred."""

    resource_type: str | None = None  # None only for specific targets named without a type
    name: str | None = None
    arn: str | None = None
    account_id: str | None = None
    region: str | None = None
    all_of_type: bool = False  # True only when the user explicitly said "all <type>"

    @model_validator(mode="after")
    def _shape(self) -> TargetCriteria:
        if self.all_of_type:
            if self.resource_type is None:
                raise ValueError("bulk targeting requires an explicit resource type")
            if self.name or self.arn:
                raise ValueError("bulk targeting cannot also name a specific resource")
        elif not (self.name or self.arn):
            raise ValueError("a specific target needs a resource name or ARN")
        return self


class TaggingRequest(_Frozen):
    operation: TaggingOperation
    target: TargetCriteria
    tags: dict[str, str]
    raw_text: str
    parsed_by: str  # "rules" now; a model-based fallback must identify itself here

    @model_validator(mode="after")
    def _consistent(self) -> TaggingRequest:
        if not self.tags:
            raise ValueError("a tagging request needs at least one tag")
        if self.operation.is_bulk != self.target.all_of_type:
            raise ValueError("bulk operations require bulk targets, and specific operations specific targets")
        if self.operation.is_update and len(self.tags) != 1:
            raise ValueError("update operations change exactly one tag")
        return self


class RejectionCode(StrEnum):
    UNSUPPORTED_REQUEST = "UNSUPPORTED_REQUEST"
    MALFORMED_TAGS = "MALFORMED_TAGS"
    INVALID_TAG = "INVALID_TAG"
    AMBIGUOUS_REQUEST = "AMBIGUOUS_REQUEST"
    OUT_OF_SCOPE = "OUT_OF_SCOPE"


class Rejection(_Frozen):
    code: RejectionCode
    message: str
    hint: str | None = None


class Candidate(_Frozen):
    resource_type: str
    resource_name: str
    resource_arn: str
    region: str


# ---------------------------------------------------------------- per-resource records


class ResourceOutcome(StrEnum):
    CHANGE_PROPOSED = "CHANGE_PROPOSED"  # at least one tag would be added or overwritten
    ALREADY_MATCHING = "ALREADY_MATCHING"  # every requested tag is already set to the requested value
    CONFLICT = "CONFLICT"  # nothing to change, and at least one requested tag conflicts
    POLICY_VIOLATION = "POLICY_VIOLATION"  # a requested value breaks the tagging policy for this type
    INSPECTION_FAILED = "INSPECTION_FAILED"  # current tags unknown: cannot prove a change is safe
    OUT_OF_SCOPE = "OUT_OF_SCOPE"  # same type, but outside the request's explicit criteria


class ChangeAction(StrEnum):
    ADD = "ADD"  # key absent on the resource
    OVERWRITE = "OVERWRITE"  # key present with a different value (update operations only)


class TagCategory(StrEnum):
    POLICY = "POLICY"  # the key is governed by the organisation's tagging policy
    CUSTOM = "CUSTOM"  # user-chosen tag; never becomes a mandatory policy tag


class TagChange(_Frozen):
    key: str
    action: ChangeAction
    old_value: str | None  # always set for OVERWRITE, so the overwrite is visible
    new_value: str
    category: TagCategory
    notes: list[str] = Field(default_factory=list)

    @model_validator(mode="after")
    def _overwrite_is_explicit(self) -> TagChange:
        if (self.action == ChangeAction.OVERWRITE) != (self.old_value is not None):
            raise ValueError("OVERWRITE changes must record old_value; ADD changes must not")
        return self


class MatchingTag(_Frozen):
    key: str
    value: str


class ConflictReason(StrEnum):
    WOULD_OVERWRITE = "WOULD_OVERWRITE"  # add operation, key has a different value: protected
    KEY_NOT_PRESENT = "KEY_NOT_PRESENT"  # update operation, key absent: not silently added
    KEY_CASE_CONFLICT = "KEY_CASE_CONFLICT"  # resource has the key in a different case
    TAG_LIMIT_EXCEEDED = "TAG_LIMIT_EXCEEDED"  # AWS allows at most 50 user tags per resource


class TagConflict(_Frozen):
    key: str
    reason: ConflictReason
    requested_value: str
    existing_key: str | None = None
    existing_value: str | None = None
    detail: str = ""


class PolicyViolation(_Frozen):
    key: str
    value: str
    policy_key: str
    allowed_values: list[str]
    aliases: dict[str, str]
    message: str


class ResourceRecord(_Frozen):
    """Everything CloudGuard knows about one resource for this request, with exactly one outcome."""

    outcome: ResourceOutcome
    resource_type: str
    resource_name: str
    resource_arn: str
    account_id: str
    region: str
    current_tags: dict[str, str] | None  # complete, as inspected; None if unknown or out of scope
    tags_inspected_at: datetime | None
    changes: list[TagChange] = Field(default_factory=list)
    matching: list[MatchingTag] = Field(default_factory=list)
    conflicts: list[TagConflict] = Field(default_factory=list)
    policy_violations: list[PolicyViolation] = Field(default_factory=list)
    inspection_error: InspectionError | None = None
    out_of_scope_reason: str | None = None
    projected_status: ComplianceStatus | None = None  # mandatory-policy status if changes applied
    projected_missing_tags: list[str] = Field(default_factory=list)

    @model_validator(mode="after")
    def _outcome_matches_details(self) -> ResourceRecord:
        o = self.outcome
        detail_lists = (self.changes, self.matching, self.conflicts, self.policy_violations)
        if o == ResourceOutcome.OUT_OF_SCOPE:
            if not self.out_of_scope_reason or any(detail_lists) or self.current_tags is not None or self.inspection_error:
                raise ValueError("OUT_OF_SCOPE records carry only a reason")
            return self
        if self.out_of_scope_reason:
            raise ValueError("only OUT_OF_SCOPE records carry out_of_scope_reason")
        if o == ResourceOutcome.INSPECTION_FAILED:
            if self.inspection_error is None or self.current_tags is not None or any(detail_lists):
                raise ValueError("INSPECTION_FAILED records carry only the inspection error")
            return self
        if self.inspection_error is not None or self.current_tags is None:
            raise ValueError(f"{o} records need inspected current_tags and no inspection_error")
        keys = [c.key for c in self.changes] + [m.key for m in self.matching] + \
               [c.key for c in self.conflicts] + [v.key for v in self.policy_violations]
        if len(keys) != len(set(keys)):
            raise ValueError("each requested tag appears in exactly one of changes/matching/conflicts/policy_violations")
        expected = {
            ResourceOutcome.POLICY_VIOLATION: bool(self.policy_violations) and not self.changes,
            ResourceOutcome.CHANGE_PROPOSED: bool(self.changes) and not self.policy_violations,
            ResourceOutcome.CONFLICT: bool(self.conflicts) and not self.changes and not self.policy_violations,
            ResourceOutcome.ALREADY_MATCHING: bool(self.matching) and not (self.changes or self.conflicts or self.policy_violations),
        }[o]
        if not expected:
            raise ValueError(f"record details are inconsistent with outcome {o}")
        return self


# ---------------------------------------------------------------- impact summary


class ChangeLine(_Frozen):
    resource_arn: str
    resource_name: str
    key: str
    action: ChangeAction
    old_value: str | None
    new_value: str


class ImpactSummary(_Frozen):
    """Derived solely from the proposal's records (validated by recomputation)."""

    resources_considered: int  # every record, including out-of-scope ones
    resources_matched: int  # records within the request's criteria
    resources_changing: int
    resources_already_matching: int
    resources_conflicting: int
    resources_policy_violations: int
    resources_inspection_failed: int
    resources_out_of_scope: int
    resources_changing_with_partial_conflicts: int
    tags_added: int
    tags_overwritten: int
    tag_conflicts: int
    tag_policy_violations: int
    conflicts_by_reason: dict[str, int]
    resource_arns_by_outcome: dict[str, list[str]]
    proposed_changes: list[ChangeLine]


def summarize_records(records: list[ResourceRecord]) -> ImpactSummary:
    by_outcome = {o.value: [r.resource_arn for r in records if r.outcome == o] for o in ResourceOutcome}
    changes = [c for r in records for c in r.changes]
    conflicts = [c for r in records for c in r.conflicts]
    reasons: dict[str, int] = {}
    for c in conflicts:
        reasons[c.reason.value] = reasons.get(c.reason.value, 0) + 1
    return ImpactSummary(
        resources_considered=len(records),
        resources_matched=len(records) - len(by_outcome[ResourceOutcome.OUT_OF_SCOPE]),
        resources_changing=len(by_outcome[ResourceOutcome.CHANGE_PROPOSED]),
        resources_already_matching=len(by_outcome[ResourceOutcome.ALREADY_MATCHING]),
        resources_conflicting=len(by_outcome[ResourceOutcome.CONFLICT]),
        resources_policy_violations=len(by_outcome[ResourceOutcome.POLICY_VIOLATION]),
        resources_inspection_failed=len(by_outcome[ResourceOutcome.INSPECTION_FAILED]),
        resources_out_of_scope=len(by_outcome[ResourceOutcome.OUT_OF_SCOPE]),
        resources_changing_with_partial_conflicts=sum(
            1 for r in records if r.outcome == ResourceOutcome.CHANGE_PROPOSED and r.conflicts),
        tags_added=sum(1 for c in changes if c.action == ChangeAction.ADD),
        tags_overwritten=sum(1 for c in changes if c.action == ChangeAction.OVERWRITE),
        tag_conflicts=len(conflicts),
        tag_policy_violations=sum(len(r.policy_violations) for r in records),
        conflicts_by_reason=dict(sorted(reasons.items())),
        resource_arns_by_outcome=by_outcome,
        proposed_changes=[
            ChangeLine(resource_arn=r.resource_arn, resource_name=r.resource_name, key=c.key, action=c.action,
                       old_value=c.old_value, new_value=c.new_value)
            for r in records for c in r.changes
        ],
    )


# ---------------------------------------------------------------- proposal


class ApprovalStatus(StrEnum):
    PENDING = "PENDING"  # approvable: has changes and no policy violations
    NOT_APPROVABLE = "NOT_APPROVABLE"  # nothing to change, or blocked by policy violations


class TaggingProposal(_Frozen):
    schema_version: str = PROPOSAL_SCHEMA
    proposal_id: str
    operation: TaggingOperation
    request: TaggingRequest
    account_id: str
    policy: PolicyRef
    created_at: datetime
    expires_at: datetime
    approval_status: ApprovalStatus  # initial state only; later transitions live in approval records
    blocking_reasons: list[str]
    records: list[ResourceRecord]  # one per considered resource
    impact: ImpactSummary
    requires_overwrite_confirmation: bool
    content_hash: str

    @model_validator(mode="after")
    def _integrity(self) -> TaggingProposal:
        if self.schema_version != PROPOSAL_SCHEMA:
            raise ValueError(f"unsupported proposal schema {self.schema_version!r}")
        if self.content_hash != proposal_hash(self):
            raise ValueError("proposal content_hash does not match its content")
        if not (self.created_at < self.expires_at <= self.created_at + MAX_PROPOSAL_TTL):
            raise ValueError(f"expires_at must be after created_at and within {MAX_PROPOSAL_TTL}")
        arns = [r.resource_arn for r in self.records]
        if len(arns) != len(set(arns)):
            raise ValueError("a resource may appear only once in a proposal")
        if any(r.account_id != self.account_id for r in self.records):
            raise ValueError("every record must belong to the proposal's account")
        if self.impact != summarize_records(self.records):
            raise ValueError("impact summary must be derived from the records")
        if not self.operation.is_update and self.impact.tags_overwritten:
            raise ValueError("add operations can never overwrite")
        if self.requires_overwrite_confirmation != (self.impact.tags_overwritten > 0):
            raise ValueError("requires_overwrite_confirmation must reflect OVERWRITE changes")
        if not self.operation.is_bulk and (self.impact.resources_matched != 1 or self.impact.resources_out_of_scope):
            raise ValueError("specific operations target exactly one resource")
        approvable = self.impact.resources_changing > 0 and self.impact.resources_policy_violations == 0
        if (self.approval_status == ApprovalStatus.PENDING) != approvable:
            raise ValueError("approval_status must be PENDING exactly when the proposal is approvable")
        if approvable == bool(self.blocking_reasons):
            raise ValueError("blocking_reasons must be present exactly when the proposal is not approvable")
        return self

    @property
    def is_approvable(self) -> bool:
        return self.approval_status == ApprovalStatus.PENDING

    def records_with(self, outcome: ResourceOutcome) -> list[ResourceRecord]:
        return [r for r in self.records if r.outcome == outcome]

    def approval_binding(self) -> dict:
        """What an authenticated approval must be bound to, and what execution must re-verify.

        `expected_state` lists, per change, the value the key must still have immediately before
        execution (None = must still be absent). Any drift invalidates the approval.
        """
        return {
            "schema_version": self.schema_version,
            "proposal_id": self.proposal_id,
            "content_hash": self.content_hash,
            "account_id": self.account_id,
            "policy": self.policy.model_dump(),
            "operation": self.operation.value,
            "expires_at": self.expires_at.isoformat(),
            "requires_overwrite_confirmation": self.requires_overwrite_confirmation,
            "expected_state": [
                {"resource_arn": r.resource_arn, "region": r.region, "resource_type": r.resource_type,
                 "key": c.key, "expected_current_value": c.old_value, "new_value": c.new_value}
                for r in self.records for c in r.changes
            ],
        }


def proposal_hash(p: TaggingProposal | dict) -> str:
    """SHA-256 over every field except content_hash itself."""
    data = p if isinstance(p, dict) else p.model_dump(mode="json")
    body = {k: v for k, v in data.items() if k != "content_hash"}
    return "sha256:" + hashlib.sha256(json.dumps(body, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


class OutcomeStatus(StrEnum):
    PROPOSAL_READY = "PROPOSAL_READY"  # approvable proposal (Phase 4 will handle approval)
    NO_CHANGES = "NO_CHANGES"  # proposal shows nothing would change (all matching/conflicting/failed)
    BLOCKED = "BLOCKED"  # proposal shows policy violations; not approvable
    NEEDS_CLARIFICATION = "NEEDS_CLARIFICATION"  # ambiguous or near-miss target
    NOT_FOUND = "NOT_FOUND"
    REJECTED = "REJECTED"  # request-level problem: unparseable, invalid tag, out of scope


class TaggingOutcome(_Frozen):
    status: OutcomeStatus
    message: str
    request: TaggingRequest | None = None
    proposal: TaggingProposal | None = None
    candidates: list[Candidate] = Field(default_factory=list)
    rejection: Rejection | None = None

    @model_validator(mode="after")
    def _shape(self) -> TaggingOutcome:
        with_proposal = (OutcomeStatus.PROPOSAL_READY, OutcomeStatus.NO_CHANGES, OutcomeStatus.BLOCKED)
        if (self.status in with_proposal) != (self.proposal is not None):
            raise ValueError(f"{self.status} outcome/proposal mismatch")
        if self.proposal is not None:
            p = self.proposal
            expected = (OutcomeStatus.PROPOSAL_READY if p.is_approvable
                        else OutcomeStatus.BLOCKED if p.impact.resources_policy_violations
                        else OutcomeStatus.NO_CHANGES)
            if self.status != expected:
                raise ValueError(f"outcome status {self.status} doesn't match proposal (expected {expected})")
        if (self.status == OutcomeStatus.REJECTED) != (self.rejection is not None):
            raise ValueError("REJECTED outcomes (only) carry a rejection")
        if self.status == OutcomeStatus.NEEDS_CLARIFICATION and not self.candidates:
            raise ValueError("clarification needs candidates to choose from")
        return self
