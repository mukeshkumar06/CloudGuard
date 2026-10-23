"""Read-only tag-governance MCP tools.

Thin adapters over cloudguard.governance: discovery (LiveSource / FixtureSource) and compliance
(run_scan / evaluate) are the existing deterministic code; nothing here decides compliance, and no LLM
is involved (`speech` and explanations are deterministic templates). Nothing writes to AWS, and no
policy is ever created or saved.

Scan reuse: one MCP call runs discovery at most once. Scans are cached per scope (the regions and services
scanned) while younger than SCAN_MAX_AGE (60 seconds), so asking for the inventory, the compliance summary
and the untagged list back to back costs one scan. A scoped inventory (get_resource_inventory with a
region or services) is answered from a fresh full scan when there is one, without calling AWS again.
`freshness` says when the scan ran and whether it was reused; refresh=true asks for a new scan. The cache
belongs to this server, whose credentials are fixed at startup; if a scan ever reports a different AWS
account than the cached ones, every cached scan is dropped, so results are never shared across accounts.

Scan protection: at most one scan runs at a time, started from one dedicated worker thread (so governance
calls can't use up the worker threads the cost tools need); discovery inside it is bounded-parallel (see
cloudguard.governance.discovery). A refresh within MIN_REFRESH_INTERVAL (10 seconds) of the last scan of
the same scope is deferred and reported in `freshness.refresh_deferred`. A call that needs a scan while
another is running waits at most SCAN_WAIT_SECONDS, then gets a clear "scan in progress" error instead of
queueing.

Coverage: a service that couldn't be listed in a region is a discovery error, and a region where every
supported service failed is reported as inaccessible. coverage_complete is false whenever anything
failed, so a partial scan is never presented as the whole account.

Error redaction: AWS error messages often name the calling principal (user or role ARN, session name,
account ID). Results carry only the error kind, the AWS error code and the operation; the message is
written, redacted, to the server's local log for diagnosis.

Untrusted data: resource names, tag keys and tag values are returned as structured data only. `speech`
and `explanation` never contain them; they identify a resource by service, region and finding number.

Recommendations: recommend_tags runs cloudguard.governance.recommendations over one finding of the
latest scan. It suggests tag values only from evidence (alias tags on the same resource, the operator
evidence file configured at startup) and never writes anything: no tag, no proposal, no approval.

Known limitation: the policy is read from the local policy store only (see context.py). The CLI's
DynamoDB policy store is not supported here, so a team policy kept only in DynamoDB is not applied;
policy_source and `limitations` say which policy was used.
"""

from __future__ import annotations

import logging
import re
from collections import Counter, defaultdict
from dataclasses import dataclass, replace
from datetime import datetime, timedelta
from typing import Annotated, Literal

import anyio
from mcp.server.fastmcp import FastMCP
from mcp.server.fastmcp.exceptions import ToolError
from mcp.types import ToolAnnotations
from pydantic import BaseModel, ConfigDict, Field

from cloudguard.cost.service import Outcome
from cloudguard.governance.compliance import run_scan, summarize
from cloudguard.governance.custom_tagging import CustomTaggingEngine
from cloudguard.governance.custom_tagging.models import (
    Candidate, NameFilter, TaggingOperation, TaggingProposal, TaggingRequest, TargetCriteria,
)
from cloudguard.governance.models import DiscoveredResource, DiscoveryResult
from cloudguard.governance.discovery import ScopeError
from cloudguard.governance.discovery.common import AWS_ERRORS, classify_error
from cloudguard.governance.models import (
    UNRESOLVED_REGION, ComplianceStatus, ErrorKind, GovernanceScan, InspectionError, InvalidTagReason, PolicyRef,
    ServiceError, TagFinding,
)
from cloudguard.governance.policy import PolicyIntegrityError, PolicyVersion, TagRule
from cloudguard.governance.recommendations import (
    LIMITATIONS as RECOMMENDATION_LIMITATIONS, OperatorEvidenceStatus, RecommendationStatus, TagRecommendation,
    assess_operator_evidence, policy_keys, recommend,
)
from cloudguard.server.context import GovernanceContext

log = logging.getLogger(__name__)

SCHEMA_VERSION = "1"
SCAN_MAX_AGE = timedelta(seconds=60)
MIN_REFRESH_INTERVAL = timedelta(seconds=10)
SCAN_WAIT_SECONDS = 5.0  # longest a call waits for another call's scan before "scan in progress"
MAX_PAGE = 100
SUPPORTED_SERVICES = ("dynamodb", "ec2", "lambda", "s3")  # one discovery pass per service per region

GOVERNANCE_TOOL_NAMES = ("get_resource_inventory", "search_resources", "scan_tag_compliance", "list_untagged_resources",
                         "explain_finding", "recommend_tags", "preview_tag_changes")

PolicySource = Literal["stored", "built-in default"]
FindingId = Annotated[str, Field(pattern=r"^tagcomp-[0-9a-f]{16}$",
                                 description="finding_id from list_untagged_resources or scan_tag_compliance")]
ResourceRef = Annotated[str, Field(
    max_length=1100,
    pattern=r"^(tagcomp-[0-9a-f]{16}|arn:aws[a-z-]*:(lambda|dynamodb|s3|ec2):[a-z0-9-]*:(\d{12})?:\S{1,1024})$",
    description="finding_id (tagcomp-...) from list_untagged_resources, or the ARN of a scanned Lambda function, "
                "DynamoDB table, S3 bucket or EC2 instance in this account")]
ServiceName = Literal["dynamodb", "ec2", "lambda", "s3"]
SERVICE_NOUN = {"lambda": "Lambda function", "dynamodb": "DynamoDB table", "s3": "S3 bucket", "ec2": "EC2 instance"}
ResourceName = Annotated[str, Field(
    min_length=1, max_length=128, pattern=r"^[^\x00-\x1f\x7f]+$",
    description="The resource's name as the user says it (for example OrderProcessor); matched against the latest "
                "scan, ignoring case, spaces and punctuation. Use instead of finding_id; never guessed when "
                "several resources match.")]
NameRegion = Annotated[str, Field(pattern=r"^[a-z]{2}(-[a-z]+)+-\d{1,2}$",
                                  description="Only match resources in this region (narrows the latest scan)")]
Services = Annotated[list[ServiceName] | None, Field(
    min_length=1, max_length=4, description='Only these services (e.g. ["lambda"] for "untagged Lambdas")')]
RegionArg = Annotated[str, Field(
    pattern=r"^(all|[a-z]{2}(-[a-z]+)+-\d{1,2})$",
    description='"all" for every enabled region, or one AWS region code such as ap-south-1. Must be enabled for '
                "the account. Map place names to codes first (Mumbai is ap-south-1).")]
Cursor = Annotated[str, Field(pattern=r"^scan-\d{8}T\d{6}Z-[0-9a-f]{6}:\d{1,6}$",
                              description="next_cursor from the previous page; omit for the first page")]

TAG_EXPIRY_NOTE = ("Tag expiry is not supported: CloudGuard does not read or evaluate tag expiry dates, "
                   "so no finding means a tag is current.")
POLICY_STORE_NOTE = ("Policies are read from the server's local policy store only; a policy kept only in "
                     "the CloudGuard DynamoDB table is not applied (see policy_source).")


# ---------- redaction ----------

_TOKEN = re.compile(r"^[A-Za-z0-9._:-]{1,64}$")
_REDACTIONS = (
    (re.compile(r"arn:aws[\w-]*:[^\s\"'`,;()<>]*"), "<arn>"),
    (re.compile(r"\b(?:assumed-role|federated-user|user|role|group)/[^\s\"'`,;()<>]+"), "<principal>"),
    (re.compile(r"\b(?:AKIA|ASIA|AIDA|AROA|AIPA|ANPA|ANVA|ASCA|AGPA)[A-Z0-9]{12,}\b"), "<aws-id>"),
    (re.compile(r"(?<!\d)\d{12}(?!\d)"), "<account>"),
    (re.compile(r"\b[A-Za-z]:[\\/][^\s\"'`]*"), "<path>"),
    (re.compile(r"(?<![\w:/.-])/(?:[^\s/\"'`]+/)+[^\s\"'`]*"), "<path>"),
)


