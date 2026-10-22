"""Evidence-backed tag recommendations. Read-only and deterministic: no AI model, no AWS call, no write.

Adapted from CloudGuard Pro's `recommendations/recommend.py` (evidence precedence, value and source gates, structural
checks on operator files, unmatched/foreign-ARN rejection), rebuilt on CloudGuard AI's TagFinding and versioned TagRule.

A recommendation is only ever made for a mandatory or optional policy key that is missing or invalid on a resource,
and only from evidence. It never invents a value: with no usable evidence the key is NEEDS_USER_INPUT.

Evidence hierarchy (for each missing or invalid key):
  1. Existing valid tags on the same resource. A key that is already valid needs nothing; it is listed in
     `verified_tags` (key names only) and gets no recommendation.
  2. Approved key aliases on the same resource (KEY_ALIASES, e.g. `env` for Environment, `cost-center` for CostCenter).
     The alias value must pass the tag-value gate and the effective policy for the resource type; the policy's own
     value aliases normalise it (`prod` -> `Production`).
  3. Explicit operator-provided values with recorded provenance (an operator evidence file loaded by the server
     operator, never a tool argument): an exact-ARN entry, which supersedes the explicit account-wide scope
     "account:<ACCOUNT_ID>" (a superseded default is kept as rejected evidence). Owner has no account-wide default:
     ownership is per resource. The file's sha256
     is reported as provenance. A file with any structural problem (unknown key, foreign-account or unmatched ARN,
     repeated JSON key, an entry without exactly {value, source}) is rejected as a whole, and then no key is
     recommended at all (INVALID_EVIDENCE / OPERATOR_EVIDENCE_REJECTED).
  4. Otherwise NEEDS_USER_INPUT.
  Every source that yields a valid value is collected. One distinct value: RECOMMENDED. Different values: CONFLICT
  (sources never override each other, unlike CloudGuard Pro where an operator value outranks an alias). An explicit
  operator value that fails validation is INVALID_EVIDENCE and is never replaced by an alias.

Never evidence:
  - creator or tool tags (`created-by`, `createdBy`, `aws:createdBy`, ...): they name who created a resource, not who is
    responsible for it (CREATOR_TAG_IGNORED);
  - AWS system tags (`aws:*`);
  - resource names, ARNs, descriptions or any other metadata (CostCenter is never inferred from a name).
Prompt-like text in tag values is only ever compared as data; it cannot change what this module does.

Confidence is a fixed label per evidence type, not a probability: "medium" for a value on the same resource or an
operator entry for that exact resource, "low" for an account-wide operator default, "none" when nothing is recommended.
Nothing here verifies that a value is true; a recommendation is not an approval.
"""

from __future__ import annotations

import hashlib
import json
import re
import unicodedata
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

from cloudguard.governance.models import ComplianceStatus, TagFinding
from cloudguard.governance.policy import MAX_VALUE_LEN, PolicyDocument, TagRule

# policy key (lower) -> other tag keys (lower) that carry the same meaning on the same resource. Deliberately narrower than
# CloudGuard Pro's table: "service", "product", "billing" and "cost" are too ambiguous to stand for Project or CostCenter.
KEY_ALIASES: dict[str, tuple[str, ...]] = {
    "environment": ("env", "stage", "environ"),
    "project": ("app", "application", "project-name", "project_name"),
    "owner": ("team", "owner-team", "owning-team", "contact", "maintainer"),
    "costcenter": ("cost-center", "cost_center", "costcentre", "cost-centre"),
}
# Tags that name a creator or tool. Never evidence, for any key.
CREATOR_KEYS = frozenset({"created-by", "createdby", "created_by", "creator", "aws:createdby"})

MAX_SOURCE_LEN, MIN_SOURCE_LEN = 80, 3
MAX_EVIDENCE_FILE_BYTES = 1_000_000
_PLACEHOLDERS = frozenset({"unknown", "tbd", "tba", "todo", "n/a", "na", "none", "null", "nil", "undefined", "changeme",
                           "change-me", "xxx", "-", "?", "placeholder", "default", "unassigned", "fixme", "pending"})
