"""Deterministic tag-compliance engine. No AI model is involved in deciding compliance."""

from __future__ import annotations

import hashlib
import uuid
from datetime import UTC, datetime

from cloudguard.governance.discovery import DiscoverySource
from cloudguard.governance.models import (
    ComplianceStatus,
    DiscoveredResource,
    Evidence,
    GovernanceScan,
    InvalidTag,
    InvalidTagReason,
    ScanSummary,
    ServiceError,
    TagFinding,
)
from cloudguard.governance.policy import TagPolicy


def finding_id_for(resource_arn: str) -> str:
    """Stable across scans so the same resource can be compared over time."""
    return "tagcomp-" + hashlib.sha256(resource_arn.encode()).hexdigest()[:16]


def evaluate(resource: DiscoveredResource, policy: TagPolicy, scanned_at: datetime) -> TagFinding:
    common = dict(
        finding_id=finding_id_for(resource.resource_arn),
        account_id=resource.account_id,
        resource_type=resource.resource_type,
        service=resource.service,
        resource_id=resource.resource_id,
        resource_name=resource.resource_name,
        resource_arn=resource.resource_arn,
        region=resource.region,
        scanned_at=scanned_at,
    )

    if resource.tags is None:
        err = resource.tag_error
        return TagFinding(
            **common,
            compliance_status=ComplianceStatus.INSPECTION_FAILED,
            existing_tags=None,
            missing_tags=[],
            invalid_tags=[],
            inspection_error=err,
            evidence=Evidence(
                source_api=err.operation,
                retrieved_at=resource.discovered_at,
                summary=f"Tags could not be retrieved ({err.code}); compliance was not evaluated.",
            ),
        )

    by_lower: dict[str, list[tuple[str, str]]] = {}
    for key, value in resource.tags.items():
        by_lower.setdefault(key.lower(), []).append((key, value))

    missing: list[str] = []
    invalid: list[InvalidTag] = []
    for req in policy.required_tags:
        matches = by_lower.get(req.key.lower(), [])
        if not matches:
            missing.append(req.key)
            continue
        non_empty = [(k, v) for k, v in matches if v.strip()]
        if not non_empty:
            k, v = matches[0]
            invalid.append(InvalidTag(key=req.key, actual_key=k, value=v, reason=InvalidTagReason.EMPTY_VALUE))
            continue
        if req.allowed_values is not None:
            allowed = {a.lower() for a in req.allowed_values}
            if not any(v.strip().lower() in allowed for _, v in non_empty):
                k, v = non_empty[0]
                invalid.append(InvalidTag(key=req.key, actual_key=k, value=v, reason=InvalidTagReason.VALUE_NOT_ALLOWED))

    total = len(policy.required_tags)
    satisfied = total - len(missing) - len(invalid)
    if not missing and not invalid:
        status = ComplianceStatus.COMPLIANT
        summary = f"All {total} mandatory tags are present with valid values."
    else:
        status = ComplianceStatus.NON_COMPLIANT
        parts = [f"{satisfied} of {total} mandatory tags are valid."]
        if missing:
            parts.append(f"Missing: {', '.join(missing)}.")
        if invalid:
            parts.append(f"Invalid: {', '.join(f'{i.key} ({i.reason.value})' for i in invalid)}.")
        summary = " ".join(parts)

    return TagFinding(
        **common,
        compliance_status=status,
        existing_tags=dict(resource.tags),
        missing_tags=missing,
        invalid_tags=invalid,
        evidence=Evidence(source_api=resource.tag_source, retrieved_at=resource.discovered_at, summary=summary),
    )


def summarize(findings: list[TagFinding], service_errors: list[ServiceError]) -> ScanSummary:
    compliant = sum(f.compliance_status == ComplianceStatus.COMPLIANT for f in findings)
    non_compliant = sum(f.compliance_status == ComplianceStatus.NON_COMPLIANT for f in findings)
    failed = sum(f.compliance_status == ComplianceStatus.INSPECTION_FAILED for f in findings)
    evaluated = compliant + non_compliant
    return ScanSummary(
        total_resources=len(findings),
        compliant_resources=compliant,
        non_compliant_resources=non_compliant,
        inspection_failed_resources=failed,
        compliance_percentage=round(100 * compliant / evaluated, 1) if evaluated else None,
        service_errors=len(service_errors),
    )


def run_scan(source: DiscoverySource, policy: TagPolicy) -> GovernanceScan:
    started = datetime.now(UTC)
    discovery = source.discover()
    findings = [evaluate(r, policy, started) for r in discovery.resources]
    return GovernanceScan(
        scan_id=f"scan-{started:%Y%m%dT%H%M%SZ}-{uuid.uuid4().hex[:6]}",
        account_id=discovery.account_id,
        regions=discovery.regions,
        mode=source.mode,
        started_at=started,
        completed_at=datetime.now(UTC),
        required_tags=policy.keys,
        summary=summarize(findings, discovery.service_errors),
        findings=findings,
        service_errors=discovery.service_errors,
    )
