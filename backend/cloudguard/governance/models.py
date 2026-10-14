"""Data models for tag-governance discovery and compliance.

Everything here is produced by deterministic code. Agents may read these models but never construct
or mutate findings.
"""

from __future__ import annotations

from datetime import datetime
from enum import StrEnum

from pydantic import BaseModel, ConfigDict, Field, model_validator


class ErrorKind(StrEnum):
    ACCESS_DENIED = "ACCESS_DENIED"
    THROTTLED = "THROTTLED"
    NOT_FOUND = "NOT_FOUND"
    SERVICE_UNAVAILABLE = "SERVICE_UNAVAILABLE"
    UNKNOWN = "UNKNOWN"


class ComplianceStatus(StrEnum):
    COMPLIANT = "COMPLIANT"
    NON_COMPLIANT = "NON_COMPLIANT"
    INSPECTION_FAILED = "INSPECTION_FAILED"


class InvalidTagReason(StrEnum):
    EMPTY_VALUE = "EMPTY_VALUE"
    VALUE_NOT_ALLOWED = "VALUE_NOT_ALLOWED"


class _Frozen(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")


class InspectionError(_Frozen):
    """An AWS API failure, kept separate from 'resource has no tags'."""

    kind: ErrorKind
    code: str
    message: str
    operation: str


class DiscoveredResource(_Frozen):
    """A resource found by service-specific discovery, with its raw tags (or why they are unknown)."""

    account_id: str
    resource_type: str  # CloudFormation-style type, e.g. "AWS::Lambda::Function"
    service: str
    resource_id: str
    resource_name: str
    resource_arn: str
    region: str
    discovered_at: datetime
    tags: dict[str, str] | None = None  # None means the tags could not be retrieved
    tag_source: str  # IAM action that produced the tags, e.g. "lambda:ListTags"
    tag_error: InspectionError | None = None

    @model_validator(mode="after")
    def _tags_xor_error(self) -> DiscoveredResource:
        if (self.tags is None) == (self.tag_error is None):
            raise ValueError("exactly one of tags or tag_error must be set")
        return self


class ServiceError(_Frozen):
    """Listing a whole service failed; resources of that service may be missing from the scan."""

    service: str
    region: str
    error: InspectionError


class DiscoveryResult(_Frozen):
    account_id: str
    regions: list[str]
    resources: list[DiscoveredResource]
    service_errors: list[ServiceError] = Field(default_factory=list)


class InvalidTag(_Frozen):
    key: str  # policy key
    actual_key: str  # key as written on the resource
    value: str
    reason: InvalidTagReason


class Evidence(_Frozen):
    source_api: str
    retrieved_at: datetime
    summary: str


class TagFinding(_Frozen):
    finding_id: str  # stable per resource ARN, so scans can be compared over time
    account_id: str
    resource_type: str
    service: str
    resource_id: str
    resource_name: str
    resource_arn: str
    region: str
    compliance_status: ComplianceStatus
    existing_tags: dict[str, str] | None
    missing_tags: list[str]
    invalid_tags: list[InvalidTag]
    evidence: Evidence
    inspection_error: InspectionError | None = None
    scanned_at: datetime


class ScanSummary(_Frozen):
    total_resources: int
    compliant_resources: int
    non_compliant_resources: int
    inspection_failed_resources: int
    # compliant / (compliant + non_compliant) * 100. Inspection failures are excluded from the
    # denominator because their compliance is unknown. None when nothing could be evaluated.
    compliance_percentage: float | None
    service_errors: int


class GovernanceScan(_Frozen):
    scan_id: str
    account_id: str
    regions: list[str]
    mode: str  # "live" or "fixture"
    started_at: datetime
    completed_at: datetime
    required_tags: list[str]
    summary: ScanSummary
    findings: list[TagFinding]
    service_errors: list[ServiceError]
