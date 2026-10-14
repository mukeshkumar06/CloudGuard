"""Human-readable scan report. Violations, notices and inspection failures are separate sections.

AWS-sourced strings (names, tag keys and values) are untrusted: they are shown quoted via repr()
and truncated, never interpreted.
"""

from __future__ import annotations

from cloudguard.governance.models import ComplianceStatus, GovernanceScan, NoticeKind


def _q(value: str, limit: int = 60) -> str:
    return repr(value if len(value) <= limit else value[:limit] + "...")


def _notice_text(n) -> str:
    if n.kind == NoticeKind.NON_CANONICAL_VALUE:
        return f"{n.key}: value {_q(n.value)} is accepted; canonical value is {_q(n.canonical_value)}"
    if n.kind == NoticeKind.KEY_CASE_MISMATCH:
        return f"{n.key}: key written as {_q(n.actual_keys[0])}"
    return f"{n.key}: multiple keys differ only by case: {', '.join(_q(k) for k in n.actual_keys)}"


def render_summary(scan: GovernanceScan) -> str:
    s = scan.summary
    inspected = s.compliant_resources + s.non_compliant_resources
    pct = f"{s.compliance_percentage}% of {inspected} inspected" if s.compliance_percentage is not None else "n/a"
    lines = [
        f"Scan {scan.scan_id} ({scan.mode}) | regions {', '.join(scan.regions)}",
        f"Policy {scan.policy.policy_id} v{scan.policy.version} ({scan.policy.content_hash[:19]}...)",
        f"Resources {s.total_resources}: compliant {s.compliant_resources}, non-compliant {s.non_compliant_resources}, "
        f"inspection failed {s.inspection_failed_resources} | compliance {pct}",
        "",
        f"VIOLATIONS ({s.total_violations}) - make a resource NON_COMPLIANT",
    ]
    for f in scan.findings:
        if f.compliance_status != ComplianceStatus.NON_COMPLIANT:
            continue
        parts = []
        if f.missing_tags:
            parts.append(f"missing {', '.join(f.missing_tags)}")
        for i in f.invalid_tags:
            kind = "mandatory" if i.mandatory else "optional"
            parts.append(f"invalid {kind} {i.key}={_q(i.value)} ({i.reason.value})")
        lines.append(f"  {f.resource_type} {_q(f.resource_name)}: {'; '.join(parts)}")
    if not s.total_violations:
        lines.append("  none")

    lines += ["", f"NOTICES ({s.total_notices}) - advisory, no effect on compliance, tags are never rewritten"]
    for f in scan.findings:
        for n in f.notices:
            lines.append(f"  {f.resource_type} {_q(f.resource_name)}: {_notice_text(n)}")
    if not s.total_notices:
        lines.append("  none")

    lines += ["", f"INSPECTION FAILURES ({s.inspection_failed_resources}) - tags unknown, not evaluated"]
    for f in scan.findings:
        if f.compliance_status == ComplianceStatus.INSPECTION_FAILED:
            e = f.inspection_error
            lines.append(f"  {f.resource_type} {_q(f.resource_name)}: {e.code} from {e.operation}")
    if not s.inspection_failed_resources:
        lines.append("  none")

    lines += ["", f"SERVICE ERRORS ({s.service_errors}) - resources of these services may be missing"]
    for e in scan.service_errors:
        lines.append(f"  {e.service} in {e.region}: {e.error.code} from {e.error.operation}")
    if not s.service_errors:
        lines.append("  none")
    return "\n".join(lines)
