"""Server-side tag validation, independent of how a request was parsed.

AWS constraints use the intersection that holds for Lambda, DynamoDB, S3 and EC2:
- keys 1-128 characters, values 0-256 (CloudGuard additionally requires a non-blank value)
- characters: Unicode letters, digits, whitespace and _ . : / = + - @
- the `aws:` prefix is reserved
- at most 50 user tags per resource
"""

from __future__ import annotations

import re

from cloudguard.governance.custom_tagging.models import PolicyViolation, Rejection, RejectionCode, TagCategory
from cloudguard.governance.policy import MAX_KEY_LEN, MAX_VALUE_LEN, TagRule

MAX_TAGS_PER_RESOURCE = 50
RESERVED_PREFIX = "cloudguard:"  # CloudGuard's own markers; never writable through a tagging request
_ALLOWED = re.compile(r"^[\w\s.:/=+\-@]*$", re.UNICODE)


def validate_tag(key: str, value: str) -> Rejection | None:
    if not key or not key.strip():
        return Rejection(code=RejectionCode.INVALID_TAG, message="tag key must not be empty")
    if key != key.strip():
        return Rejection(code=RejectionCode.INVALID_TAG, message=f"tag key {key!r} has leading or trailing spaces")
    if len(key) > MAX_KEY_LEN:
        return Rejection(code=RejectionCode.INVALID_TAG, message=f"tag key exceeds {MAX_KEY_LEN} characters")
    if key.lower().startswith("aws:"):
        return Rejection(code=RejectionCode.INVALID_TAG, message=f"tag key {key!r} uses the reserved 'aws:' prefix")
    if key.lower().startswith(RESERVED_PREFIX):
        # Checked case-insensitively: IAM matches tag-key names in conditions case-insensitively, and
        # IAM's prefix deny (StringLike) can't, so this is the complete guard for CloudGuard markers.
        return Rejection(code=RejectionCode.INVALID_TAG, message=f"tag key {key!r} uses the reserved 'cloudguard:' prefix",
                         hint="cloudguard: tags (e.g. cloudguard:protected) are managed outside tagging requests")
    if not _ALLOWED.match(key):
        return Rejection(code=RejectionCode.INVALID_TAG, message=f"tag key {key!r} contains characters AWS doesn't allow",
                         hint="allowed: letters, digits, spaces and _ . : / = + - @")
    if not value.strip():
        return Rejection(code=RejectionCode.INVALID_TAG, message=f"tag {key!r} has an empty value",
                         hint="CloudGuard never invents tag values; provide the value explicitly")
    if len(value) > MAX_VALUE_LEN:
        return Rejection(code=RejectionCode.INVALID_TAG, message=f"value for {key!r} exceeds {MAX_VALUE_LEN} characters")
    if not _ALLOWED.match(value):
        return Rejection(code=RejectionCode.INVALID_TAG, message=f"value for {key!r} contains characters AWS doesn't allow",
                         hint="allowed: letters, digits, spaces and _ . : / = + - @")
    return None


def policy_rule_for(key: str, rules: list[TagRule]) -> TagRule | None:
    return next((r for r in rules if r.key.lower() == key.lower()), None)


def classify_and_check(key: str, value: str, rules: list[TagRule]) -> tuple[TagCategory, PolicyViolation | None, list[str]]:
    """Category of a requested tag under the policy, a violation if the value breaks the policy,
    and advisory notes. A custom key is never treated as mandatory."""
    rule = policy_rule_for(key, rules)
    if rule is None:
        return TagCategory.CUSTOM, None, ["custom tag: not part of the mandatory tagging policy"]
    notes = []
    if key != rule.key:
        notes.append(f"policy key is spelled {rule.key!r}; {key!r} will still satisfy it (keys match case-insensitively)")
    canonical = rule.canonical_value(value)
    if canonical is None:
        allowed = ", ".join(rule.allowed_values)
        aliases = f"; aliases: {', '.join(rule.aliases)}" if rule.aliases else ""
        return TagCategory.POLICY, PolicyViolation(
            key=key, value=value, policy_key=rule.key, allowed_values=list(rule.allowed_values),
            aliases=dict(rule.aliases),
            message=f"{value!r} is not an allowed value for policy tag {rule.key!r} (allowed: {allowed}{aliases})",
        ), []
    if canonical != value:
        notes.append(f"{value!r} is accepted by policy; the canonical value is {canonical!r} (not changed automatically)")
    return TagCategory.POLICY, None, notes