_SOURCE_PLACEHOLDERS = _PLACEHOLDERS | {"test", "testing", "sample", "example", "dummy", "foo", "bar", "baz", "asdf",
                                        "string", "source", "value", "unspecified"}
_SOURCE_PLACEHOLDER_PHRASES = ("to be determined", "to be decided", "not applicable", "not available", "your source",
                               "source here", "insert source", "fill in", "fill-in", "enter source")
_BRACKETED = re.compile(r"^[<\[{(].*[>\]})]$")
_BAD_CATEGORIES = {"Cc", "Cf", "Cs", "Co", "Cn", "Zl", "Zp"}  # control, invisible/bidi format, line/paragraph separators
_TAG_VALUE = re.compile(r"^[\w .:/=+@-]+$")  # AWS tag value charset: letters, digits, spaces and _ . : / = + - @
_ACCOUNT_SCOPE = re.compile(r"^account:(\d{12})$")
NO_ACCOUNT_DEFAULT = frozenset({"owner"})  # keys that must never come from an account-wide default
MAX_FREE_FORM_WORDS = 6
# Words that mark instructions or prose aimed at a model, never governance data. Matched as whole words.
_INSTRUCTION_WORDS = re.compile(
    r"\b(ignore|disregard|instruction|instructions|assistant|prompt|override|previous|pretend|jailbreak)\b", re.I)
_SCOPE_ARN = re.compile(r"^arn:aws[a-z-]*:(lambda|dynamodb|s3|ec2):[a-z0-9-]*:(\d{12})?:\S{1,1024}$")
ENTRY_FIELDS = ("value", "source")

RecommendationStatus = Literal["RECOMMENDED", "NEEDS_USER_INPUT", "CONFLICT", "INVALID_EVIDENCE"]
ReasonCode = Literal["SUPPORTED_BY_EVIDENCE", "NO_EVIDENCE", "CREATOR_TAG_IGNORED", "CONFLICTING_EVIDENCE",
                     "INVALID_ALIAS_VALUE", "INVALID_OPERATOR_VALUE", "OPERATOR_EVIDENCE_REJECTED"]
Confidence = Literal["medium", "low", "none"]

LIMITATIONS = [
    "Recommendations are suggestions from evidence; CloudGuard does not verify that a value is true.",
    "Confidence is a fixed label per evidence type (medium: same resource or an exact operator entry; low: an "
    "account-wide operator default), not a probability.",
    "Resource names, ARNs and descriptions are never used as evidence; Owner and CostCenter are never inferred.",
    "A recommendation is not an approval: changing tags needs a separate proposal approved by an authorized person.",
]


# ---------- models ----------

