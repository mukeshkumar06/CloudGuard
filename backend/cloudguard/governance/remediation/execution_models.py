"""Execution models (Checkpoint 4.3, fixture-only).

- ExecutionRecord: immutable record of one execution of one approved proposal.
- ExecutionLease: the single mutable item; serialises executors and fences stale ones (token).
- ExecutionEvent: append-only, hash-chained audit of revalidation, write intents and per-resource
  outcomes. Every write is preceded by a fenced WRITE_INTENT event (Batch 2 / M2), so a write can
  never happen without an audit record that announced it.
Nothing here describes changes the caller supplied: every change is derived from the stored proposal.
"""

from __future__ import annotations

from datetime import datetime
from enum import StrEnum

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_serializer, model_validator

from cloudguard.governance.custom_tagging.models import ChangeAction
from cloudguard.governance.remediation.models import (
    Actor,
    IdentityEvidence,
    OperatingMode,
    ProposalIntegrityError,
    ProposalState,
    _utc,
    canonical_digest,
)

EXECUTION_SCHEMA = "cloudguard.execution/v1"


class _Frozen(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")


class ExecutionIntegrityError(ProposalIntegrityError):
    pass


class RevalidationStatus(StrEnum):
    ELIGIBLE = "ELIGIBLE"
    ALREADY_APPLIED = "ALREADY_APPLIED"
    RESOURCE_MISSING = "RESOURCE_MISSING"
    RESOURCE_RECREATED = "RESOURCE_RECREATED"
    TAG_DRIFT = "TAG_DRIFT"
    PROTECTED_TAG = "PROTECTED_TAG"
    TAG_LIMIT_EXCEEDED = "TAG_LIMIT_EXCEEDED"
    POLICY_CHANGED = "POLICY_CHANGED"
    ACCOUNT_MISMATCH = "ACCOUNT_MISMATCH"
    REGION_MISMATCH = "REGION_MISMATCH"
    UNVERIFIABLE = "UNVERIFIABLE"  # reads kept failing transiently (throttling / server errors)
    INSPECTION_FAILED = "INSPECTION_FAILED"  # tags could not be read (e.g. access denied)


class ApprovedChange(_Frozen):
    """One approved tag change, taken verbatim from a CHANGE_PROPOSED record."""

    key: str
    action: ChangeAction
    expected_current: str | None  # None: key must be absent (ADD)
    new_value: str


class RevalidationResult(_Frozen):
    resource_arn: str
    resource_type: str
    region: str
    status: RevalidationStatus
    pending: list[ApprovedChange] = Field(default_factory=list)  # still to apply (ELIGIBLE only)
    applied_keys: list[str] = Field(default_factory=list)  # already at the approved final value
    detail: str = ""
    baseline_uid: str | None = None
    observed_uid: str | None = None
    identity_note: str | None = None
    observed_tags_digest: str | None = None
    request_id: str | None = None
    attempts: int = 1

    @model_validator(mode="after")
    def _shape(self) -> RevalidationResult:
        if (self.status == RevalidationStatus.ELIGIBLE) != bool(self.pending):
            raise ValueError("exactly ELIGIBLE results carry pending changes")
        return self


class ResultStatus(StrEnum):
    APPLIED_VERIFIED = "APPLIED_VERIFIED"
    ALREADY_APPLIED = "ALREADY_APPLIED"
    SKIPPED = "SKIPPED"  # not eligible at revalidation; nothing written
    FAILED_WRITE = "FAILED_WRITE"
    FAILED_VERIFICATION = "FAILED_VERIFICATION"  # written, but the read-back did not confirm it
    NOT_ATTEMPTED = "NOT_ATTEMPTED"  # execution abandoned before reaching the resource
    UNCONFIRMED_WRITE = "UNCONFIRMED_WRITE"  # a write intent was recorded but its outcome never was


class ErrorCategory(StrEnum):
    NONE = "NONE"
    THROTTLED = "THROTTLED"
    TRANSIENT = "TRANSIENT"
    ACCESS_DENIED = "ACCESS_DENIED"
    NOT_FOUND = "NOT_FOUND"
    INELIGIBLE = "INELIGIBLE"
    VERIFICATION = "VERIFICATION"
    ABANDONED = "ABANDONED"
    UNKNOWN = "UNKNOWN"


class ResourceResult(_Frozen):
    resource_arn: str
    status: ResultStatus
    revalidation: RevalidationStatus | None
    error_category: ErrorCategory = ErrorCategory.NONE
    detail: str = ""
    attempts: int = 0
    request_ids: list[str] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)
    written_keys: list[str] = Field(default_factory=list)

    @property
    def succeeded(self) -> bool:
        return self.status in (ResultStatus.APPLIED_VERIFIED, ResultStatus.ALREADY_APPLIED)


