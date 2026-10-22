"""Alexa response projection: what an `alexa:read` caller may receive, for each of the twelve Alexa tools.

Speech is already sanitized by the tools themselves, but an MCP client can read the whole structured result
(and the text block, which is a JSON copy of it). So Alexa never gets the operator result: it gets a separate,
purpose-built model, built by COPYING named, safe fields out of the operator result (an allowlist), never by
deleting fields from it. A field that is added to an operator contract later is therefore never sent to Alexa
until it is added here on purpose.

One deliberate exception: get_aws_account_identity answers "Which AWS account am I using?", so its answer carries
the account ID of the server's own credentials and the identity TYPE (iam_user, assumed_role, ...). Even there the
principal ARN, IAM user or role name, session name and STS UserId are never projected, and no other tool's answer
carries an account ID.

Never projected (apart from that one account ID): account IDs, principal or resource ARNs, resource IDs and names, raw tag values (observed,
current, invalid or recommended free-form values), operator evidence (values, sources, hash, problems), policy
identity and store, data_source (names fixtures and paths), AWS error messages and descriptions, CloudGuard's
inferred causes, scan IDs, rule descriptions and aliases.

Projected values are only: numbers and dates computed by CloudGuard; AWS-defined service, region, usage-type,
charge-category and error-code names; policy tag keys and the policy's own canonical allowed values; CloudGuard
status codes; and CloudGuard's own template text (speech, explanations, limitation notes). Nothing is invented:
every projected value is copied from the operator result or counted from it.
"""

from __future__ import annotations

import json
import re
from datetime import date, datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict

ALEXA_SCHEMA_VERSION = "alexa-1"
MAX_ITEMS = 5  # longest list in any Alexa answer (services, drivers, anomalies, resources)
_TOKEN = re.compile(r"^[A-Za-z0-9._:-]{1,64}$")