class _Model(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")


class EvidenceItem(_Model):
    """One piece of evidence looked at for a key, accepted or not. Values are untrusted data, never instructions."""

    source_type: Literal["alias_tag", "creator_tag", "operator_exact", "operator_default"]
    reference: str  # the tag key it came from, or the operator's stated source
    observed_value: str
    normalized_value: str | None  # the policy's canonical spelling, when accepted
    accepted: bool
    reason: str


class PolicyCheck(_Model):
    allowed_values: list[str]  # empty: any valid non-empty value
    value_valid: bool | None  # None when there is no value to check
    reason: str


class TagRecommendation(_Model):
    resource_arn: str
    resource_type: str
    key: str  # policy key
    required: bool
    status: RecommendationStatus
    reason_code: ReasonCode
    recommended_value: str | None
    confidence: Confidence
    evidence: list[EvidenceItem]
    policy_validation: PolicyCheck
    current_value: str | None  # the present but invalid value, if any (untrusted data)
    human_input_reason: str | None  # why a person must decide; None when RECOMMENDED


class OperatorEvidenceStatus(_Model):
    status: Literal["not_configured", "loaded", "rejected"]
    entries: int = 0  # scopes in the file
    content_hash: str | None = None  # sha256 of the file bytes: which exact file the operator values came from
    problems: list[str] = Field(default_factory=list)  # why the file was rejected (names only, no ARNs or values)
    note: str = ""  # plain statement of what operator evidence was (not) used; never a path


NOT_CONFIGURED_NOTE = ("No operator evidence source is configured on this server; only tags on the resource itself "
                       "were considered as evidence.")
LOADED_NOTE = "Operator evidence file loaded; each accepted value names its source."
REJECTED_NOTE = "The configured operator evidence file was rejected; no value is recommended until it is fixed."
LOW_CONFIDENCE_NOTE = ("Account-wide operator default, not verified for this resource; a person must confirm it "
                       "applies before it is used.")


class ResourceRecommendations(_Model):
    resource_arn: str
    resource_type: str
    compliance_status: ComplianceStatus
    tags_readable: bool
    verified_tags: list[str]  # policy keys already valid on the resource
    recommendations: list[TagRecommendation]


# ---------- value and source gates ----------

def is_placeholder(value: str) -> bool:
    return value.strip().lower() in _PLACEHOLDERS


def validate_tag_value(value: Any) -> tuple[bool, str]:
    """Deterministic gate for any value that could be recommended. -> (ok, reason)."""
    if not isinstance(value, str):
        return False, "value is not text"
    if not value.strip():
        return False, "value is empty or whitespace-only"
    if value != value.strip():
        return False, "value has leading or trailing whitespace"
    if len(value) > MAX_VALUE_LEN:
        return False, f"value exceeds {MAX_VALUE_LEN} characters"
    if not _TAG_VALUE.match(value):
        return False, "value has characters AWS tags do not allow"
    if is_placeholder(value):
        return False, "value is a placeholder, not governance data"
    if len(value.split()) > MAX_FREE_FORM_WORDS:
        return False, f"value has more than {MAX_FREE_FORM_WORDS} words; it reads as prose, not a tag value"
    if _INSTRUCTION_WORDS.search(value):
        return False, "value contains instruction-like words; it is treated as untrusted text, not a tag value"
    if "=" in value and re.search(r"\w\s*=\s*\w", value) and len(value.split()) > 1:
        return False, "value embeds key=value assignments; it is treated as untrusted text, not a tag value"
    return True, "ok"


def validate_source(source: Any) -> tuple[bool, str]:
    """Gate for the provenance of an operator value. It can't prove a source is true; it refuses what is clearly not a
    reference. Too long is rejected, never truncated."""
    if not isinstance(source, str):
        return False, "source is not text"
    if not source.strip():
        return False, "source is empty"
    if any(unicodedata.category(c) in _BAD_CATEGORIES for c in source):
        return False, "source contains control, line-break or invisible characters"
    if source != source.strip():
        return False, "source has leading or trailing whitespace"
    if len(source) > MAX_SOURCE_LEN:
        return False, f"source is longer than {MAX_SOURCE_LEN} characters"
    if len(source) < MIN_SOURCE_LEN:
        return False, f"source is shorter than {MIN_SOURCE_LEN} characters"
    if not any(c.isalnum() for c in source):
        return False, "source has no letter or digit"
    low = source.lower()
    if (low in _SOURCE_PLACEHOLDERS or _BRACKETED.match(source) or len(set(low)) == 1
            or any(p in low for p in _SOURCE_PLACEHOLDER_PHRASES)):
        return False, "source is a placeholder, not a real reference"
    return True, "ok"


def check_against_policy(value: str, rule: TagRule) -> tuple[str | None, str]:
    """(canonical value or None, reason): the tag-value gate, then the rule's allowed values and value aliases."""
    ok, why = validate_tag_value(value)
    if not ok:
        return None, why
    canonical = rule.canonical_value(value)
    if canonical is None:
        return None, f"not an allowed value for {rule.key}"
    return canonical, "ok"


# ---------- operator evidence ----------

class OperatorEvidence(_Model):
    """Operator-provided values: {scope: {policy key: {value, source}}}. A scope is "*" (account-wide default) or an
    exact resource ARN. Loaded by the server operator from a file; never supplied by an MCP caller."""

    scopes: dict[str, Any]  # shape is checked by operator_problems, so a bad block is reported, not raised
    content_hash: str


def _label(arn: str) -> str:
    """'service:name' for messages: never the account id or region."""
    parts = arn.split(":", 5)
    if len(parts) < 6:
        return "?"
    name = parts[5].split("/", 1)[-1] if parts[2] in ("dynamodb", "ec2") else parts[5].split(":")[-1]
    return f"{parts[2]}:" + "".join(c if c.isprintable() else "?" for c in name)[:64]


def load_operator_evidence(path: Path | None) -> tuple[OperatorEvidence | None, list[str]]:
    """(evidence, problems). Problems never include the path, values or sources."""
    if path is None:
        return None, []
    try:
        raw = path.read_bytes()
    except OSError:
        return None, ["the operator evidence file could not be read"]
    if len(raw) > MAX_EVIDENCE_FILE_BYTES:
        return None, ["the operator evidence file is too large"]
    try:
        data = json.loads(raw.decode("utf-8"), object_pairs_hook=_no_duplicate_keys)
    except _DuplicateKey:
        return None, ["the operator evidence file repeats a JSON key; json would silently keep only the last one"]
    except (UnicodeDecodeError, json.JSONDecodeError):
        return None, ["the operator evidence file is not valid JSON"]
    if not isinstance(data, dict) or data.get("version") != 1 or not isinstance(data.get("scopes"), dict) \
            or set(data) - {"version", "scopes"}:
        return None, ['the operator evidence file must be {"version": 1, "scopes": {...}}']
    return OperatorEvidence(scopes=data["scopes"], content_hash="sha256:" + hashlib.sha256(raw).hexdigest()), []


class _DuplicateKey(ValueError):
    pass


def _no_duplicate_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    keys = [k for k, _ in pairs]
    if len(keys) != len(set(keys)):
        raise _DuplicateKey
    return dict(pairs)


def policy_keys(document: PolicyDocument) -> list[str]:
    """Every tag key the policy names for any resource type (defaults and override rules), first spelling kept."""
    keys: dict[str, str] = {}
    for rule in [*document.defaults, *(r for ov in document.overrides.values() for r in ov.rules)]:
        keys.setdefault(rule.key.lower(), rule.key)
    return list(keys.values())


def assess_operator_evidence(path: Path | None, keys: list[str], account_id: str, scanned_arns: set[str]
                             ) -> tuple[OperatorEvidence | None, OperatorEvidenceStatus]:
    """Load and structurally check the operator file against one scan. A file with any problem is unusable as a
    whole: (None, rejected status). Values and sources are checked per key later, in recommend()."""
    if path is None:
        return None, OperatorEvidenceStatus(status="not_configured", note=NOT_CONFIGURED_NOTE)
    evidence, problems = load_operator_evidence(path)
    if evidence is not None:
        problems = operator_problems(evidence, keys, account_id, scanned_arns)
    if problems:
        return None, OperatorEvidenceStatus(
            status="rejected", entries=len(evidence.scopes) if evidence else 0,
            content_hash=evidence.content_hash if evidence else None, problems=problems, note=REJECTED_NOTE)
    return evidence, OperatorEvidenceStatus(status="loaded", entries=len(evidence.scopes),
                                            content_hash=evidence.content_hash, note=LOADED_NOTE)


def operator_problems(evidence: OperatorEvidence, policy_keys: list[str], account_id: str,
                      scanned_arns: set[str]) -> list[str]:
    """Structural problems that make the whole file unusable (empty list = usable). A misspelt key or field, or an ARN
    that matches nothing, would otherwise be ignored without a trace; an ARN of another account is never allowed."""
    problems: list[str] = []
    canon = {k.lower(): k for k in policy_keys}
    for scope, block in evidence.scopes.items():
        if isinstance(scope, str) and (a := _ACCOUNT_SCOPE.match(scope)):
            label = "account"
            if a.group(1) != account_id:
                problems.append("an account-wide scope names a different AWS account")
                continue
        elif scope == "*":
            problems.append("scope '*' is not allowed; write the account-wide scope explicitly as 'account:<ACCOUNT_ID>'")
            continue
        elif isinstance(scope, str) and (m := _SCOPE_ARN.match(scope)):
            label = _label(scope)
            if m.group(2) and m.group(2) != account_id:
                problems.append(f"scope {label!r} belongs to a different AWS account")
                continue
            if scope not in scanned_arns:
                problems.append(f"scope {label!r} matches no resource in this scan")
                continue
        else:
            problems.append("a scope is neither 'account:<ACCOUNT_ID>' nor a supported resource ARN")
            continue
        if not isinstance(block, dict):
            problems.append(f"scope {label!r}: expected an object of tag keys")
            continue
        seen: set[str] = set()
        for key, entry in block.items():
            low = key.lower() if isinstance(key, str) else None
            if low not in canon:
                problems.append(f"scope {label!r}: unknown tag key; allowed: {', '.join(policy_keys)}")
                continue
            if low in seen:
                problems.append(f"scope {label!r}: tag key {canon[low]!r} appears twice")
            if label == "account" and low in NO_ACCOUNT_DEFAULT:
                problems.append(f"tag key {canon[low]!r} can't have an account-wide default; ownership is per resource")
            seen.add(low)
            if not isinstance(entry, dict) or set(entry) != set(ENTRY_FIELDS):
                problems.append(f"scope {label!r}, key {canon[low]!r}: expected exactly {{value, source}}")
    return problems


def _operator_entries(evidence: OperatorEvidence | None, arn: str, account_id: str, key: str
                      ) -> list[tuple[dict, str]]:
    """[(entry, "operator_exact" | "operator_default")], exact first. Only the first is used as evidence; a default
    behind an exact entry is reported as superseded."""
    if evidence is None:
        return []
    found = []
    for scope, kind in ((arn, "operator_exact"), (f"account:{account_id}", "operator_default")):
        block = evidence.scopes.get(scope) or {}
        for k, entry in block.items():
            if isinstance(k, str) and k.lower() == key.lower():
                found.append((entry, kind))
    return found


# ---------- recommendation ----------

def _tag(tags: dict[str, str], key_lower: str) -> tuple[str, str] | None:
    for k, v in tags.items():
        if k.lower() == key_lower:
            return k, v
    return None


def _recommend_key(finding: TagFinding, rule: TagRule, current: str | None,
                   evidence: OperatorEvidence | None, evidence_rejected: bool) -> TagRecommendation:
    tags = finding.existing_tags or {}
    items: list[EvidenceItem] = []
    candidates: list[tuple[str, str]] = []  # (canonical value, source_type)
    policy_ref = PolicyCheck(allowed_values=list(rule.allowed_values), value_valid=None, reason="no value to check")

    for alias in KEY_ALIASES.get(rule.key.lower(), ()):
        found = _tag(tags, alias)
        if found is None:
            continue
        canonical, why = check_against_policy(found[1], rule)
        items.append(EvidenceItem(source_type="alias_tag", reference=found[0], observed_value=found[1],
                                  normalized_value=canonical, accepted=canonical is not None, reason=why))
        if canonical is not None:
            candidates.append((canonical, "alias_tag"))
    # A creator tag is only ever a tempting stand-in for Owner; it is recorded there as rejected evidence.
    creators = [(k, v) for k, v in tags.items() if k.lower() in CREATOR_KEYS] if rule.key.lower() == "owner" else []
    for k, v in creators:
        items.append(EvidenceItem(source_type="creator_tag", reference=k, observed_value=v, normalized_value=None,
                                  accepted=False, reason="creator or tool tags are never evidence"))

    operator_rejected = False
    ops = _operator_entries(evidence, finding.resource_arn, finding.account_id, rule.key)
    for entry, kind in ops[1:]:  # exact-resource evidence takes precedence over the account-wide default
        value, source = entry.get("value"), entry.get("source")
        items.append(EvidenceItem(source_type=kind, reference=source if isinstance(source, str) else "?",
                                  observed_value=value if isinstance(value, str) else "?", normalized_value=None,
                                  accepted=False, reason="superseded by the exact-resource operator entry"))
    if ops:
        entry, kind = ops[0]
        value, source = entry.get("value"), entry.get("source")
        canonical, why = check_against_policy(value, rule) if isinstance(value, str) else (None, "value is not text")
        src_ok, src_why = validate_source(source)
        if not src_ok:
            canonical, why = None, src_why
        items.append(EvidenceItem(source_type=kind, reference=source if isinstance(source, str) else "?",
                                  observed_value=value if isinstance(value, str) else "?",
                                  normalized_value=canonical, accepted=canonical is not None, reason=why))
        if canonical is None:
            operator_rejected = True
        else:
            candidates.append((canonical, kind))

    def result(status, code, value=None, confidence="none", human=None, valid=None, reason="no value to check"):
        return TagRecommendation(
            resource_arn=finding.resource_arn, resource_type=finding.resource_type, key=rule.key,
            required=rule.required, status=status, reason_code=code, recommended_value=value, confidence=confidence,
            evidence=items, current_value=current, human_input_reason=human,
            policy_validation=policy_ref.model_copy(update={"value_valid": valid, "reason": reason}))

    if evidence_rejected:  # the rejected file may hold a value that contradicts the alias: recommend nothing
        return result("INVALID_EVIDENCE", "OPERATOR_EVIDENCE_REJECTED", valid=None,
                      reason="operator evidence file rejected",
                      human="The configured operator evidence file was rejected (see operator_evidence.problems); "
                            "the operator must fix it before any value can be recommended.")
    if operator_rejected:  # an explicit operator value that fails is never replaced by an alias
        return result("INVALID_EVIDENCE", "INVALID_OPERATOR_VALUE", valid=False, reason="operator value rejected",
                      human="The operator-provided value or its source failed validation; the operator must correct it.")
    values = {v for v, _ in candidates}
    if len(values) > 1:
        return result("CONFLICT", "CONFLICTING_EVIDENCE",
                      human="The evidence disagrees; a person responsible for the resource must choose the value.")
    if values:
        value = values.pop()
        kinds = {k for _, k in candidates}
        if kinds == {"operator_default"}:  # never presented as a verified fact
            return result("RECOMMENDED", "SUPPORTED_BY_EVIDENCE", value=value, confidence="low", valid=True,
                          reason="ok", human=LOW_CONFIDENCE_NOTE)
        return result("RECOMMENDED", "SUPPORTED_BY_EVIDENCE", value=value, confidence="medium", valid=True, reason="ok")
    if any(i.source_type == "alias_tag" for i in items):
        return result("INVALID_EVIDENCE", "INVALID_ALIAS_VALUE", valid=False, reason="alias value rejected",
                      human="The only evidence on the resource failed validation; a person must provide the value.")
    if creators:
        return result("NEEDS_USER_INPUT", "CREATOR_TAG_IGNORED",
                      human="Only a creator or tool tag exists; it names who created the resource, not who is "
                            "responsible for it. A person must provide the value.")
    return result("NEEDS_USER_INPUT", "NO_EVIDENCE",
                  human="There is no evidence for this value; a person responsible for the resource must provide it.")


def recommend(finding: TagFinding, rules: list[TagRule], evidence: OperatorEvidence | None = None,
              evidence_rejected: bool = False) -> ResourceRecommendations:
    """Recommendations for one finding under its effective policy rules. Pure: reads nothing and writes nothing.

    `evidence_rejected`: an operator evidence file is configured but unusable. Every key then gets
    INVALID_EVIDENCE, because the rejected file may have held a value that conflicts with the other evidence."""
    readable = finding.compliance_status != ComplianceStatus.INSPECTION_FAILED
    wanted = {k.lower() for k in finding.missing_tags} | {i.key.lower() for i in finding.invalid_tags}
    invalid_values = {i.key.lower(): i.value for i in finding.invalid_tags}
    recs, verified = [], []
    for rule in rules if readable else []:
        if rule.key.lower() in wanted:
            recs.append(_recommend_key(finding, rule, invalid_values.get(rule.key.lower()), evidence,
                                       evidence_rejected))
        elif _tag(finding.existing_tags or {}, rule.key.lower()) is not None:
            verified.append(rule.key)
    return ResourceRecommendations(resource_arn=finding.resource_arn, resource_type=finding.resource_type,
                                   compliance_status=finding.compliance_status, tags_readable=readable,
                                   verified_tags=verified, recommendations=recs)
