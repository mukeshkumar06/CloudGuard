"""Read-only tag-governance MCP tools.

Thin adapters over cloudguard.governance: discovery (LiveSource / FixtureSource) and compliance
(run_scan / evaluate) are the existing deterministic code; nothing here decides compliance, and no LLM
is involved (`speech` and explanations are deterministic templates). Nothing writes to AWS, and no
policy is ever created or saved.

Scan reuse: one MCP call runs discovery at most once. The tools share the latest scan this server ran
while it is younger than SCAN_MAX_AGE (5 minutes), so asking for the inventory, the compliance summary
and the untagged list back to back costs one scan. `freshness` says when the scan ran and whether it
was reused; scan_tag_compliance(refresh=true) asks for a new scan.

Scan protection: at most one scan runs at a time, on one dedicated worker thread (so the boto3 Session
is never shared between threads, and governance calls can't use up the worker threads the cost tools
need). A refresh within MIN_REFRESH_INTERVAL (60 seconds) of the last scan is deferred and reported in
`freshness.refresh_deferred`. A call that needs a scan while another is running waits at most
SCAN_WAIT_SECONDS, then gets a clear "scan in progress" error instead of queueing.

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
from cloudguard.governance.compliance import run_scan
from cloudguard.governance.discovery.common import AWS_ERRORS, classify_error
from cloudguard.governance.models import (
    ComplianceStatus, ErrorKind, GovernanceScan, InspectionError, InvalidTagReason, PolicyRef, ServiceError,
    TagFinding,
)
from cloudguard.governance.policy import PolicyIntegrityError, PolicyVersion, TagRule
from cloudguard.governance.recommendations import (
    LIMITATIONS as RECOMMENDATION_LIMITATIONS, OperatorEvidenceStatus, RecommendationStatus, TagRecommendation,
    assess_operator_evidence, policy_keys, recommend,
)
from cloudguard.server.context import GovernanceContext

log = logging.getLogger(__name__)

SCHEMA_VERSION = "1"
SCAN_MAX_AGE = timedelta(minutes=5)
MIN_REFRESH_INTERVAL = timedelta(seconds=60)
SCAN_WAIT_SECONDS = 5.0  # longest a call waits for another call's scan before "scan in progress"
MAX_PAGE = 100
SUPPORTED_SERVICES = ("dynamodb", "ec2", "lambda", "s3")  # one discovery pass per service per region

GOVERNANCE_TOOL_NAMES = ("get_resource_inventory", "scan_tag_compliance", "list_untagged_resources",
                         "explain_finding", "recommend_tags")

PolicySource = Literal["stored", "built-in default"]
FindingId = Annotated[str, Field(pattern=r"^tagcomp-[0-9a-f]{16}$",
                                 description="finding_id from list_untagged_resources or scan_tag_compliance")]
ResourceRef = Annotated[str, Field(
    max_length=1100,
    pattern=r"^(tagcomp-[0-9a-f]{16}|arn:aws[a-z-]*:(lambda|dynamodb|s3|ec2):[a-z0-9-]*:(\d{12})?:\S{1,1024})$",
    description="finding_id (tagcomp-...) from list_untagged_resources, or the ARN of a scanned Lambda function, "
                "DynamoDB table, S3 bucket or EC2 instance in this account")]
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
    refresh_deferred: bool = False  # a refresh was asked for but the last scan is under a minute old
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


class InventoryCount(_Model):
    service: str
    region: str
    count: int


class InventoryResult(_Envelope):
    total_resources: int
    by_service: dict[str, int]  # every supported service; see discovery_errors before reading 0 as none
    by_region: dict[str, int]  # every scanned region, including empty ones
    counts: list[InventoryCount]  # non-zero (service, region) pairs
    coverage_complete: bool  # False if any service listing failed in any region
    discovery_errors: list[DiscoveryError]
    inaccessible_regions: list[str]  # regions where every supported service failed


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


def inaccessible_regions(scan: GovernanceScan) -> list[str]:
    """Regions where every supported service failed to list (derived from per-service ServiceErrors)."""
    failed: dict[str, set[str]] = defaultdict(set)
    for e in scan.service_errors:
        failed[e.region].add(e.service)
    return sorted(r for r, services in failed.items() if set(SUPPORTED_SERVICES) <= services)


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


class _Scans:
    """The latest scan, per server, and the guard that keeps scans single and bounded.

    All state changes happen on the event loop, so checking for a running scan and starting one can't
    interleave. The scan itself runs on a private one-thread limiter: governance never holds more than
    one worker thread, and the boto3 Session is only ever used by that thread.
    """

    def __init__(self, ctx: GovernanceContext):
        self.ctx = ctx
        self._latest: _Scan | None = None
        self._running: anyio.Event | None = None
        self._limiter: anyio.CapacityLimiter | None = None  # created on the server's event loop

    def _age(self, s: _Scan) -> timedelta:
        return self.ctx.clock() - s.scan.completed_at

    def fresh(self) -> _Scan | None:
        """The latest scan if it is still young enough to reuse; never starts a scan."""
        latest = self._latest
        return latest if latest is not None and self._age(latest) < SCAN_MAX_AGE else None

    async def get(self, refresh: bool = False) -> _Scan:
        latest = self._latest
        if latest is not None:
            age = self._age(latest)
            if refresh and age < MIN_REFRESH_INTERVAL:
                return replace(latest, reused=True, refresh_deferred=True)
            if not refresh and age < SCAN_MAX_AGE:
                return replace(latest, reused=True)
        if self._running is not None:  # someone else's scan is running: wait a bounded time for it
            running = self._running
            with anyio.move_on_after(SCAN_WAIT_SECONDS):
                await running.wait()
            if not running.is_set():
                raise ToolError("a governance scan is already running; try again in a few seconds")
            done = self._latest
            if done is None or done is latest:
                raise ToolError("the governance scan that was running did not complete; try again")
            return replace(done, reused=True)
        if self._limiter is None:
            self._limiter = anyio.CapacityLimiter(1)
        self._running = event = anyio.Event()
        try:
            self._latest = await anyio.to_thread.run_sync(self._scan, limiter=self._limiter)
            return self._latest
        finally:
            self._running = None
            event.set()

    def _scan(self) -> _Scan:
        try:
            policy, source = self.ctx.policy()
            scan = run_scan(self.ctx.source, policy)
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
        return _Scan(scan, policy, source)


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
    async def get_resource_inventory() -> InventoryResult:
        """How many supported resources exist, by service and region: "What resources do I have?"

        Counts Lambda functions, DynamoDB tables, S3 buckets and EC2 instances in every enabled region.
        discovery_errors lists services that could not be listed in a region (their counts may be missing,
        not zero); inaccessible_regions lists regions where every service failed. coverage_complete is
        false whenever anything failed. Reuses a scan younger than 5 minutes (see freshness).
        """
        s = await scans.get()
        scan = s.scan
        pairs = Counter((f.service, f.region) for f in scan.findings)
        by_service = {svc: 0 for svc in SUPPORTED_SERVICES} | dict(Counter(f.service for f in scan.findings))
        by_region = {r: 0 for r in scan.regions} | dict(Counter(f.region for f in scan.findings))
        dead = inaccessible_regions(scan)
        total = len(scan.findings)
        found = sorted(((n, svc) for svc, n in by_service.items() if n), key=lambda x: (-x[0], x[1]))
        populated = sum(1 for n in by_region.values() if n)
        speech = (f"CloudGuard found {_plural(total, 'resource')} in {_plural(populated, 'region')} "
                  f"of {_plural(len(scan.regions), 'scanned region')}"
                  + (": " + ", ".join(f"{n} {svc}" for n, svc in found) + "." if total else "."))
        if dead:
            speech += f" {_plural(len(dead), 'region')} could not be read at all."
        if scan.service_errors:
            speech += " Some services could not be listed, so this inventory is incomplete."
        return InventoryResult(
            **common("get_resource_inventory", s, attention(scan.service_errors), speech, policy=False),
            total_resources=total, by_service=dict(sorted(by_service.items())), by_region=dict(sorted(by_region.items())),
            counts=[InventoryCount(service=svc, region=r, count=n) for (svc, r), n in sorted(pairs.items())],
            coverage_complete=not scan.service_errors, discovery_errors=discovery_errors(scan),
            inaccessible_regions=dead)

    @server.tool(annotations=_annotations(ctx, "Tag compliance scan"), structured_output=True)
    async def scan_tag_compliance(refresh: Annotated[bool, Field(
            description="Ask for a new scan; deferred if the last one finished under a minute ago")] = False,
    ) -> TagComplianceResult:
        """Check every supported resource against the tagging policy: "Are my resources tagged properly?"

        Read-only scan of Lambda functions, DynamoDB tables, S3 buckets and EC2 instances in every enabled
        region, against the active stored policy or the built-in default (mandatory Environment, Project,
        Owner, CostCenter); policy and policy_source say which. Each resource is compliant, non-compliant
        (missing or invalid mandatory tags) or unknown (tags could not be read); compliance_percentage
        excludes unknown. Returns breakdowns by service and region, how often each mandatory tag is
        missing, invalid values, and discovery and evaluation errors. A refresh less than a minute after
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
        speech = ("A new scan was not started because the last one finished less than a minute ago. "
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

    @server.tool(annotations=_annotations(ctx, "Untagged resources"), structured_output=True)
    async def list_untagged_resources(
            limit: Annotated[int, Field(ge=1, le=MAX_PAGE, description="Resources per page")] = 20,
            cursor: Cursor | None = None,
    ) -> UntaggedResourcesResult:
        """Resources missing mandatory tags, one page at a time: "Which resources are untagged?"

        Lists non-compliant resources that miss at least one mandatory tag, most missing tags first, with
        ARN, type, region, account, missing tags, observed tags, the applicable policy rules and the
        evidence (source API and observation time). Pass next_cursor to get the next page; it is null on
        the last page. A cursor belongs to one scan and expires with it (5 minutes): an expired or
        replaced cursor is refused without scanning, so start again without a cursor. Resources whose
        tags could not be read are counted in `unknown`, not listed.
        """
        offset = 0
        if cursor is None:
            s = await scans.get()
        else:
            cursor_scan, raw_offset = cursor.rsplit(":", 1)
            offset = int(raw_offset)
            current = scans.fresh()  # never scan for a cursor: its scan is either cached or gone
            if current is None or current.scan.scan_id != cursor_scan:
                raise ToolError("this cursor has expired or belongs to a scan that was replaced; "
                                "list again without a cursor")
            s = replace(current, reused=True)
        scan = s.scan
        rows = sorted((f for f in scan.findings if _missing(f)),
                      key=lambda f: (-len(f.missing_tags), f.service, f.region, f.resource_arn))
        if offset and offset >= len(rows):
            raise ToolError(f"offset {offset} is past the last resource ({len(rows)} in total); "
                            "list again without a cursor")
        page = rows[offset:offset + limit]
        end = offset + len(page)
        unknown = sum(f.compliance_status == ComplianceStatus.INSPECTION_FAILED for f in scan.findings)
        rules = {t: s.policy.document.rules_for(t) for t in {f.resource_type for f in page}}
        if rows:
            speech = (f"{_plural(len(rows), 'resource')} {'is' if len(rows) == 1 else 'are'} missing mandatory "
                      f"tags. Showing {offset + 1} to {end}.")
        else:
            speech = "No inspected resource is missing a mandatory tag."
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
    async def explain_finding(finding_id: FindingId) -> FindingExplanation:
        """Why one resource is or isn't compliant, with evidence: "Explain this finding."

        Looks the finding up in the latest scan (reused if younger than 5 minutes) and returns the full
        finding (raw tags, missing and invalid tags, advisory notices, evidence), the policy rules that
        apply to its resource type, and the tag changes that would make it compliant. Speech names the
        resource by service, region and finding number, never by its name or tag values. Explanations
        are deterministic; nothing is changed in AWS.
        """
        s = await scans.get()
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
    async def recommend_tags(resource: ResourceRef) -> RecommendTagsResult:
        """Evidence-backed tag values for one non-compliant resource: "What should this resource's tags be?"

        Takes a finding_id or the ARN of a resource in the latest scan (reused if younger than 5 minutes).
        For each missing or invalid policy tag it returns a status: RECOMMENDED (one value supported by
        evidence), NEEDS_USER_INPUT (no evidence), CONFLICT (sources disagree) or INVALID_EVIDENCE (the
        evidence failed validation). Evidence is only an alias tag on the same resource (env, team,
        cost-center, ...) or the operator evidence file configured on the server; resource names, creator
        tags and descriptions are never used, so Owner and CostCenter are never guessed. Every value is
        checked against the effective policy. An ARN of another account, or one not in the scan, is refused.
        Read-only: it writes no tag, creates no proposal and approves nothing.
        """
        s = await scans.get()
        scan = s.scan
        if resource.startswith("tagcomp-"):
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
            finding_id=finding.finding_id, finding_number=number, resource_arn=finding.resource_arn,
            resource_type=finding.resource_type, service=finding.service, region=finding.region,
            compliance_status=finding.compliance_status, tags_readable=result.tags_readable,
            verified_tags=result.verified_tags, recommendations=result.recommendations,
            status_counts={st: len(by_status[st]) for st in
                           ("RECOMMENDED", "NEEDS_USER_INPUT", "CONFLICT", "INVALID_EVIDENCE")},
            operator_evidence=evidence_status, rules=rules)