class _A(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")


def _token(value: Any) -> str | None:
    return value if isinstance(value, str) and _TOKEN.match(value) else None


# ---------- shared ----------

class AlexaFreshness(_A):
    generated_at: datetime
    observed_at: datetime | None  # when AWS (or the fixture) answered, or when the scan completed
    reused: bool = False  # the answer reused an earlier scan
    estimated: bool = False  # AWS marked figures as estimated


class _Answer(_A):
    schema_version: str = ALEXA_SCHEMA_VERSION
    tool: str
    outcome: Literal["answered", "findings", "unavailable"]
    mode: Literal["fixture", "live"]
    speech: str
    freshness: AlexaFreshness


def _base(r: dict, observed: Any, reused: bool = False, estimated: bool = False) -> dict:
    fresh = r["freshness"]
    return dict(tool=r["tool"], outcome=r["outcome"], mode=r["mode"], speech=r["speech"],
                freshness=AlexaFreshness(generated_at=fresh["generated_at"], observed_at=observed, reused=reused,
                                         estimated=estimated))


# ---------- cost ----------

class AlexaPeriod(_A):
    label: str
    start: date
    end_exclusive: date
    days: int


class AlexaMonthTotals(_A):
    charges: str  # before discounts, credits and refunds
    usage: str
    fees: str
    tax: str
    discounts: str
    credits: str
    refunds: str
    calculated_net: str  # CloudGuard calculation, not the AWS invoice


class AlexaServiceAmount(_A):
    service: str  # AWS service name
    amount: str


class AlexaChange(_A):
    previous_charges: str
    change: str
    change_percent: str | None


class _CostAnswer(_Answer):
    currency: str
    as_of: date
    warnings: list[str]
    error_code: str | None  # AWS error code only, never its message


def _cost_base(r: dict) -> dict:
    report = r.get("report") or {}
    err = report.get("error") or None
    return _base(r, r["freshness"].get("retrieved_at"), estimated=bool(r["freshness"].get("estimated"))) | dict(
        currency=r["currency"], as_of=r["as_of"],
        warnings=[w for w in (report.get("warnings") or []) if _token(w)],
        error_code=_token(err.get("code")) if isinstance(err, dict) else None)


def _period(p: dict | None) -> AlexaPeriod | None:
    return AlexaPeriod(label=p["label"], start=p["start"], end_exclusive=p["end_exclusive"], days=p["days"]) if p else None


def _totals(m: dict | None) -> AlexaMonthTotals | None:
    if not m:
        return None
    return AlexaMonthTotals(charges=m["charges_before_discounts_credits_and_refunds"], usage=m["usage"], fees=m["fees"],
                            tax=m["tax"], discounts=m["discounts"], credits=m["credits"], refunds=m["refunds"],
                            calculated_net=m["calculated_net"])


def _top(services: list[dict] | None) -> list[AlexaServiceAmount]:
    rows = sorted(services or [], key=lambda s: -float(s["amount"]))
    return [AlexaServiceAmount(service=s["service"], amount=s["amount"]) for s in rows[:MAX_ITEMS]]


class AlexaBill(_CostAnswer):
    is_final_bill: bool
    period: AlexaPeriod | None
    totals: AlexaMonthTotals | None
    top_services: list[AlexaServiceAmount]  # at most 5, largest first
    service_count: int
    previous_month: AlexaChange | None


def project_bill(r: dict) -> AlexaBill:
    report = r.get("report") or {}
    month = report.get("month")
    cmp = report.get("comparison")
    return AlexaBill(**_cost_base(r), is_final_bill=r["is_final_bill"], period=_period(r.get("period")),
                     totals=_totals(month), top_services=_top(month and month.get("services")),
                     service_count=len((month or {}).get("services") or []),
                     previous_month=AlexaChange(previous_charges=cmp["previous_charges"], change=cmp["change"],
                                                change_percent=cmp.get("change_percent")) if cmp else None)


class AlexaRankedService(_A):
    rank: int
    service: str
    amount: str


class AlexaBreakdown(_CostAnswer):
    is_final_bill: bool
    period: AlexaPeriod | None
    basis: str | None
    services: list[AlexaRankedService]  # at most 5
    total_services: int
    excluded_tax: str | None


def project_breakdown(r: dict) -> AlexaBreakdown:
    ranking = r.get("ranking") or {}
    return AlexaBreakdown(**_cost_base(r), is_final_bill=r["is_final_bill"], period=_period(r.get("period")),
                          basis=ranking.get("basis"),
                          services=[AlexaRankedService(rank=s["rank"], service=s["service"], amount=s["amount"])
                                    for s in (ranking.get("services") or [])[:MAX_ITEMS]],
                          total_services=ranking.get("total_services") or 0, excluded_tax=ranking.get("excluded_tax"))


class AlexaDriver(_A):
    kind: str  # service | category | adjustment
    name: str  # AWS service or charge-category name
    change: str
    share_of_gross_increase_percent: str | None


class AlexaComparison(_CostAnswer):
    earlier: AlexaPeriod | None
    later: AlexaPeriod | None
    earlier_charges: str | None
    later_charges: str | None
    charges_change: str | None
    charges_change_percent: str | None
    net_change: str | None
    verdict: str | None
    drivers: list[AlexaDriver]  # at most 5
    offsets: list[AlexaDriver]  # at most 5
    limitation: str | None  # CloudGuard's note that attribution is not a cause


def project_comparison(r: dict) -> AlexaComparison:
    report = r.get("report") or {}
    exp = report.get("explanation") or {}

    def drivers(rows):
        return [AlexaDriver(kind=d["kind"], name=d["name"], change=d["change"],
                            share_of_gross_increase_percent=d.get("share_of_gross_increase_percent"))
                for d in (rows or [])[:MAX_ITEMS]]

    def charges(m):
        return m["charges_before_discounts_credits_and_refunds"] if m else None
    return AlexaComparison(
        **_cost_base(r), earlier=_period(report.get("earlier")), later=_period(report.get("later")),
        earlier_charges=charges(report.get("earlier_month")), later_charges=charges(report.get("later_month")),
        charges_change=report.get("charges_change"), charges_change_percent=report.get("charges_change_percent"),
        net_change=report.get("net_change"), verdict=_token(exp.get("verdict")), drivers=drivers(exp.get("drivers")),
        offsets=drivers(exp.get("offsets")), limitation=exp.get("limitation"))


class AlexaAnomaly(_A):
    service: str
    region: str | None
    start_date: date | None
    end_date: date | None
    expected_spend: str | None
    actual_spend: str | None
    impact: str | None
    impact_percent: str | None


class AlexaAnomalies(_CostAnswer):
    status: str | None
    window_start: date | None
    window_end: date | None
    monitor_count: int | None
    last_evaluated: date | None
    stale: bool | None
    anomaly_count: int
    anomalies: list[AlexaAnomaly]  # at most 5, as AWS ordered them


def project_anomalies(r: dict) -> AlexaAnomalies:
    report = r.get("report") or {}
    rows = report.get("anomalies") or []
    return AlexaAnomalies(
        **_cost_base(r), status=_token(report.get("status")), window_start=report.get("window_start"),
        window_end=report.get("window_end"), monitor_count=report.get("monitor_count"),
        last_evaluated=report.get("last_evaluated"), stale=report.get("stale"), anomaly_count=len(rows),
        anomalies=[AlexaAnomaly(service=a["service"], region=a.get("region"), start_date=a.get("start_date"),
                                end_date=a.get("end_date"), expected_spend=a.get("expected_spend"),
                                actual_spend=a.get("actual_spend"), impact=a.get("impact"),
                                impact_percent=a.get("impact_percent")) for a in rows[:MAX_ITEMS]])


# ---------- governance ----------

class _GovAnswer(_Answer):
    regions_scanned: list[str]


def _gov_base(r: dict) -> dict:
    fresh = r["freshness"]
    return _base(r, fresh["scanned_at"], reused=fresh["scan_reused"]) | dict(regions_scanned=r["regions_scanned"])


class AlexaInventory(_GovAnswer):
    total_resources: int
    by_service: dict[str, int]
    by_region: dict[str, int]
    coverage_complete: bool
    discovery_error_count: int
    inaccessible_regions: list[str]


def project_inventory(r: dict) -> AlexaInventory:
    return AlexaInventory(**_gov_base(r), total_resources=r["total_resources"], by_service=r["by_service"],
                          by_region=r["by_region"], coverage_complete=r["coverage_complete"],
                          discovery_error_count=len(r["discovery_errors"]),
                          inaccessible_regions=r["inaccessible_regions"])


class AlexaBreakdownCounts(_A):
    total: int
    compliant: int
    non_compliant: int
    unknown: int
    compliance_percentage: float | None


class AlexaKeyCount(_A):
    key: str  # policy tag key
    count: int


class AlexaInvalidCount(_A):
    key: str  # policy tag key; the invalid values themselves are never sent
    reason: str
    count: int


class AlexaCompliance(_GovAnswer):
    total_discovered: int
    compliant: int
    non_compliant: int
    unknown: int
    compliance_percentage: float | None
    by_service: dict[str, AlexaBreakdownCounts]
    by_region: dict[str, AlexaBreakdownCounts]
    missing_mandatory_tags: list[AlexaKeyCount]
    invalid_values: list[AlexaInvalidCount]
    total_missing_tags: int
    total_invalid_values: int
    coverage_complete: bool
    discovery_error_count: int
    evaluation_error_count: int


def project_compliance(r: dict) -> AlexaCompliance:
    invalid: dict[tuple[str, str], int] = {}
    for row in r["invalid_values"]:
        invalid[(row["key"], row["reason"])] = invalid.get((row["key"], row["reason"]), 0) + row["count"]
    return AlexaCompliance(
        **_gov_base(r), total_discovered=r["total_discovered"], compliant=r["compliant"],
        non_compliant=r["non_compliant"], unknown=r["unknown"], compliance_percentage=r["compliance_percentage"],
        by_service={k: AlexaBreakdownCounts(**v) for k, v in r["by_service"].items()},
        by_region={k: AlexaBreakdownCounts(**v) for k, v in r["by_region"].items()},
        missing_mandatory_tags=[AlexaKeyCount(key=m["key"], count=m["count"]) for m in r["missing_mandatory_tags"]],
        invalid_values=[AlexaInvalidCount(key=k, reason=reason, count=n)
                        for (k, reason), n in sorted(invalid.items(), key=lambda kv: (-kv[1], kv[0]))],
        total_missing_tags=r["total_missing_tags"], total_invalid_values=r["total_invalid_values"],
        coverage_complete=r["coverage_complete"], discovery_error_count=len(r["discovery_errors"]),
        evaluation_error_count=len(r["evaluation_errors"]))


class AlexaResourceSummary(_A):
    finding_id: str  # opaque hash: use it with explain_finding or recommend_tags
    service: str
    region: str
    resource_type: str
    missing_tags: list[str]  # policy keys
    invalid_tags: list[str]  # policy keys


class AlexaUntagged(_GovAnswer):
    total: int
    offset: int
    returned: int
    next_cursor: str | None
    unknown: int
    coverage_complete: bool
    resources: list[AlexaResourceSummary]


def project_untagged(r: dict) -> AlexaUntagged:
    return AlexaUntagged(
        **_gov_base(r), total=r["total"], offset=r["offset"], returned=r["returned"], next_cursor=r["next_cursor"],
        unknown=r["unknown"], coverage_complete=r["coverage_complete"],
        resources=[AlexaResourceSummary(finding_id=x["finding_id"], service=x["service"], region=x["region"],
                                        resource_type=x["resource_type"], missing_tags=x["missing_tags"],
                                        invalid_tags=x["invalid_tags"]) for x in r["resources"]])


class AlexaInvalidTag(_A):
    key: str  # policy key; the value is never sent
    reason: str
    mandatory: bool


class AlexaFindingError(_A):
    kind: str
    code: str


class _FindingRef(_GovAnswer):
    finding_id: str
    finding_number: int
    service: str
    region: str
    resource_type: str
    compliance_status: str


class AlexaExplanation(_FindingRef):
    missing_tags: list[str]
    invalid_tags: list[AlexaInvalidTag]
    notice_count: int
    evidence_source_api: str
    observed_at: datetime
    inspection_error: AlexaFindingError | None
    explanation: str  # CloudGuard template: policy keys, service, region and finding number only


def project_explanation(r: dict) -> AlexaExplanation:
    f = r["finding"]
    err = f.get("inspection_error")
    return AlexaExplanation(
        **_gov_base(r), finding_id=f["finding_id"], finding_number=r["finding_number"], service=f["service"],
        region=f["region"], resource_type=f["resource_type"], compliance_status=f["compliance_status"],
        missing_tags=f["missing_tags"],
        invalid_tags=[AlexaInvalidTag(key=i["key"], reason=i["reason"], mandatory=i["mandatory"])
                      for i in f["invalid_tags"]],
        notice_count=len(f["notices"]), evidence_source_api=f["evidence"]["source_api"],
        observed_at=f["evidence"]["retrieved_at"],
        inspection_error=AlexaFindingError(kind=err["kind"], code=_token(err["code"]) or "Unknown") if err else None,
        explanation=r["explanation"])


class AlexaRecommendation(_A):
    key: str  # policy key
    required: bool
    status: str
    reason_code: str
    confidence: str
    # Only a value from the policy's own allow-list (e.g. Production) is sent; a free-form value (Owner, Project,
    # CostCenter ...) is tag or operator data and is withheld: value_withheld is then true.
    recommended_value: str | None
    value_withheld: bool
    evidence_types: list[str]  # accepted evidence kinds only (alias_tag, operator_exact, operator_default)
    human_input_reason: str | None  # CloudGuard template text


class AlexaRecommendations(_FindingRef):
    tags_readable: bool
    verified_tags: list[str]
    recommendations: list[AlexaRecommendation]
    status_counts: dict[str, int]
    operator_evidence_status: Literal["not_configured", "loaded", "rejected"]
    changes_made: Literal[False] = False
    authorizes_change: Literal[False] = False
    next_step: str


def project_recommendations(r: dict) -> AlexaRecommendations:
    recs = []
    for x in r["recommendations"]:
        allowed = x["policy_validation"]["allowed_values"]
        value = x["recommended_value"]
        safe = value if value is not None and value in allowed else None
        recs.append(AlexaRecommendation(
            key=x["key"], required=x["required"], status=x["status"], reason_code=x["reason_code"],
            confidence=x["confidence"], recommended_value=safe, value_withheld=value is not None and safe is None,
            evidence_types=sorted({e["source_type"] for e in x["evidence"] if e["accepted"]}),
            human_input_reason=x["human_input_reason"]))
    return AlexaRecommendations(
        **_gov_base(r), finding_id=r["finding_id"], finding_number=r["finding_number"], service=r["service"],
        region=r["region"], resource_type=r["resource_type"], compliance_status=r["compliance_status"],
        tags_readable=r["tags_readable"], verified_tags=r["verified_tags"], recommendations=recs,
        status_counts=r["status_counts"], operator_evidence_status=r["operator_evidence"]["status"],
        changes_made=r["changes_made"], authorizes_change=r["authorizes_change"], next_step=r["next_step"])


# ---------- identity ----------

class AlexaIdentityError(_A):
    kind: str
    code: str


class AlexaAccountIdentity(_Answer):
    synthetic: bool  # fixture mode: invented demo identity
    account_id: str | None  # the one account ID Alexa receives (see the module docstring)
    account_id_last4: str | None
    identity_type: Literal["iam_user", "assumed_role", "root", "federated_user", "other"] | None
    identity_label: str | None  # generic type label ("IAM role"); never the IAM name
    error: AlexaIdentityError | None


_IDENTITY_LABEL = {"iam_user": "IAM user", "assumed_role": "IAM role", "root": "Root user",
                   "federated_user": "Federated user", "other": "Other identity"}
_ACCOUNT_ID = re.compile(r"^\d{12}$")


def project_identity(r: dict) -> AlexaAccountIdentity:
    account = r["account_id"]
    if account is not None and not _ACCOUNT_ID.match(account):
        raise ValueError("account_id")
    kind = r["identity_type"]
    err = r["error"]
    return AlexaAccountIdentity(
        tool=r["tool"], outcome=r["outcome"], mode=r["mode"], speech=r["speech"],
        freshness=AlexaFreshness(generated_at=r["generated_at"], observed_at=r["retrieved_at"]),
        synthetic=r["synthetic"], account_id=account, account_id_last4=account[-4:] if account else None,
        identity_type=kind, identity_label=_IDENTITY_LABEL[kind] if kind else None,
        error=AlexaIdentityError(kind=err["kind"], code=_token(err["code"]) or "Unknown") if err else None)


# ---------- registry ----------

PROJECTIONS: dict[str, tuple[type[_Answer], Any]] = {
    "get_monthly_aws_bill": (AlexaBill, project_bill),
    "get_current_month_spending": (AlexaBill, project_bill),
    "get_cost_breakdown": (AlexaBreakdown, project_breakdown),
    "compare_monthly_costs": (AlexaComparison, project_comparison),
    "investigate_cost_increase": (AlexaComparison, project_comparison),
    "get_cost_anomalies": (AlexaAnomalies, project_anomalies),
    "get_resource_inventory": (AlexaInventory, project_inventory),
    "scan_tag_compliance": (AlexaCompliance, project_compliance),
    "list_untagged_resources": (AlexaUntagged, project_untagged),
    "explain_finding": (AlexaExplanation, project_explanation),
    "recommend_tags": (AlexaRecommendations, project_recommendations),
    "get_aws_account_identity": (AlexaAccountIdentity, project_identity),
}


class ProjectionError(RuntimeError):
    """No projection for a tool, or the operator result didn't have the expected shape. Fails closed."""


def project(tool: str, structured: dict) -> dict:
    """The Alexa answer for one operator result, as JSON-ready data. The operator result is only read."""
    entry = PROJECTIONS.get(tool)
    if entry is None:
        raise ProjectionError(tool)
    try:
        return entry[1](structured).model_dump(mode="json")
    except (KeyError, TypeError, ValueError) as exc:
        raise ProjectionError(tool) from exc


def output_schema(tool: str) -> dict | None:
    entry = PROJECTIONS.get(tool)
    return entry[0].model_json_schema() if entry else None


def as_text(projected: dict) -> str:
    return json.dumps(projected, indent=2)
