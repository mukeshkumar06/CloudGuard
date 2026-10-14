from datetime import UTC, datetime

import pytest
from pydantic import ValidationError

from cloudguard.governance.compliance import evaluate, finding_id_for, summarize
from cloudguard.governance.models import (
    ComplianceStatus,
    DiscoveredResource,
    ErrorKind,
    InspectionError,
    InvalidTagReason,
    NoticeKind,
)
from cloudguard.governance.policy import DEFAULT_POLICY_DOCUMENT, TagRule

NOW = datetime(2026, 10, 14, 4, 30, tzinfo=UTC)
RULES = DEFAULT_POLICY_DOCUMENT.rules_for("AWS::Lambda::Function")
FULL = {"Environment": "Development", "Project": "cloudguard", "Owner": "eng", "CostCenter": "CC-1"}


def resource(tags=None, tag_error=None, arn="arn:aws:lambda:ap-south-1:123456789012:function:fn"):
    return DiscoveredResource(
        account_id="123456789012",
        resource_type="AWS::Lambda::Function",
        service="lambda",
        resource_id="fn",
        resource_name="fn",
        resource_arn=arn,
        region="ap-south-1",
        discovered_at=NOW,
        tags=tags,
        tag_source="lambda:ListTags",
        tag_error=tag_error,
    )


def kinds(finding):
    return [n.kind for n in finding.notices]


def test_fully_tagged_is_compliant_without_notices():
    f = evaluate(resource(FULL), RULES, NOW)
    assert f.compliance_status == ComplianceStatus.COMPLIANT
    assert f.missing_tags == [] and f.invalid_tags == [] and f.notices == []
    assert f.required_tags == ["Environment", "Project", "Owner", "CostCenter"]
    assert f.evidence.source_api == "lambda:ListTags"


def test_untagged_reports_all_missing_in_policy_order():
    f = evaluate(resource({}), RULES, NOW)
    assert f.compliance_status == ComplianceStatus.NON_COMPLIANT
    assert f.missing_tags == ["Environment", "Project", "Owner", "CostCenter"]
    assert "0 of 4" in f.evidence.summary


def test_partial_tags():
    f = evaluate(resource({"Environment": "Production", "Project": "x"}), RULES, NOW)
    assert f.missing_tags == ["Owner", "CostCenter"]


def test_keys_match_case_insensitively_with_key_case_notice():
    tags = {"environment": "Development", "PROJECT": "cg", "Owner": "eng", "CostCenter": "CC-1"}
    f = evaluate(resource(tags), RULES, NOW)
    assert f.compliance_status == ComplianceStatus.COMPLIANT
    mism = [n for n in f.notices if n.kind == NoticeKind.KEY_CASE_MISMATCH]
    assert [(n.key, n.actual_keys) for n in mism] == [("Environment", ["environment"]), ("Project", ["PROJECT"])]


@pytest.mark.parametrize("value", ["", "   ", "\t"])
def test_empty_or_whitespace_value_is_invalid_not_missing(value):
    f = evaluate(resource({**FULL, "Owner": value}), RULES, NOW)
    assert f.compliance_status == ComplianceStatus.NON_COMPLIANT
    assert f.missing_tags == []
    assert [(i.key, i.reason) for i in f.invalid_tags] == [("Owner", InvalidTagReason.EMPTY_VALUE)]


def test_duplicate_case_keys_pass_if_one_is_valid_and_are_flagged():
    f = evaluate(resource({**FULL, "owner": ""}), RULES, NOW)
    assert f.compliance_status == ComplianceStatus.COMPLIANT
    dup = next(n for n in f.notices if n.kind == NoticeKind.DUPLICATE_KEY_CASE)
    assert dup.key == "Owner" and sorted(dup.actual_keys) == ["Owner", "owner"]


@pytest.mark.parametrize(("value", "canonical"), [("prod", "Production"), ("DEV", "Development"), ("stage", "Staging")])
def test_alias_is_valid_but_recommends_canonical_value(value, canonical):
    f = evaluate(resource({**FULL, "Environment": value}), RULES, NOW)
    assert f.compliance_status == ComplianceStatus.COMPLIANT
    n = next(n for n in f.notices if n.kind == NoticeKind.NON_CANONICAL_VALUE)
    assert (n.value, n.canonical_value) == (value, canonical)
    assert f.existing_tags["Environment"] == value  # never rewritten


def test_case_variant_of_allowed_value_is_valid_with_notice():
    f = evaluate(resource({**FULL, "Environment": "production"}), RULES, NOW)
    assert f.compliance_status == ComplianceStatus.COMPLIANT
    assert kinds(f) == [NoticeKind.NON_CANONICAL_VALUE]


@pytest.mark.parametrize("value", ["qa", "production-eu", "prd"])
def test_value_outside_allow_list_and_aliases_is_invalid(value):
    f = evaluate(resource({**FULL, "Environment": value}), RULES, NOW)
    assert f.compliance_status == ComplianceStatus.NON_COMPLIANT
    assert [(i.key, i.reason, i.value) for i in f.invalid_tags] == [("Environment", InvalidTagReason.VALUE_NOT_ALLOWED, value)]


def test_optional_rule_absent_is_fine_but_invalid_value_is_not():
    rules = [*RULES, TagRule(key="DataClass", required=False, allowed_values=["Public", "Internal"])]
    assert evaluate(resource(FULL), rules, NOW).compliance_status == ComplianceStatus.COMPLIANT
    bad = evaluate(resource({**FULL, "DataClass": "Secret"}), rules, NOW)
    assert bad.invalid_tags[0].key == "DataClass"
    assert "DataClass" not in bad.required_tags


def test_inspection_failure_is_never_classified_as_untagged():
    err = InspectionError(kind=ErrorKind.ACCESS_DENIED, code="AccessDenied", message="no", operation="lambda:ListTags")
    f = evaluate(resource(tag_error=err), RULES, NOW)
    assert f.compliance_status == ComplianceStatus.INSPECTION_FAILED
    assert f.missing_tags == [] and f.existing_tags is None
    assert f.inspection_error == err


def test_resource_must_have_tags_or_error():
    with pytest.raises(ValidationError):
        resource()


def test_finding_id_is_stable_per_arn():
    a = evaluate(resource({}), RULES, NOW)
    b = evaluate(resource(FULL), RULES, datetime.now(UTC))
    assert a.finding_id == b.finding_id == finding_id_for(a.resource_arn)
    other = evaluate(resource({}, arn="arn:aws:lambda:ap-south-1:123456789012:function:other"), RULES, NOW)
    assert other.finding_id != a.finding_id


def test_compliance_percentage_excludes_inspection_failures():
    err = InspectionError(kind=ErrorKind.THROTTLED, code="Throttling", message="", operation="lambda:ListTags")
    findings = [
        evaluate(resource(FULL, arn="arn:1"), RULES, NOW),
        evaluate(resource(FULL, arn="arn:2"), RULES, NOW),
        evaluate(resource({}, arn="arn:3"), RULES, NOW),
        evaluate(resource(tag_error=err, arn="arn:4"), RULES, NOW),
    ]
    s = summarize(findings, [])
    assert (s.total_resources, s.compliant_resources, s.non_compliant_resources, s.inspection_failed_resources) == (4, 2, 1, 1)
    assert s.compliance_percentage == 66.7


def test_compliance_percentage_none_when_nothing_evaluable():
    assert summarize([], []).compliance_percentage is None