class ExecutionSummary(_Frozen):
    outcome: ProposalState  # SUCCEEDED | PARTIAL | FAILED | ABORTED
    intended: int
    succeeded: int
    already_applied: int
    skipped: int
    failed: int
    not_attempted: int  # never reached; excludes in-flight and unconfirmed writes (A1)
    unconfirmed: int = 0  # a write may have been issued but its outcome is unknown: in_flight + UNCONFIRMED_WRITE
    in_flight: list[str] = Field(default_factory=list)  # ABORTED only: write intents with no recorded outcome

    @model_validator(mode="after")
    def _outcome(self) -> ExecutionSummary:
        if self.outcome not in (ProposalState.SUCCEEDED, ProposalState.PARTIAL, ProposalState.FAILED,
                                ProposalState.ABORTED):
            raise ValueError("execution outcome must be SUCCEEDED, PARTIAL, FAILED or ABORTED")
        if self.in_flight and self.outcome != ProposalState.ABORTED:
            raise ValueError("only an aborted execution can end with writes in flight")
        if self.succeeded + self.skipped + self.failed + self.not_attempted + self.unconfirmed != self.intended:
            raise ValueError("every intended resource is counted exactly once")
        return self


def summarize(intended: list[str], results: dict[str, ResourceResult], *, aborted: bool = False,
              in_flight: list[str] | tuple[str, ...] = ()) -> ExecutionSummary:
    final = [results[a] for a in intended if a in results]
    unrecorded = [a for a in intended if a not in results]
    if not set(in_flight) <= set(unrecorded):
        raise ValueError("in-flight resources must be intended resources without a recorded result")
    ok = sum(r.succeeded for r in final)
    outcome = (ProposalState.ABORTED if aborted
               else ProposalState.SUCCEEDED if ok == len(intended) and intended
               else ProposalState.PARTIAL if ok else ProposalState.FAILED)
    return ExecutionSummary(
        outcome=outcome, intended=len(intended), succeeded=ok,
        already_applied=sum(r.status == ResultStatus.ALREADY_APPLIED for r in final),
        skipped=sum(r.status == ResultStatus.SKIPPED for r in final),
        failed=sum(r.status in (ResultStatus.FAILED_WRITE, ResultStatus.FAILED_VERIFICATION) for r in final),
        # A1: an in-flight or unconfirmed write is neither a success, a failure nor "not attempted".
        not_attempted=sum(r.status == ResultStatus.NOT_ATTEMPTED for r in final) + len(unrecorded) - len(in_flight),
        unconfirmed=sum(r.status == ResultStatus.UNCONFIRMED_WRITE for r in final) + len(in_flight),
        in_flight=sorted(in_flight),
    )


class ExecutionRecord(_Frozen):
    schema_version: str = EXECUTION_SCHEMA
    execution_id: str
    proposal_id: str
    approval_id: str
    content_hash: str
    binding_digest: str
    account_id: str
    mode: OperatingMode
    requested_by: Actor
    approver: Actor
    executor_identity: IdentityEvidence  # M3: the person who started the execution
    approver_identity: IdentityEvidence  # M3: the person who approved it (from the approval record)
    started_at: datetime
    approval_expires_at: datetime  # Batch 2 (M1/L2): no write is ever issued at or after this instant
    idempotency_key: str
    request_fingerprint: str
    intended_resources: list[str]  # CHANGE_PROPOSED ARNs of the stored proposal, in order
    # Copied from an approval made under a PersonalAccountWaiver (personal voice tagging): the one person may also
    # execute. Omitted from serialization when unset, so every other record's form and hash are unchanged.
    separation_waived: str | None = None

    @model_serializer(mode="wrap")
    def _omit_unset_waiver(self, handler):
        data = handler(self)
        if data.get("separation_waived") is None:
            data.pop("separation_waived", None)
        return data

    @field_validator("started_at", "approval_expires_at")
    @classmethod
    def _utc_only(cls, v: datetime) -> datetime:
        return _utc(v)

    @model_validator(mode="after")
    def _consistent(self) -> ExecutionRecord:
        if not self.started_at < self.approval_expires_at:
            raise ValueError("an execution must start before its approval expires")
        if self.executor_identity.person_id == self.approver_identity.person_id and not self.separation_waived:
            raise ValueError("separation of duties: the executor must not be the approver")
        if self.schema_version != EXECUTION_SCHEMA:
            raise ValueError("unsupported execution schema")
        # Checkpoint 5: a LIVE execution only ever records AWS-authenticated people on both sides.
        if self.mode == OperatingMode.LIVE and not (self.requested_by.authenticated and self.approver.authenticated):
            raise ValueError("a live execution needs AWS-authenticated requester and approver")
        if not self.intended_resources or len(set(self.intended_resources)) != len(self.intended_resources):
            raise ValueError("intended resources must be non-empty and unique")
        return self

    def record_hash(self) -> str:
        return canonical_digest(self.model_dump(mode="json"))


