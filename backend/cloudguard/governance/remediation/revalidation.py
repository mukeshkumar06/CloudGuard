"""Per-resource revalidation of an approved proposal against current resource state.

Pure decision logic: given the approved record, a fresh snapshot, the baseline immutable identity
and the ORIGINAL policy rules, decide what (if anything) may still be written. It never alters
the approved intent: it can only narrow it (skip resources, or skip changes already applied).

Rules (in order):
 1. the resource must exist and its ARN, account and region must match the approved record;
 2. where a baseline immutable identity was captured, it must still match (else RESOURCE_RECREATED);
    Lambda has no universal immutable identifier, so recreation under the same name is undetectable;
 3. CloudGuard-protected resources (cloudguard:protected=true, any case) are never written;
 4. every change must still pass AWS tag constraints and the ORIGINAL policy version;
 5. per change: ADD requires the key to still be absent (no case variant either); OVERWRITE
    requires the key to still hold the approved previous value. A key already at the approved
    final value counts as applied. Anything else is TAG_DRIFT for the whole resource;
 6. S3 replaces the entire tag set, so for buckets every unrelated tag must also be unchanged;
 7. existing user tags + pending additions must stay within AWS's 50-tag limit.
Unrelated tags on Lambda/DynamoDB/EC2 may change without blocking (they are never touched).
"""

from __future__ import annotations

from cloudguard.governance.custom_tagging.models import ChangeAction, ResourceRecord
from cloudguard.governance.custom_tagging.validation import MAX_TAGS_PER_RESOURCE, classify_and_check, validate_tag
from cloudguard.governance.policy import TagRule
from cloudguard.governance.remediation.execution_models import (
    ApprovedChange,
    RevalidationResult,
)
from cloudguard.governance.remediation.execution_models import RevalidationStatus as RS
from cloudguard.governance.remediation.models import canonical_digest
from cloudguard.governance.remediation.writers import ResourceSnapshot

PROTECTED_MARKER = "cloudguard:protected"


def approved_changes(record: ResourceRecord) -> list[ApprovedChange]:
    return [ApprovedChange(key=c.key, action=c.action, expected_current=c.old_value, new_value=c.new_value)
            for c in record.changes]


def _user_tag_count(tags: dict[str, str]) -> int:
    return sum(1 for k in tags if not k.lower().startswith("aws:"))


def revalidate(record: ResourceRecord, snapshot: ResourceSnapshot, *, account_id: str, baseline_uid: str | None,
               original_rules: list[TagRule], attempts: int = 1) -> RevalidationResult:
    base = dict(resource_arn=record.resource_arn, resource_type=record.resource_type, region=record.region,
                baseline_uid=baseline_uid, request_id=snapshot.request_id, attempts=attempts)

    def result(status: RS, detail: str, **extra) -> RevalidationResult:
        return RevalidationResult(status=status, detail=detail, **base, **extra)

    if not snapshot.exists:
        return result(RS.RESOURCE_MISSING, "resource no longer exists")
    observed = dict(observed_uid=snapshot.uid,
                    observed_tags_digest=canonical_digest(snapshot.tags) if snapshot.tags is not None else None)
    if snapshot.arn != record.resource_arn or snapshot.resource_type != record.resource_type:
        return result(RS.RESOURCE_RECREATED, "resource identity (ARN/type) differs from the approved record", **observed)
    if snapshot.account_id != account_id or record.account_id != account_id:
        return result(RS.ACCOUNT_MISMATCH, f"resource account {snapshot.account_id} is not {account_id}", **observed)
    if snapshot.region != record.region:
        return result(RS.REGION_MISMATCH, f"resource is in {snapshot.region}, approved for {record.region}", **observed)

    note = None
    if baseline_uid is None:
        note = ("no immutable identifier captured; recreation under the same ARN cannot be detected"
                if snapshot.uid_kind is None else "no baseline identity was captured at proposal time")
    elif snapshot.uid != baseline_uid:
        return result(RS.RESOURCE_RECREATED, f"{snapshot.uid_kind} changed since approval", identity_note=None, **observed)
    observed["identity_note"] = note

    tags = snapshot.tags or {}
    if any(k.lower() == PROTECTED_MARKER and v.strip().lower() == "true" for k, v in tags.items()):
        return result(RS.PROTECTED_TAG, "resource is marked cloudguard:protected", **observed)

    changes = approved_changes(record)
    for c in changes:
        if validate_tag(c.key, c.new_value) is not None:
            return result(RS.PROTECTED_TAG, f"tag {c.key!r} is not writable (reserved or invalid)", **observed)
        _, violation, _ = classify_and_check(c.key, c.new_value, original_rules)
        if violation is not None:
            return result(RS.POLICY_CHANGED, f"{c.key}={c.new_value!r} violates the approved policy version", **observed)

    pending: list[ApprovedChange] = []
    applied: list[str] = []
    for c in changes:
        present = c.key in tags
        if present and tags[c.key] == c.new_value:
            applied.append(c.key)
            continue
        variants = [k for k in tags if k.lower() == c.key.lower() and k != c.key]
        if c.action == ChangeAction.ADD:
            if present:
                return result(RS.TAG_DRIFT, f"{c.key!r} now exists with another value; adding would overwrite it",
                              **observed)
            if variants:
                return result(RS.TAG_DRIFT, f"{c.key!r} now exists as {variants[0]!r}", **observed)
        else:  # OVERWRITE
            if not present:
                return result(RS.TAG_DRIFT, f"{c.key!r} no longer exists; an update cannot add it", **observed)
            if tags[c.key] != c.expected_current:
                return result(RS.TAG_DRIFT, f"{c.key!r} changed since approval", **observed)
        pending.append(c)

    if record.resource_type == "AWS::S3::Bucket":
        touched = {c.key for c in changes}
        before = {k: v for k, v in (record.current_tags or {}).items() if k not in touched}
        now = {k: v for k, v in tags.items() if k not in touched}
        if before != now:
            return result(RS.TAG_DRIFT, "S3 replaces the whole tag set and other bucket tags changed since approval",
                          **observed)

    if not pending:
        return result(RS.ALREADY_APPLIED, "every approved change is already present", applied_keys=applied, **observed)
    additions = sum(1 for c in pending if c.key not in tags)
    if _user_tag_count(tags) + additions > MAX_TAGS_PER_RESOURCE:
        return result(RS.TAG_LIMIT_EXCEEDED, f"{_user_tag_count(tags)} existing + {additions} new tags exceeds "
                      f"{MAX_TAGS_PER_RESOURCE}", **observed)
    return result(RS.ELIGIBLE, f"{len(pending)} change(s) pending" + (f", {len(applied)} already applied" if applied else ""),
                  pending=pending, applied_keys=applied, **observed)