def redact(text: str, limit: int = 300) -> str:
    """Strip principal ARNs, IAM unique IDs, access key IDs, account IDs and file paths (for local logs)."""
    for pattern, replacement in _REDACTIONS:
        text = pattern.sub(replacement, text)
    return text[:limit]


def _token(value: str) -> str:
    """An AWS error code or operation name, or 'Unknown' if it isn't a plain identifier."""
    return value if _TOKEN.match(value) else "Unknown"


# ---------- output models ----------

class _Model(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")


class SafeError(_Model):
    """What went wrong, without AWS's message text (which can name the calling principal)."""

    kind: ErrorKind
    code: str  # AWS error code, e.g. AccessDenied
    operation: str  # IAM action that failed, e.g. s3:GetBucketTagging

    @classmethod
    def of(cls, error: InspectionError) -> SafeError:
        return cls(kind=error.kind, code=_token(error.code), operation=_token(error.operation))


class DiscoveryError(_Model):
    """Listing a whole service failed in a region; its resources there may be missing."""

    service: str
    region: str
    error: SafeError

    @classmethod
    def of(cls, e: ServiceError) -> DiscoveryError:
        return cls(service=e.service, region=e.region, error=SafeError.of(e.error))


class SafeFinding(TagFinding):
    """A TagFinding whose inspection error carries no AWS message text."""

    inspection_error: SafeError | None = None

    @classmethod
    def of(cls, f: TagFinding) -> SafeFinding:
        err = SafeError.of(f.inspection_error).model_dump() if f.inspection_error else None
        return cls.model_validate(f.model_dump() | {"inspection_error": err})


class Freshness(_Model):
    generated_at: datetime  # when CloudGuard built this answer
    scanned_at: datetime  # when discovery and evaluation completed (the observation time)
    scan_reused: bool  # True when this answer reused a scan another call ran
    refresh_deferred: bool = False  # a refresh was asked for but the last scan is under MIN_REFRESH_INTERVAL old
    refresh_available_at: datetime  # earliest time refresh=true starts a new scan
    max_scan_age_seconds: int = int(SCAN_MAX_AGE.total_seconds())
    note: str = "Results reflect AWS at scanned_at; scans are reused for up to max_scan_age_seconds."


class _Envelope(_Model):
    schema_version: str = SCHEMA_VERSION
    tool: str
    outcome: Outcome  # answered, or findings when something needs attention (violations, failures)
    mode: Literal["fixture", "live"]
    data_source: str
    account_id: str  # the account the server's own credentials belong to (never chosen by a caller)
    scan_id: str
    regions_scanned: list[str]  # every enabled region in live mode
    freshness: Freshness
    speech: str  # short deterministic reply for a voice client; never contains AWS-sourced free text


class _PolicyEnvelope(_Envelope):
    policy: PolicyRef  # the exact policy version (id, version, content_hash) the findings were evaluated against
    policy_source: PolicySource  # "built-in default" is held in memory and never saved


class InventoryItem(_Model):
    name: str  # untrusted data (structured fields only, never speech)
    service: str
    region: str
    resource_type: str
    finding_id: str  # use with explain_finding or recommend_tags


class InventoryCount(_Model):
    service: str
    region: str
    count: int


class InventoryResult(_Envelope):
    services_scanned: list[str]  # the services this answer covers (all four unless narrowed)
    total_resources: int
    by_service: dict[str, int]  # every scanned service; see discovery_errors before reading 0 as none
    by_region: dict[str, int]  # every scanned region, including empty ones
    counts: list[InventoryCount]  # non-zero (service, region) pairs
    coverage_complete: bool  # False if any service listing failed in any region
    discovery_errors: list[DiscoveryError]
    inaccessible_regions: list[str]  # regions where every supported service failed
    resources: list[InventoryItem]  # the counted resources by name (first MAX_PAGE, by service, region, name)
    resources_truncated: bool  # True when more resources were counted than are listed


class ComplianceBreakdown(_Model):
    total: int
    compliant: int
    non_compliant: int
    unknown: int  # INSPECTION_FAILED: tags could not be read
    compliance_percentage: float | None  # compliant / (compliant + non_compliant); unknown excluded


class TagFrequency(_Model):
    key: str
    count: int


class InvalidValueCount(_Model):
    key: str  # policy key
    value: str  # value as found on the resources (untrusted data)
    reason: InvalidTagReason
    mandatory: bool
    count: int


class EvaluationError(_Model):
    finding_id: str
    resource_arn: str
    resource_type: str
    region: str
    error: SafeError


class TagComplianceResult(_PolicyEnvelope):
    total_discovered: int
    compliant: int
    non_compliant: int
    unknown: int  # INSPECTION_FAILED resources; excluded from compliance_percentage
    compliance_percentage: float | None  # None when nothing could be evaluated
    by_service: dict[str, ComplianceBreakdown]
    by_region: dict[str, ComplianceBreakdown]
    missing_mandatory_tags: list[TagFrequency]  # resources missing each mandatory key, most first
    invalid_values: list[InvalidValueCount]
    total_missing_tags: int
    total_invalid_values: int
    resources_with_notices: int  # advisory only (aliases, key case); never affect compliance
    coverage_complete: bool  # False if any discovery error or any unknown resource
    discovery_errors: list[DiscoveryError]
    inaccessible_regions: list[str]
    evaluation_errors: list[EvaluationError]  # per-resource tag retrieval failures (the unknown resources)
    tag_expiry_supported: Literal[False] = False
    limitations: list[str] = [TAG_EXPIRY_NOTE, POLICY_STORE_NOTE]


class UntaggedResource(_Model):
    finding_id: str
    resource_arn: str
    resource_id: str
    resource_name: str
    resource_type: str
    service: str
    region: str
    account_id: str
    missing_tags: list[str]
    invalid_tags: list[str]  # policy keys whose value is also empty or not allowed
    observed_tags: dict[str, str]  # raw tags as AWS returned them (untrusted data)
    applicable_rules: list[TagRule]  # effective policy rules for this resource type
    evidence_source_api: str
    observed_at: datetime


NameMatch = Literal["contains", "starts_with", "ends_with", "exact"]


class SearchHit(_Model):
    name: str  # untrusted data (structured fields only, never speech)
    service: str
    resource_type: str
    region: str
    resource_id: str  # the service's own identifier (function name, table name, bucket name, instance id)
    resource_arn: str
    finding_id: str  # internal identifier: use with explain_finding or recommend_tags


class SearchResult(_Envelope):
    match: NameMatch
    services_scanned: list[str]
    total: int  # resources whose names match, before paging
    offset: int
    returned: int
    next_cursor: str | None  # None on the last page
    duplicate_names: list[str]  # names that more than one matching resource shares (tell them apart by region)
    coverage_complete: bool  # False if any service could not be listed: matches there may be missing
    discovery_errors: list[DiscoveryError]
    resources: list[SearchHit]


PREVIEW_NEXT_STEP = (
    "Nothing was changed and nothing was stored. To apply these tags, an operator saves the proposal with "
    "`python -m cloudguard.governance proposal save --request-json FILE --mode live` (FILE holds `request`), a "
    "different allow-listed person approves its exact content hash (`proposal approve ... --content-hash H`, plus "
    "--acknowledge-overwrites if any value is overwritten), and a person who is not the approver runs "
    "`proposal execute --mode live`. Approval and execution are not available through MCP.")


class PreviewOverwrite(_Model):
    resource_name: str  # untrusted data
    region: str
    key: str
    current_value: str
    new_value: str


class PreviewSummary(_Model):
    """The preview at a glance: what an operator checks before saving."""

    matched: int  # resources the request selected (any outcome)
    changing: int  # resources whose tags would change
    resource_names: list[str]  # untrusted data, as discovered
    regions: list[str]
    proposed_tags: dict[str, str]
    overwrites: list[PreviewOverwrite]  # existing values that would be replaced (needs --acknowledge-overwrites)
    conflicts: int  # existing different values an add leaves untouched


class TagPreviewResult(_Envelope):
    status: str  # PROPOSAL_READY, NO_CHANGES, BLOCKED, NOT_FOUND, NEEDS_CLARIFICATION, REJECTED
    message: str
    summary: PreviewSummary | None
    request: TaggingRequest | None  # exactly what would be proposed; pass to `proposal save --request-json`
    proposal: TaggingProposal | None  # resources, ARNs, existing tags, new and overwritten values, account, regions
    candidates: list[Candidate]
    changes_made: Literal[False] = False
    stored: Literal[False] = False
    next_step: str = PREVIEW_NEXT_STEP


class UntaggedResourcesResult(_PolicyEnvelope):
    total: int  # resources missing at least one mandatory tag
    offset: int
    returned: int
    next_cursor: str | None  # None on the last page
    unknown: int  # resources whose tags could not be read: not listed, compliance unknown
    coverage_complete: bool
    resources: list[UntaggedResource]


class FindingExplanation(_PolicyEnvelope):
    finding: SafeFinding
    finding_number: int  # 1-based position of the finding in this scan, used in speech
    rules: list[TagRule]  # effective policy rules for this resource type
    explanation: str
    required_actions: list[str]  # what would make it compliant; CloudGuard changes nothing here


RECOMMENDATION_NEXT_STEP = ("Nothing was changed. To apply a value, a person must create a tagging proposal "
                            "and an authorized approver must approve it; this result is not an approval.")


class RecommendTagsResult(_PolicyEnvelope):
    finding_id: str
    finding_number: int  # 1-based position of the finding in this scan, used in speech
    resource_name: str  # untrusted data (structured fields only, never speech)
    resource_arn: str
    resource_type: str
    service: str
    region: str
    compliance_status: ComplianceStatus
    tags_readable: bool  # False: tags could not be read, so nothing is recommended
    verified_tags: list[str]  # policy keys already valid on the resource
    recommendations: list[TagRecommendation]  # one per missing or invalid policy key; values are untrusted data
    status_counts: dict[RecommendationStatus, int]  # every status, including zeros
    operator_evidence: OperatorEvidenceStatus  # the operator file's status and sha256 (never its path or values)
    rules: list[TagRule]  # effective policy rules for this resource type
    changes_made: Literal[False] = False
    authorizes_change: Literal[False] = False  # a recommendation is never an approval
    next_step: str = RECOMMENDATION_NEXT_STEP
    limitations: list[str] = RECOMMENDATION_LIMITATIONS


# ---------- helpers ----------

def _q(value: str, limit: int = 60) -> str:
    """Untrusted text, quoted and truncated, for structured fields only (never speech)."""
    return repr(value if len(value) <= limit else value[:limit] + "...")


def _plural(n: int, word: str) -> str:
    return f"{n} {word}{'' if n == 1 else 's'}"


def _pct(compliant: int, non_compliant: int) -> float | None:
    evaluated = compliant + non_compliant
    return round(100 * compliant / evaluated, 1) if evaluated else None


def _breakdown(findings: list[TagFinding]) -> ComplianceBreakdown:
    c = Counter(f.compliance_status for f in findings)
    ok, bad = c[ComplianceStatus.COMPLIANT], c[ComplianceStatus.NON_COMPLIANT]
    return ComplianceBreakdown(total=len(findings), compliant=ok, non_compliant=bad,
                               unknown=c[ComplianceStatus.INSPECTION_FAILED], compliance_percentage=_pct(ok, bad))


def inaccessible_regions(scan: GovernanceScan, scanned: tuple[str, ...] = SUPPORTED_SERVICES) -> list[str]:
    """Regions where every scanned service failed to list (derived from per-service ServiceErrors)."""
    failed: dict[str, set[str]] = defaultdict(set)
    for e in scan.service_errors:
        failed[e.region].add(e.service)
    return sorted(r for r, services in failed.items() if set(scanned) <= services)


_SAFE_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")


def display_name(name: str) -> str | None:
    """A resource name safe to show or say (letters, digits, '.', '_' and '-', no spaces, at most 64 characters),
    or None. Names are untrusted: anything else (sentences, markup, odd characters) is withheld, not cleaned."""
    return name if _SAFE_NAME.match(name) else None


def _norm(text: str) -> str:
    return re.sub(r"[^a-z0-9]", "", text.lower())


def find_by_name(findings: list[TagFinding], name: str, service: str | None = None,
                 region: str | None = None) -> tuple[int, TagFinding]:
    """The one finding whose resource is called `name` (ignoring case, spaces and punctuation), narrowed by service
    and region. A unique partial match ("payments" for payments-api) is accepted; several matches or none are a
    ToolError naming the candidates (by safe display name, service and region), never a guess. The caller's own
    text is never echoed."""
    pool = [(i, f) for i, f in enumerate(findings, 1)
            if (not service or f.service == service) and (not region or f.region == region)]
    query = _norm(name)
    if not query:
        raise ToolError("give the resource's name")
    hits = [(i, f) for i, f in pool if query in (_norm(f.resource_name), _norm(f.resource_id))]
    if not hits and len(query) >= 3:
        hits = [(i, f) for i, f in pool if query in _norm(f.resource_name)]
    noun = SERVICE_NOUN[service] if service else "resource"
    if len(hits) == 1:
        return hits[0]
    if not hits:
        names = sorted({display_name(f.resource_name) or "(name withheld)" for _, f in pool})
        where = f" in {region}" if region else ""
        listed = (": " + ", ".join(names[:10]) + (f" and {len(names) - 10} more" if len(names) > 10 else "")
                  if names else "; the latest scan found none")
        raise ToolError(f"no scanned {noun}{where} has that name. Scanned {noun}s{where}{listed}")
    choices = "; ".join(f"{display_name(f.resource_name) or '(name withheld)'} ({SERVICE_NOUN[f.service]} in "
                        f"{f.region})" for _, f in hits[:10])
    raise ToolError(f"{len(hits)} scanned resources match that name: {choices}. Say which service or region.")


def _missing(f: TagFinding) -> bool:
    return f.compliance_status == ComplianceStatus.NON_COMPLIANT and bool(f.missing_tags)


def _annotations(ctx: GovernanceContext, title: str) -> ToolAnnotations:
    return ToolAnnotations(title=title, readOnlyHint=True, destructiveHint=False, idempotentHint=True,
                           openWorldHint=ctx.mode == "live")


def _log_failures(scan: GovernanceScan) -> None:
    """Local diagnostics for what the results only summarise; AWS message text is redacted."""
    for e in scan.service_errors:
        log.warning("governance discovery error: %s in %s, %s from %s: %s", e.service, e.region,
                    _token(e.error.code), _token(e.error.operation), redact(e.error.message))
    for f in scan.findings:
        if f.inspection_error is not None:
            e = f.inspection_error
            log.warning("governance tag read failed: %s %s in %s, %s from %s: %s", f.finding_id, f.resource_type,
                        f.region, _token(e.code), _token(e.operation), redact(e.message))


@dataclass(frozen=True)
class _Scan:
    scan: GovernanceScan
    policy: PolicyVersion
    policy_source: PolicySource
    reused: bool = False
    refresh_deferred: bool = False
    services: tuple[str, ...] = SUPPORTED_SERVICES  # the services this scan covers


# What a scan covers: (regions or None for every enabled region, services or None for all supported services).
Scope = tuple[tuple[str, ...] | None, tuple[str, ...] | None]
FULL: Scope = (None, None)


def _narrow(full: _Scan, scope: Scope) -> _Scan | None:
    """The part of a full scan that a narrower scope covers (no AWS call), or None if it can't be answered from it."""
    regions, services = scope
    if regions and not set(regions) <= set(full.scan.regions):
        return None
    keep_regions = set(regions or full.scan.regions) | {UNRESOLVED_REGION}
    keep_services = set(services or SUPPORTED_SERVICES)
    findings = [f for f in full.scan.findings if f.service in keep_services and f.region in keep_regions]
    errors = [e for e in full.scan.service_errors if e.service in keep_services and e.region in keep_regions]
    scan = full.scan.model_copy(update=dict(regions=list(regions or full.scan.regions), findings=findings,
                                            service_errors=errors, summary=summarize(findings, errors)))
    return replace(full, scan=scan, services=tuple(s for s in SUPPORTED_SERVICES if s in keep_services))


class _Scans:
    """Recent scans per scope, per server, and the guard that keeps scans single and bounded.

    All state changes happen on the event loop, so checking for a running scan and starting one can't
    interleave. Each scan is started on a private one-thread limiter, so governance never holds more than one
    of the server's worker threads; discovery inside it uses its own bounded pool.
    """

    def __init__(self, ctx: GovernanceContext):
        self.ctx = ctx
        self._cache: dict[Scope, _Scan] = {}
        self._running: anyio.Event | None = None
        self._running_scope: Scope | None = None
        self._limiter: anyio.CapacityLimiter | None = None  # created on the server's event loop

    def _age(self, s: _Scan) -> timedelta:
        return self.ctx.clock() - s.scan.completed_at

    def _young(self, scope: Scope) -> _Scan | None:
        s = self._cache.get(scope)
        return s if s is not None and self._age(s) < SCAN_MAX_AGE else None

    def fresh(self) -> _Scan | None:
        """The latest full scan if it is still young enough to reuse; never starts a scan."""
        return self._young(FULL)

    def by_id(self, scan_id: str) -> tuple[Scope, _Scan] | None:
        """A still-young cached scan by its scan_id (for paging cursors); never starts a scan."""
        return next(((k, s) for k, s in self._cache.items()
                     if s.scan.scan_id == scan_id and self._age(s) < SCAN_MAX_AGE), None)

    def _reuse(self, scope: Scope) -> _Scan | None:
        """A young scan of exactly this scope, else the matching part of a young full scan."""
        if (s := self._young(scope)) is not None:
            return replace(s, reused=True)
        full = self.fresh() if scope != FULL else None
        narrowed = _narrow(full, scope) if full is not None else None
        return replace(narrowed, reused=True) if narrowed is not None else None

    async def get(self, refresh: bool = False, scope: Scope = FULL) -> _Scan:
        latest = self._cache.get(scope)
        if refresh and latest is not None and self._age(latest) < MIN_REFRESH_INTERVAL:
            return replace(latest, reused=True, refresh_deferred=True)
        if not refresh and (s := self._reuse(scope)) is not None:
            return s
        if self._running is not None:  # a scan is running: wait a bounded time for it
            running, running_scope = self._running, self._running_scope
            with anyio.move_on_after(SCAN_WAIT_SECONDS):
                await running.wait()
            if not running.is_set():
                raise ToolError("a governance scan is already running; try again in a few seconds")
            done = self._cache.get(scope)
            if done is not None and done is not latest:  # a new scan of this scope finished while we waited
                return replace(done, reused=True)
            if not refresh and (s := self._reuse(scope)) is not None:  # a full scan finished: answer from it
                return s
            if running_scope == scope or (running_scope == FULL and not refresh):
                raise ToolError("the governance scan that was running did not complete; try again")
            if self._running is not None:  # another scan started while we waited
                raise ToolError("a governance scan is already running; try again in a few seconds")
        if self._limiter is None:
            self._limiter = anyio.CapacityLimiter(1)
        self._running, self._running_scope = (event := anyio.Event()), scope
        try:
            s = await anyio.to_thread.run_sync(self._scan, scope, limiter=self._limiter)
            self._store(scope, s)
            return s
        finally:
            self._running, self._running_scope = None, None
            event.set()

    def _store(self, scope: Scope, s: _Scan) -> None:
        # Credentials are fixed per server, but if a scan ever reports another account, nothing cached for the
        # previous one may answer for it.
        if any(c.scan.account_id != s.scan.account_id for c in self._cache.values()):
            log.warning("governance scan reported a different AWS account; dropping cached scans")
            self._cache.clear()
        # Expired scans are never reused, so drop them: the cache stays bounded by the scopes asked for recently.
        self._cache = {k: v for k, v in self._cache.items() if self._age(v) < SCAN_MAX_AGE}
        self._cache[scope] = s

    def _scan(self, scope: Scope = FULL) -> _Scan:
        regions, services = scope
        try:
            policy, source = self.ctx.policy()
            scan = run_scan(self.ctx.source, policy, regions=list(regions or ()), services=list(services or ()))
        except ScopeError as exc:  # the requested region isn't enabled for this account (validated input only)
            raise ToolError(f"{exc}; ask for one of the enabled regions, or all regions") from None
        except AWS_ERRORS as exc:
            err = classify_error(exc, "scan")
            log.warning("governance scan could not run: %s %s: %s", err.kind.value, _token(err.code),
                        redact(err.message))
            raise ToolError(f"the scan could not run: {err.kind.value} ({_token(err.code)})") from None
        except PolicyIntegrityError as exc:
            log.error("stored tagging policy failed verification: %s", redact(str(exc)))
            raise ToolError("the stored tagging policy failed verification; check it with the CLI") from None
        except Exception as exc:  # never expose internals to the client
            log.error("governance scan failed unexpectedly: %s: %s", type(exc).__name__, redact(str(exc)))
            raise ToolError("the scan failed unexpectedly; see the server log") from None
        _log_failures(scan)
        return _Scan(scan, policy, source, services=services or SUPPORTED_SERVICES)


# ---------- tools ----------

def register_governance_tools(server: FastMCP, ctx: GovernanceContext) -> None:
    """Register the read-only governance tools on `server`, bound to the startup context `ctx`."""
    scans = _Scans(ctx)

    def common(tool: str, s: _Scan, outcome: Outcome, speech: str, policy: bool = True) -> dict:
        out = dict(tool=tool, outcome=outcome, mode=ctx.mode, data_source=ctx.data_source,
                   account_id=s.scan.account_id, scan_id=s.scan.scan_id, regions_scanned=list(s.scan.regions),
                   freshness=Freshness(generated_at=ctx.clock(), scanned_at=s.scan.completed_at,
                                       scan_reused=s.reused, refresh_deferred=s.refresh_deferred,
                                       refresh_available_at=s.scan.completed_at + MIN_REFRESH_INTERVAL),
                   speech=speech)
        if policy:
            out |= dict(policy=s.scan.policy, policy_source=s.policy_source)
        return out

    def attention(flag) -> Outcome:
        return Outcome.FINDINGS if flag else Outcome.ANSWERED

    def discovery_errors(scan: GovernanceScan) -> list[DiscoveryError]:
        return [DiscoveryError.of(e) for e in scan.service_errors]

    @server.tool(annotations=_annotations(ctx, "Resource inventory"), structured_output=True)
    async def get_resource_inventory(
            region: RegionArg | None = None,
            services: Annotated[list[ServiceName] | None, Field(
                min_length=1, max_length=4,
                description="Only these services (e.g. [\"lambda\"] for \"show my Lambda functions\"); omit for all")] = None,
            refresh: Annotated[bool, Field(
                description="Ask for a new scan; deferred if the last one of this scope finished under 10 seconds ago")] = False,
    ) -> InventoryResult:
        """How many supported resources exist, by service and region: "What resources do I have?"

        Counts Lambda functions, DynamoDB tables, S3 buckets and EC2 instances. Scanning every enabled
        region is the slowest call; if the user did not say where, ask "all AWS regions or a specific
        region?" first, then pass region (a code such as ap-south-1, or "all"). Omitting region scans every
        enabled region. Pass services to count only some services. discovery_errors lists services that
        could not be listed (or timed out) in a region: their counts may be missing, not zero;
        inaccessible_regions lists regions where every scanned service failed. coverage_complete is false
        whenever anything failed. Reuses a scan younger than 60 seconds (see freshness).
        """
        wanted = tuple(svc for svc in SUPPORTED_SERVICES if svc in (services or ()))
        scope: Scope = (None if region in (None, "all") else (region,),
                        None if not wanted or wanted == SUPPORTED_SERVICES else wanted)
        s = await scans.get(refresh, scope)
        scan = s.scan
        pairs = Counter((f.service, f.region) for f in scan.findings)
        by_service = {svc: 0 for svc in s.services} | dict(Counter(f.service for f in scan.findings))
        by_region = {r: 0 for r in scan.regions} | dict(Counter(f.region for f in scan.findings))
        dead = inaccessible_regions(scan, s.services)
        total = len(scan.findings)
        found = sorted(((n, svc) for svc, n in by_service.items() if n), key=lambda x: (-x[0], x[1]))
        populated = sum(1 for n in by_region.values() if n)
        speech = ("A new scan was not started because the last one finished less than 10 seconds ago. "
                  if s.refresh_deferred else "")
        speech += (f"CloudGuard found {_plural(total, 'resource')} in {_plural(populated, 'region')} "
                  f"of {_plural(len(scan.regions), 'scanned region')}"
                  + (": " + ", ".join(f"{n} {svc}" for n, svc in found) + "." if total else "."))
        if dead:
            speech += f" {_plural(len(dead), 'region')} could not be read at all."
        if scan.service_errors:
            speech += " Some services could not be listed, so this inventory is incomplete."
        return InventoryResult(
            **common("get_resource_inventory", s, attention(scan.service_errors), speech, policy=False),
            services_scanned=list(s.services), total_resources=total, by_service=dict(sorted(by_service.items())), by_region=dict(sorted(by_region.items())),
            counts=[InventoryCount(service=svc, region=r, count=n) for (svc, r), n in sorted(pairs.items())],
            coverage_complete=not scan.service_errors, discovery_errors=discovery_errors(scan),
            inaccessible_regions=dead,
            resources=[InventoryItem(name=f.resource_name, service=f.service, region=f.region,
                                     resource_type=f.resource_type, finding_id=f.finding_id)
                       for f in sorted(scan.findings, key=lambda f: (f.service, f.region, f.resource_name.lower()))
                       ][:MAX_PAGE],
            resources_truncated=len(scan.findings) > MAX_PAGE)

    @server.tool(annotations=_annotations(ctx, "Tag compliance scan"), structured_output=True)
    async def scan_tag_compliance(refresh: Annotated[bool, Field(
            description="Ask for a new scan; deferred if the last one finished under 10 seconds ago")] = False,
    ) -> TagComplianceResult:
        """Check every supported resource against the tagging policy: "Are my resources tagged properly?"

        Read-only scan of Lambda functions, DynamoDB tables, S3 buckets and EC2 instances in every enabled
        region, against the active stored policy or the built-in default (mandatory Environment, Project,
        Owner, CostCenter); policy and policy_source say which. Each resource is compliant, non-compliant
        (missing or invalid mandatory tags) or unknown (tags could not be read); compliance_percentage
        excludes unknown. Returns breakdowns by service and region, how often each mandatory tag is
        missing, invalid values, and discovery and evaluation errors. A refresh less than 10 seconds after
        the last scan is deferred (freshness.refresh_deferred). Tag expiry is not supported.
        """
        s = await scans.get(refresh)
        scan = s.scan
        findings = scan.findings
        totals = _breakdown(findings)
        by_service = {svc: _breakdown([f for f in findings if f.service == svc])
                      for svc in sorted({f.service for f in findings})}
        by_region = {r: _breakdown([f for f in findings if f.region == r])
                     for r in sorted(set(scan.regions) | {f.region for f in findings})}
        missing = Counter(k for f in findings for k in f.missing_tags)
        invalid = Counter((i.key, i.value, i.reason, i.mandatory) for f in findings for i in f.invalid_tags)
        failures = [EvaluationError(finding_id=f.finding_id, resource_arn=f.resource_arn,
                                    resource_type=f.resource_type, region=f.region,
                                    error=SafeError.of(f.inspection_error))
                    for f in findings if f.compliance_status == ComplianceStatus.INSPECTION_FAILED]
        dead = inaccessible_regions(scan)
        complete = not scan.service_errors and not failures
        speech = ("A new scan was not started because the last one finished less than 10 seconds ago. "
                  if s.refresh_deferred else "")
        if totals.total == 0:
            speech += f"No supported resources were found in {_plural(len(scan.regions), 'region')}."
        else:
            speech += (f"{totals.non_compliant} of {_plural(totals.total, 'resource')} are missing mandatory tags "
                       f"or have invalid values, across {_plural(len(scan.regions), 'region')}.")
            if missing:
                key, n = min(missing.items(), key=lambda kv: (-kv[1], kv[0]))  # same order as the list
                speech += f" The most often missing tag is {key}, on {n}."
        if failures:
            speech += f" {len(failures)} could not be inspected."
        if scan.service_errors:
            speech += " Some services could not be listed, so the scan is incomplete."
        return TagComplianceResult(
            **common("scan_tag_compliance", s, attention(totals.non_compliant or not complete), speech),
            total_discovered=totals.total, compliant=totals.compliant, non_compliant=totals.non_compliant,
            unknown=totals.unknown, compliance_percentage=totals.compliance_percentage,
            by_service=by_service, by_region=by_region,
            missing_mandatory_tags=[TagFrequency(key=k, count=n)
                                    for k, n in sorted(missing.items(), key=lambda kv: (-kv[1], kv[0]))],
            invalid_values=[InvalidValueCount(key=k, value=v, reason=r, mandatory=m, count=n)
                            for (k, v, r, m), n in sorted(invalid.items(), key=lambda kv: (-kv[1], kv[0][:2]))],
            total_missing_tags=sum(missing.values()), total_invalid_values=sum(invalid.values()),
            resources_with_notices=sum(1 for f in findings if f.notices),
            coverage_complete=complete, discovery_errors=discovery_errors(scan), inaccessible_regions=dead,
            evaluation_errors=failures)

    @server.tool(annotations=_annotations(ctx, "Search resources by name"), structured_output=True)
    async def search_resources(
            text: Annotated[str, Field(min_length=1, max_length=128, pattern=r"^[^\x00-\x1f\x7f]+$",
                                       description="Part of a resource name, as the user said it (ecommerce, user)")],
            match: Annotated[NameMatch, Field(description="How the name must relate to text; case is ignored")] = "contains",
            services: Services = None,
            region: RegionArg | None = None,
            limit: Annotated[int, Field(ge=1, le=MAX_PAGE, description="Resources per page")] = 20,
            cursor: Cursor | None = None,
    ) -> SearchResult:
        """Find resources by NAME: "Lambda functions containing ecommerce", "tables starting with user".

        Matches discovered Lambda function, DynamoDB table, S3 bucket and EC2 instance names (an EC2
        instance's name is its Name tag, else its instance ID), ignoring case: contains, starts_with,
        ends_with or exact. This is a name filter, not a tag filter. Every enabled region unless region
        is given; services narrows the types. Results list name, type, region, the service's own ID, ARN
        and finding_id, in pages (pass next_cursor with the same arguments). Resources that share a name
        are listed separately and named in duplicate_names. Never invents results: only scanned resources.
        """
        wanted = tuple(svc for svc in SUPPORTED_SERVICES if svc in (services or ()))
        scope: Scope = (None if region in (None, "all") else (region,),
                        None if not wanted or wanted == SUPPORTED_SERVICES else wanted)
        offset = 0
        if cursor is None:
            s = await scans.get(scope=scope)
        else:
            cursor_scan, raw_offset = cursor.rsplit(":", 1)
            offset = int(raw_offset)
            found = scans.by_id(cursor_scan)  # never scan for a cursor
            if found is None:
                raise ToolError("this cursor has expired or belongs to a scan that was replaced; "
                                "search again without a cursor")
            s = replace(found[1], reused=True)
            if found[0] != scope and (narrowed := _narrow(s, scope)) is not None:
                s = replace(narrowed, reused=True)
        needle = text.casefold()
        test = {"contains": lambda n: needle in n, "starts_with": lambda n: n.startswith(needle),
                "ends_with": lambda n: n.endswith(needle), "exact": lambda n: n == needle}[match]
        rows = sorted((f for f in s.scan.findings if f.service in s.services and test(f.resource_name.casefold())),
                      key=lambda f: (f.service, f.resource_name.casefold(), f.region, f.resource_arn))
        if offset and offset >= len(rows):
            raise ToolError(f"offset {offset} is past the last match ({len(rows)} in total); search again without "
                            "a cursor")
        page = rows[offset:offset + limit]
        end = offset + len(page)
        counts = Counter(f.resource_name.casefold() for f in rows)
        duplicates = sorted({f.resource_name for f in rows if counts[f.resource_name.casefold()] > 1})
        noun = SERVICE_NOUN[s.services[0]] if len(s.services) == 1 else "resource"
        how = {"contains": "contains", "starts_with": "starts with", "ends_with": "ends with",
               "exact": "exactly matches"}[match]
        regions = {f.region for f in rows}
        if rows:
            speech = (f"{_plural(len(rows), noun)} {'has a name that' if len(rows) == 1 else 'have a name that'} "
                      f"{how} that text, in {_plural(len(regions), 'region')}.")
            if end < len(rows):
                speech += f" Showing {offset + 1} to {end}."
            if duplicates:
                speech += f" {_plural(len(duplicates), 'name')} {'is' if len(duplicates) == 1 else 'are'} shared by " \
                          "more than one resource."
        else:
            speech = f"No scanned {noun} has a name that {how} that text." if match != "exact" else \
                f"No scanned {noun} has exactly that name."
        if s.scan.service_errors:
            speech += " Some services could not be listed, so there may be more."
        return SearchResult(
            **common("search_resources", s, attention(s.scan.service_errors), speech, policy=False),
            match=match, services_scanned=list(s.services), total=len(rows), offset=offset, returned=len(page),
            next_cursor=f"{s.scan.scan_id}:{end}" if end < len(rows) else None, duplicate_names=duplicates,
            coverage_complete=not s.scan.service_errors, discovery_errors=discovery_errors(s.scan),
            resources=[SearchHit(name=f.resource_name, service=f.service, resource_type=f.resource_type,
                                 region=f.region, resource_id=f.resource_id, resource_arn=f.resource_arn,
                                 finding_id=f.finding_id) for f in page])

    class _ScanSource:
        """The latest scan as a discovery source, so a preview reads no AWS API beyond the scan it reuses."""

        def __init__(self, s: _Scan):
            self.mode, self._s = ctx.mode, s

        def discover(self, regions=None, services=None) -> DiscoveryResult:
            scan = self._s.scan
            return DiscoveryResult(account_id=scan.account_id, regions=list(scan.regions), service_errors=[], resources=[
                DiscoveredResource(account_id=f.account_id, resource_type=f.resource_type, service=f.service,
                                   resource_id=f.resource_id, resource_name=f.resource_name,
                                   resource_arn=f.resource_arn, region=f.region, discovered_at=f.scanned_at,
                                   tags=f.existing_tags, tag_source=f.evidence.source_api,
                                   tag_error=f.inspection_error if f.existing_tags is None else None)
                for f in scan.findings])

    @server.tool(annotations=_annotations(ctx, "Preview tag changes"), structured_output=True)
    async def preview_tag_changes(
            tags: Annotated[dict[str, str], Field(min_length=1, max_length=10,
                                                  description="Tags to add (or, with overwrite, the one tag to update)")],
            service: Annotated[ServiceName | None, Field(description="Only this resource type")] = None,
            name_match: Annotated[NameMatch | None, Field(description="With name_text: how names must match")] = None,
            name_text: Annotated[str | None, Field(min_length=1, max_length=128, pattern=r"^[^\x00-\x1f\x7f]+$",
                                                   description="Part of the resource names to tag")] = None,
            finding_ids: Annotated[list[FindingId] | None, Field(
                min_length=1, max_length=MAX_PAGE,
                description="An explicit selection, e.g. the finding_ids search_resources returned")] = None,
            resource_name: Annotated[str | None, Field(
                min_length=1, max_length=128, pattern=r"^[^\x00-\x1f\x7f]+$",
                description="One resource by its exact name (case-sensitive); several matches or none are asked "
                            "about, never guessed")] = None,
            region: NameRegion | None = None,
            overwrite: Annotated[bool, Field(description="Update an existing value (exactly one tag; the "
                                                         "overwrite must be acknowledged at approval)")] = False,
    ) -> TagPreviewResult:
        """Preview a tagging change, without making it: "Add Environment=Production to the ecommerce Lambdas".

        Read-only dry run. Builds the same proposal `proposal save` would (every matching resource, its
        ARN and region, its current tags, the tags that would be added, values that would be overwritten,
        conflicts and policy violations) from the latest scan, and returns `request`, the exact request to
        save, and `summary` (count, names, regions, proposed tags, overwrites). Target every resource of a
        service ("all Lambdas": service), one resource by exact name (resource_name: duplicates across
        regions come back as candidates to choose from, and no exact match offers similar names, never
        selected automatically), a name pattern (name_match + name_text), or finding_ids; region narrows
        any of them. Without overwrite, an existing different value is a conflict and is never changed.
        Nothing is written to AWS and nothing is stored: saving, approving and executing a proposal happen
        in the authenticated CLI, with separation of duties (see next_step).
        """
        if (name_match is None) != (name_text is None):
            raise ToolError("give name_match and name_text together")
        if resource_name is not None and (name_text or finding_ids):
            raise ToolError("give one resource_name, or a name filter or finding_ids, not both")
        if not (service or name_text or finding_ids or resource_name):
            raise ToolError("say which resources: a service, a name filter, finding_ids or a resource_name")
        if overwrite and len(tags) != 1:
            raise ToolError("an overwrite changes exactly one tag")
        s = await scans.get()
        rtype = {f.service: f.resource_type for f in s.scan.findings}.get(service) if service else None
        if service and rtype is None:
            rtype = {"lambda": "AWS::Lambda::Function", "dynamodb": "AWS::DynamoDB::Table", "s3": "AWS::S3::Bucket",
                     "ec2": "AWS::EC2::Instance"}[service]
        arns = None
        if finding_ids:
            by_id = {f.finding_id: f.resource_arn for f in s.scan.findings}
            unknown = [i for i in finding_ids if i not in by_id]
            if unknown:
                raise ToolError(f"{len(unknown)} finding_id(s) are not in the latest scan ({s.scan.scan_id}); search again")
            arns = tuple(dict.fromkeys(by_id[i] for i in finding_ids))
        try:
            if resource_name is not None:  # one resource: exact name, resolved by the server's own scan
                target = TargetCriteria(resource_type=rtype, region=region, name=resource_name)
                op = TaggingOperation.UPDATE_TAG if overwrite else TaggingOperation.ADD_TAG
            else:
                target = TargetCriteria(resource_type=rtype, region=region, all_of_type=True, arns=arns,
                                        name_filter=NameFilter(match=name_match, text=name_text) if name_text else None)
                op = TaggingOperation.BULK_UPDATE_TAG if overwrite else TaggingOperation.BULK_ADD_TAG
            request = TaggingRequest(operation=op, target=target, tags=tags, raw_text="preview_tag_changes",
                                     parsed_by="mcp")
        except ValueError as exc:
            raise ToolError(f"invalid tagging request: {exc}") from None
        policy, _source = ctx.policy()
        outcome = await anyio.to_thread.run_sync(
            lambda: CustomTaggingEngine(_ScanSource(s), policy, clock=ctx.clock).prepare_request(request))
        candidates = list(outcome.candidates)
        if resource_name is not None and outcome.status.value == "NOT_FOUND":
            # No exact name: offer names that contain it, for the user to choose; never selected automatically.
            needle = resource_name.casefold()
            candidates = [Candidate(resource_type=f.resource_type, resource_name=f.resource_name,
                                    resource_arn=f.resource_arn, region=f.region)
                          for f in s.scan.findings
                          if needle in f.resource_name.casefold() and (rtype is None or f.resource_type == rtype)
                          and (region is None or f.region == region)][:20]
        summary = None
        if outcome.proposal is not None:
            records = [r for r in outcome.proposal.records if r.outcome.value != "OUT_OF_SCOPE"]
            summary = PreviewSummary(
                matched=len(records), changing=sum(1 for r in records if r.changes),
                resource_names=[r.resource_name for r in records], regions=sorted({r.region for r in records}),
                proposed_tags=dict(tags), conflicts=sum(len(r.conflicts) for r in records),
                overwrites=[PreviewOverwrite(resource_name=r.resource_name, region=r.region, key=c.key,
                                             current_value=c.old_value or "", new_value=c.new_value)
                            for r in records for c in r.changes if c.action.value == "OVERWRITE"])
        return TagPreviewResult(
            **common("preview_tag_changes", s, Outcome.ANSWERED, outcome.message + " Nothing was stored.",
                     policy=False),
            status=outcome.status.value, message=outcome.message, summary=summary, request=outcome.request,
            proposal=outcome.proposal, candidates=candidates)

    @server.tool(annotations=_annotations(ctx, "Untagged resources"), structured_output=True)
    async def list_untagged_resources(
            limit: Annotated[int, Field(ge=1, le=MAX_PAGE, description="Resources per page")] = 20,
            cursor: Cursor | None = None,
            services: Services = None,
            region: RegionArg | None = None,
    ) -> UntaggedResourcesResult:
        """Resources missing mandatory tags, one page at a time: "Which resources are untagged?"

        Lists non-compliant resources that miss at least one mandatory tag, most missing tags first, with
        name, ARN, type, region, account, missing tags, observed tags, the applicable policy rules and the
        evidence (source API and observation time). Pass services to list only some resource types
        ("untagged Lambdas": ["lambda"]). Pass region to cover one region (a code such as ap-south-1);
        omitted or "all" covers every enabled region. Pass next_cursor (with the same services and
        region) to get the next page; it is null on the last page. A cursor belongs to one scan and
        expires with it (60 seconds): an expired or replaced cursor is refused without scanning, so start
        again without a cursor. Resources whose tags could not be read are counted in `unknown`, not listed.
        """
        wanted = set(services or SUPPORTED_SERVICES)
        regions = None if region in (None, "all") else (region,)
        offset = 0
        if cursor is None:
            s = await scans.get(scope=(regions, None))
        else:
            cursor_scan, raw_offset = cursor.rsplit(":", 1)
            offset = int(raw_offset)
            found = scans.by_id(cursor_scan)  # never scan for a cursor: its scan is either cached or gone
            if found is None or found[0] not in (FULL, (regions, None)):
                raise ToolError("this cursor has expired or belongs to a scan that was replaced; "
                                "list again without a cursor")
            s = replace(found[1], reused=True)
            if found[0] != (regions, None) and (narrowed := _narrow(s, (regions, None))) is not None:
                s = replace(narrowed, reused=True)
        scan = s.scan
        rows = sorted((f for f in scan.findings if _missing(f) and f.service in wanted),
                      key=lambda f: (-len(f.missing_tags), f.service, f.region, f.resource_arn))
        if offset and offset >= len(rows):
            raise ToolError(f"offset {offset} is past the last resource ({len(rows)} in total); "
                            "list again without a cursor")
        page = rows[offset:offset + limit]
        end = offset + len(page)
        unknown = sum(f.compliance_status == ComplianceStatus.INSPECTION_FAILED and f.service in wanted
                      for f in scan.findings)
        rules = {t: s.policy.document.rules_for(t) for t in {f.resource_type for f in page}}
        noun = SERVICE_NOUN[next(iter(wanted))] if len(wanted) == 1 else "resource"
        if rows:
            speech = (f"{_plural(len(rows), noun)} {'is' if len(rows) == 1 else 'are'} missing mandatory "
                      f"tags. Showing {offset + 1} to {end}.")
        else:
            speech = f"No inspected {noun} is missing a mandatory tag."
        if unknown:
            speech += f" {unknown} could not be inspected."
        return UntaggedResourcesResult(
            **common("list_untagged_resources", s, attention(rows or unknown or scan.service_errors), speech),
            total=len(rows), offset=offset, returned=len(page),
            next_cursor=f"{scan.scan_id}:{end}" if end < len(rows) else None, unknown=unknown,
            coverage_complete=not scan.service_errors and not unknown,
            resources=[UntaggedResource(
                finding_id=f.finding_id, resource_arn=f.resource_arn, resource_id=f.resource_id,
                resource_name=f.resource_name, resource_type=f.resource_type, service=f.service, region=f.region,
                account_id=f.account_id, missing_tags=f.missing_tags, invalid_tags=[i.key for i in f.invalid_tags],
                observed_tags=f.existing_tags or {}, applicable_rules=rules[f.resource_type],
                evidence_source_api=f.evidence.source_api, observed_at=f.evidence.retrieved_at) for f in page])

    @server.tool(annotations=_annotations(ctx, "Explain a finding"), structured_output=True)
    async def explain_finding(
            finding_id: FindingId | None = None,
            resource_name: ResourceName | None = None,
            service: Annotated[ServiceName | None, Field(description="With resource_name: only this service")] = None,
            region: NameRegion | None = None,
    ) -> FindingExplanation:
        """Why one resource is or isn't compliant, with evidence: "Why is OrderProcessor non-compliant?"

        Give either finding_id, or resource_name (optionally with service and region to tell resources
        with the same name apart). A name is matched against the latest scan; when several resources
        match, or none does, the error lists the candidates instead of guessing. Looks the finding up in
        the latest scan (reused if younger than 60 seconds) and returns the full finding (raw tags,
        missing and invalid tags, advisory notices, evidence), the policy rules that apply to its
        resource type, and the tag changes that would make it compliant. Speech names the resource by
        service, region and finding number, never by its name or tag values. Explanations are
        deterministic; nothing is changed in AWS.
        """
        if (finding_id is None) == (resource_name is None):
            raise ToolError("give either finding_id or resource_name")
        s = await scans.get()
        if resource_name is not None:
            number, finding = find_by_name(s.scan.findings, resource_name, service, region)
        else:
            number, finding = next(((i, f) for i, f in enumerate(s.scan.findings, 1) if f.finding_id == finding_id),
                                   (0, None))
        if finding is None:
            raise ToolError(f"{finding_id} is not in the latest scan ({s.scan.scan_id}); run scan_tag_compliance")
        rules = s.policy.document.rules_for(finding.resource_type)
        by_key = {r.key: r for r in rules}
        actions: list[str] = []  # structured detail; may quote untrusted values, so never spoken
        for key in finding.missing_tags:
            rule = by_key.get(key)
            hint = f" ({rule.description})" if rule and rule.description else ""
            allowed = f", one of {', '.join(rule.allowed_values)}" if rule and rule.allowed_values else ""
            actions.append(f"Add tag {key}{hint}{allowed}.")
        for bad in finding.invalid_tags:
            rule = by_key.get(bad.key)
            if bad.reason == InvalidTagReason.EMPTY_VALUE:
                actions.append(f"Give tag {bad.key} a non-empty value.")
            else:
                allowed = ", ".join(rule.allowed_values) if rule else "an allowed value"
                actions.append(f"Change tag {bad.key} from {_q(bad.value)} to one of {allowed}.")
        # Speech and explanation use only policy keys, the service, the region and the finding number.
        label = f"{finding.service} resource in {finding.region} (finding {number} of {len(s.scan.findings)})"
        if finding.compliance_status == ComplianceStatus.INSPECTION_FAILED:
            err = SafeError.of(finding.inspection_error)
            explanation = (f"The tags of the {label} could not be read ({err.code} from {err.operation}), so its "
                           "compliance is unknown. Check the inspector role's permissions and scan again.")
            speech = f"CloudGuard could not read the tags of the {label}, so its compliance is unknown."
        elif finding.compliance_status == ComplianceStatus.COMPLIANT:
            explanation = f"The {label} is compliant. {finding.evidence.summary}"
            speech = f"The {label} has all its mandatory tags."
        else:
            explanation = f"The {label} is not compliant. {finding.evidence.summary}"
            problems = []
            if finding.missing_tags:
                problems.append("missing " + ", ".join(finding.missing_tags))
            if finding.invalid_tags:
                keys = [i.key for i in finding.invalid_tags]
                problems.append(f"{', '.join(keys)} {'has' if len(keys) == 1 else 'have'} an invalid value")
            speech = (f"The {label} has {_plural(finding.violation_count, 'tag problem')}: "
                      + "; ".join(problems) + ".")
        if finding.notices:
            explanation += f" {_plural(len(finding.notices), 'advisory notice')} (do not affect compliance)."
        explanation += (f" Evidence: {finding.evidence.source_api} at {finding.evidence.retrieved_at:%Y-%m-%dT%H:%MZ}."
                        " Tag changes need a separate, approved proposal.")
        return FindingExplanation(
            **common("explain_finding", s, attention(finding.compliance_status != ComplianceStatus.COMPLIANT), speech),
            finding=SafeFinding.of(finding), finding_number=number, rules=rules, explanation=explanation,
            required_actions=actions)

    @server.tool(annotations=_annotations(ctx, "Recommend tag values"), structured_output=True)
    async def recommend_tags(
            resource: ResourceRef | None = None,
            resource_name: ResourceName | None = None,
            service: Annotated[ServiceName | None, Field(description="With resource_name: only this service")] = None,
            region: NameRegion | None = None,
    ) -> RecommendTagsResult:
        """Evidence-backed tag values for one non-compliant resource: "What tags should OrderProcessor have?"

        Takes a finding_id or the ARN of a resource in the latest scan (reused if younger than 60 seconds),
        or instead resource_name (optionally with service and region), matched as in explain_finding.
        For each missing or invalid policy tag it returns a status: RECOMMENDED (one value supported by
        evidence), NEEDS_USER_INPUT (no evidence), CONFLICT (sources disagree) or INVALID_EVIDENCE (the
        evidence failed validation). Evidence is only an alias tag on the same resource (env, team,
        cost-center, ...) or the operator evidence file configured on the server; resource names, creator
        tags and descriptions are never used, so Owner and CostCenter are never guessed. Every value is
        checked against the effective policy. An ARN of another account, or one not in the scan, is refused.
        Read-only: it writes no tag, creates no proposal and approves nothing.
        """
        if (resource is None) == (resource_name is None):
            raise ToolError("give either resource (a finding_id or ARN) or resource_name")
        s = await scans.get()
        scan = s.scan
        if resource_name is not None:
            found = [find_by_name(scan.findings, resource_name, service, region)]
        elif resource.startswith("tagcomp-"):
            found = [(i, f) for i, f in enumerate(scan.findings, 1) if f.finding_id == resource]
        else:
            account = resource.split(":")[4]
            if account and account != scan.account_id:
                raise ToolError("that ARN belongs to a different AWS account; CloudGuard only reads the account "
                                "its own credentials belong to")
            found = [(i, f) for i, f in enumerate(scan.findings, 1) if f.resource_arn == resource]
        if not found:  # never echo the caller's ARN back
            raise ToolError(f"that resource is not in the latest scan ({scan.scan_id}); "
                            "use a finding_id or ARN from list_untagged_resources")
        number, finding = found[0]
        evidence, evidence_status = assess_operator_evidence(
            ctx.operator_evidence, policy_keys(s.policy.document), scan.account_id,
            {f.resource_arn for f in scan.findings})
        if evidence_status.problems:
            log.warning("operator evidence file rejected: %s", redact("; ".join(evidence_status.problems)))
        rules = s.policy.document.rules_for(finding.resource_type)
        result = recommend(finding, rules, evidence, evidence_rejected=evidence_status.status == "rejected")
        by_status: dict[str, list[str]] = defaultdict(list)
        for rec in result.recommendations:
            by_status[rec.status].append(rec.key)
        # Speech uses only policy keys, the service, the region and the finding number: never values.
        label = f"{finding.service} resource in {finding.region} (finding {number} of {len(scan.findings)})"
        if not result.tags_readable:
            speech = f"CloudGuard could not read the tags of the {label}, so it can't recommend values."
        elif not result.recommendations:
            speech = f"The {label} has all its mandatory tags; there is nothing to recommend."
        else:
            parts = []
            defaults = [r.key for r in result.recommendations if r.status == "RECOMMENDED" and r.confidence == "low"]
            if keys := [k for k in by_status["RECOMMENDED"] if k not in defaults]:
                parts.append(f"CloudGuard recommends a value for {', '.join(keys)} from evidence.")
            if defaults:
                parts.append(f"{', '.join(defaults)} only {'has' if len(defaults) == 1 else 'have'} an "
                             "account-wide default, which a person must confirm.")
            if keys := by_status["NEEDS_USER_INPUT"]:
                parts.append(f"{', '.join(keys)} {'needs' if len(keys) == 1 else 'need'} a person to provide "
                             "the value.")
            if keys := by_status["CONFLICT"]:
                parts.append(f"The evidence for {', '.join(keys)} disagrees.")
            if keys := by_status["INVALID_EVIDENCE"]:
                parts.append(f"The evidence for {', '.join(keys)} was rejected.")
            speech = f"For the {label}: " + " ".join(parts) + " Nothing was changed."
        return RecommendTagsResult(
            **common("recommend_tags", s, attention(finding.compliance_status != ComplianceStatus.COMPLIANT), speech),
            finding_id=finding.finding_id, finding_number=number, resource_name=finding.resource_name,
            resource_arn=finding.resource_arn,
            resource_type=finding.resource_type, service=finding.service, region=finding.region,
            compliance_status=finding.compliance_status, tags_readable=result.tags_readable,
            verified_tags=result.verified_tags, recommendations=result.recommendations,
            status_counts={st: len(by_status[st]) for st in
                           ("RECOMMENDED", "NEEDS_USER_INPUT", "CONFLICT", "INVALID_EVIDENCE")},
            operator_evidence=evidence_status, rules=rules)