class ExecutionLease(_Frozen):
    execution_id: str
    proposal_id: str
    owner: str
    token: int = Field(ge=1)  # fencing token: every write must present the current token
    acquired_at: datetime
    expires_at: datetime
    released: bool = False

    @field_validator("acquired_at", "expires_at")
    @classmethod
    def _utc_only(cls, v: datetime) -> datetime:
        return _utc(v)

    def held_at(self, now: datetime) -> bool:
        return not self.released and now < self.expires_at


class ExecutionEventType(StrEnum):
    REVALIDATED = "REVALIDATED"
    WRITE_INTENT = "WRITE_INTENT"  # Batch 2 (M2): fenced announcement of exactly one upcoming write
    RESOURCE_RESULT = "RESOURCE_RESULT"
    LEASE_RECOVERED = "LEASE_RECOVERED"
    FINALIZED = "FINALIZED"
    ABORTED = "ABORTED"  # Batch 2 (M1): written by the ABORT decision; ends the execution


class ExecutionEvent(_Frozen):
    execution_id: str
    proposal_id: str
    seq: int = Field(ge=1)
    event_type: ExecutionEventType
    at: datetime
    lease_token: int = Field(ge=1)
    actor: Actor
    resource_arn: str | None = None
    revalidation: RevalidationResult | None = None
    result: ResourceResult | None = None
    summary: ExecutionSummary | None = None
    intent_changes: list[ApprovedChange] | None = None  # WRITE_INTENT: exactly the changes about to be written
    write_deadline: datetime | None = None  # WRITE_INTENT: the write must be issued before this instant
    note: str = ""
    prev_hash: str
    event_hash: str = ""

    @field_validator("at", "write_deadline")
    @classmethod
    def _utc_only(cls, v: datetime | None) -> datetime | None:
        return None if v is None else _utc(v)

    @model_validator(mode="after")
    def _shape(self) -> ExecutionEvent:
        payload = {ExecutionEventType.REVALIDATED: self.revalidation, ExecutionEventType.RESOURCE_RESULT: self.result,
                   ExecutionEventType.FINALIZED: self.summary, ExecutionEventType.ABORTED: self.summary,
                   ExecutionEventType.WRITE_INTENT: self.intent_changes,
                   ExecutionEventType.LEASE_RECOVERED: True}[self.event_type]
        if payload is None:
            raise ValueError(f"{self.event_type} events need their payload")
        if self.event_type in (ExecutionEventType.REVALIDATED, ExecutionEventType.RESOURCE_RESULT):
            inner = self.revalidation or self.result
            if self.resource_arn is None or inner.resource_arn != self.resource_arn:
                raise ValueError("resource events must name their resource consistently")
        is_intent = self.event_type == ExecutionEventType.WRITE_INTENT
        if is_intent != (self.write_deadline is not None):
            raise ValueError("exactly WRITE_INTENT events carry a write deadline")
        if is_intent and (not self.intent_changes or self.resource_arn is None or self.write_deadline <= self.at):
            raise ValueError("a WRITE_INTENT names its resource, its changes and a future write deadline")
        if self.event_type == ExecutionEventType.ABORTED and self.summary.outcome != ProposalState.ABORTED:
            raise ValueError("an ABORTED event carries an ABORTED summary")
        if self.event_type == ExecutionEventType.FINALIZED and self.summary.outcome == ProposalState.ABORTED:
            raise ValueError("FINALIZED events never carry an ABORTED summary")
        if self.event_hash and self.event_hash != self.compute_hash():
            raise ValueError("execution event hash does not match its content")
        return self

    def compute_hash(self) -> str:
        return canonical_digest(self.model_dump(mode="json", exclude={"event_hash"}))

    @classmethod
    def sealed(cls, **fields) -> ExecutionEvent:
        draft = cls.model_construct(**{**fields, "event_hash": ""})
        validated = cls.model_validate(draft.model_dump(mode="json"))
        return validated.model_copy(update={"event_hash": validated.compute_hash()})


class ExecutionView(_Frozen):
    record: ExecutionRecord
    lease: ExecutionLease
    events: list[ExecutionEvent]
    results: dict[str, ResourceResult]  # latest per resource
    summary: ExecutionSummary | None  # set once FINALIZED

    @property
    def finished(self) -> bool:
        return self.summary is not None
