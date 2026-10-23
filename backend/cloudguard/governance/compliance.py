"""Deterministic tag-compliance engine. No AI model is involved in deciding compliance.

Each resource is evaluated against the effective rules for its resource type (policy defaults plus
overrides). Notices (aliases, key-case variants, duplicate keys) are advisory: they never change
status, and nothing here ever modifies tags.
"""

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
    NoticeKind,
    ScanSummary,
    ServiceError,
    TagFinding,
    TagNotice,
)
from cloudguard.governance.policy import PolicyVersion, TagRule


def finding_id_for(resource_arn: str) -> str:
    """Stable across scans so the same resource can be compared over time."""
    return "tagcomp-" + hashlib.sha256(resource_arn.encode()).hexdigest()[:16]


def _check_rule(
    rule: TagRule, matches: list[tuple[str, str]]
) -> tuple[bool, InvalidTag | None, list[TagNotice]]:
    """Returns (missing, invalid, notices) for one rule against the resource's matching tags."""
    notices: list[TagNotice] = []
    if not matches:
        return rule.required, None, notices
    actual_keys = [k for k, _ in matches]
    if len(matches) > 1:
        notices.append(TagNotice(kind=NoticeKind.DUPLICATE_KEY_CASE, key=rule.key, actual_keys=actual_keys))
    elif actual_keys[0] != rule.key:
        notices.append(TagNotice(kind=NoticeKind.KEY_CASE_MISMATCH, key=rule.key, actual_keys=actual_keys))

    non_empty = [(k, v) for k, v in matches if v.strip()]
    if not non_empty:
        k, v = matches[0]
        return False, InvalidTag(key=rule.key, actual_key=k, value=v, reason=InvalidTagReason.EMPTY_VALUE,
                                 mandatory=rule.required), notices

    if rule.allowed_values:
        valid = [(k, v, c) for k, v in non_empty if (c := rule.canonical_value(v)) is not None]
        if not valid:
            k, v = non_empty[0]
            return False, InvalidTag(key=rule.key, actual_key=k, value=v, reason=InvalidTagReason.VALUE_NOT_ALLOWED,
                                     mandatory=rule.required), notices
        k, v, canonical = valid[0]
        if v != canonical:
            notices.append(
                TagNotice(kind=NoticeKind.NON_CANONICAL_VALUE, key=rule.key, actual_keys=[k], value=v, canonical_value=canonical)
            )
    return False, None, notices


def evaluate(resource: DiscoveredResource, rules: list[TagRule], scanned_at: datetime) -> TagFinding:
    required = [r.key for r in rules if r.required]
    common = dict(
        finding_id=finding_id_for(resource.resource_arn),
        account_id=resource.account_id,
        resource_type=resource.resource_type,
        service=resource.service,
        resource_id=resource.resource_id,
        resource_name=resource.resource_name,
        resource_arn=resource.resource_arn,
        region=resource.region,
        required_tags=required,
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
    notices: list[TagNotice] = []
    for rule in rules:
        is_missing, bad, rule_notices = _check_rule(rule, by_lower.get(rule.key.lower(), []))
        if is_missing:
            missing.append(rule.key)
        if bad:
            invalid.append(bad)
        notices.extend(rule_notices)

    invalid_keys = {i.key for i in invalid}
    valid_required = sum(1 for k in required if k not in missing and k not in invalid_keys)
    if not missing and not invalid:
        status = ComplianceStatus.COMPLIANT
        summary = f"All {len(required)} mandatory tags are present with valid values."
    else:
        status = ComplianceStatus.NON_COMPLIANT
        parts = [f"{valid_required} of {len(required)} mandatory tags are valid."]
        if missing:
            parts.append(f"Missing mandatory: {', '.join(missing)}.")
        bad_mandatory = [i for i in invalid if i.mandatory]
        bad_optional = [i for i in invalid if not i.mandatory]
        if bad_mandatory:
            parts.append(f"Invalid mandatory: {', '.join(f'{i.key} ({i.reason.value})' for i in bad_mandatory)}.")
        if bad_optional:
            parts.append(f"Invalid optional: {', '.join(f'{i.key} ({i.reason.value})' for i in bad_optional)}.")
        summary = " ".join(parts)

    return TagFinding(
        **common,
        compliance_status=status,
        existing_tags=dict(resource.tags),
        missing_tags=missing,
        invalid_tags=invalid,
        notices=notices,
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
        total_violations=sum(f.violation_count for f in findings),
        total_notices=sum(len(f.notices) for f in findings),
        resources_with_notices=sum(1 for f in findings if f.notices),
        service_errors=len(service_errors),
    )


def run_scan(source: DiscoverySource, policy: PolicyVersion, regions: list[str] | None = None,
             services: list[str] | None = None) -> GovernanceScan:
    """Discover (everything, or only `regions` / `services`) and evaluate each resource against `policy`."""
    started = datetime.now(UTC)
    scope = {k: v for k, v in (("regions", regions), ("services", services)) if v}
    discovery = source.discover(**scope)
    findings = [evaluate(r, policy.document.rules_for(r.resource_type), started) for r in discovery.resources]
    return GovernanceScan(
        scan_id=f"scan-{started:%Y%m%dT%H%M%SZ}-{uuid.uuid4().hex[:6]}",
        account_id=discovery.account_id,
        regions=discovery.regions,
        mode=source.mode,
        started_at=started,
        completed_at=datetime.now(UTC),
        policy=policy.ref,
        summary=summarize(findings, discovery.service_errors),
        findings=findings,
        service_errors=discovery.service_errors,
    )
