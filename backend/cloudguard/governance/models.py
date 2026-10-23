"""Data models for tag-governance discovery and compliance.

Everything here is produced by deterministic code. Agents may read these models but never construct
or mutate findings.
"""

from __future__ import annotations

from datetime import datetime
from enum import StrEnum

from pydantic import BaseModel, ConfigDict, Field, model_validator


SUPPORTED_RESOURCE_TYPES = ("AWS::Lambda::Function", "AWS::DynamoDB::Table", "AWS::S3::Bucket", "AWS::EC2::Instance")

# Region recorded for an S3 bucket whose region AWS would not tell us. Such a bucket is reported
# once per scan (never once per scanned region).
UNRESOLVED_REGION = "unresolved"


class ErrorKind(StrEnum):
    ACCESS_DENIED = "ACCESS_DENIED"
    THROTTLED = "THROTTLED"
    NOT_FOUND = "NOT_FOUND"
    SERVICE_UNAVAILABLE = "SERVICE_UNAVAILABLE"
    TIMEOUT = "TIMEOUT"  # discovery did not finish within its deadline; results for that service are incomplete
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
    mandatory: bool  # False for an optional rule whose present value is invalid


class NoticeKind(StrEnum):
    # Valid, but not the canonical spelling (an alias like "prod", or "production" vs "Production").
    NON_CANONICAL_VALUE = "NON_CANONICAL_VALUE"
    # The resource's key differs from the policy key only by case ("environment" vs "Environment").
    KEY_CASE_MISMATCH = "KEY_CASE_MISMATCH"
    # The resource has several keys that differ only by case ("Owner" and "owner").
    DUPLICATE_KEY_CASE = "DUPLICATE_KEY_CASE"


class TagNotice(_Frozen):
    """Advisory observation. Notices never change compliance status and never trigger tag rewrites."""

    kind: NoticeKind
    key: str  # policy key
    actual_keys: list[str]
    value: str | None = None
    canonical_value: str | None = None


class PolicyRef(_Frozen):
    """The exact policy version a scan was evaluated against."""

    policy_id: str
    version: int
    content_hash: str


class Evidence(_Frozen):
    source_api: str
    retrieved_at: datetime
    summary: str


class TagFinding(_Frozen):
    """One resource's result. Three categories are kept strictly apart:

    - violations (`missing_tags`, `invalid_tags`): the only thing that makes a resource NON_COMPLIANT
    - notices (`notices`): advisory only, never change status
    - inspection failure (`inspection_error`): tags unknown, so nothing else is reported
    """

    finding_id: str  # stable per resource ARN, so scans can be compared over time
    account_id: str
    resource_type: str
    service: str
    resource_id: str
    resource_name: str
    resource_arn: str
    region: str
    compliance_status: ComplianceStatus
    required_tags: list[str]  # effective mandatory keys for this resource type under the policy
    existing_tags: dict[str, str] | None
    missing_tags: list[str]
    invalid_tags: list[InvalidTag]
    notices: list[TagNotice] = Field(default_factory=list)
    evidence: Evidence
    inspection_error: InspectionError | None = None
    scanned_at: datetime

    @property
    def violation_count(self) -> int:
        return len(self.missing_tags) + len(self.invalid_tags)

    @model_validator(mode="after")
    def _categories_are_consistent(self) -> TagFinding:
        status = self.compliance_status
        if status == ComplianceStatus.INSPECTION_FAILED:
            if self.inspection_error is None or self.existing_tags is not None:
                raise ValueError("INSPECTION_FAILED requires inspection_error and no existing_tags")
            if self.violation_count or self.notices:
                raise ValueError("INSPECTION_FAILED findings cannot carry violations or notices")
        else:
            if self.inspection_error is not None or self.existing_tags is None:
                raise ValueError(f"{status} findings require existing_tags and no inspection_error")
            if status == ComplianceStatus.COMPLIANT and self.violation_count:
                raise ValueError("COMPLIANT findings cannot carry violations")
            if status == ComplianceStatus.NON_COMPLIANT and not self.violation_count:
                raise ValueError("NON_COMPLIANT findings need at least one violation")
        return self


class ScanSummary(_Frozen):
    total_resources: int
    compliant_resources: int
    non_compliant_resources: int
    inspection_failed_resources: int
    # compliant / (compliant + non_compliant) * 100. Inspection failures are excluded from the
    # denominator because their compliance is unknown. None when nothing could be evaluated.
    compliance_percentage: float | None
    total_violations: int  # missing + invalid tags across all findings
    total_notices: int  # advisory notices; never affect compliance
    resources_with_notices: int
    service_errors: int


class GovernanceScan(_Frozen):
    scan_id: str
    account_id: str
    regions: list[str]
    mode: str  # "live" or "fixture"
    started_at: datetime
    completed_at: datetime
    policy: PolicyRef
    summary: ScanSummary
    findings: list[TagFinding]
    service_errors: list[ServiceError]
