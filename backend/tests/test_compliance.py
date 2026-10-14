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
)
from cloudguard.governance.policy import DEFAULT_POLICY, RequiredTag, TagPolicy

NOW = datetime(2026, 10, 14, 4, 30, tzinfo=UTC)
FULL = {"Environment": "dev", "Project": "cloudguard", "Owner": "eng", "CostCenter": "CC-1"}


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


def test_fully_tagged_is_compliant():
    f = evaluate(resource(FULL), DEFAULT_POLICY, NOW)
    assert f.compliance_status == ComplianceStatus.COMPLIANT
    assert f.missing_tags == [] and f.invalid_tags == []
    assert f.evidence.source_api == "lambda:ListTags"


def test_untagged_reports_all_missing_in_policy_order():
    f = evaluate(resource({}), DEFAULT_POLICY, NOW)
    assert f.compliance_status == ComplianceStatus.NON_COMPLIANT
    assert f.missing_tags == ["Environment", "Project", "Owner", "CostCenter"]
    assert "0 of 4" in f.evidence.summary


def test_partial_tags():
    f = evaluate(resource({"Environment": "prod", "Project": "x"}), DEFAULT_POLICY, NOW)
    assert f.missing_tags == ["Owner", "CostCenter"]


def test_keys_match_case_insensitively():
    tags = {"environment": "dev", "PROJECT": "cg", "owner": "eng", "costcenter": "CC-1"}
    f = evaluate(resource(tags), DEFAULT_POLICY, NOW)
    assert f.compliance_status == ComplianceStatus.COMPLIANT


@pytest.mark.parametrize("value", ["", "   ", "\t"])
def test_empty_or_whitespace_value_is_invalid_not_missing(value):
    f = evaluate(resource({**FULL, "Owner": value}), DEFAULT_POLICY, NOW)
    assert f.compliance_status == ComplianceStatus.NON_COMPLIANT
    assert f.missing_tags == []
    assert [(i.key, i.reason) for i in f.invalid_tags] == [("Owner", InvalidTagReason.EMPTY_VALUE)]


def test_case_duplicate_keys_with_one_valid_value_pass():
    f = evaluate(resource({**FULL, "owner": ""}), DEFAULT_POLICY, NOW)
    assert f.compliance_status == ComplianceStatus.COMPLIANT


def test_allowed_values_enforced_case_insensitively():
    policy = TagPolicy(required_tags=[RequiredTag(key="Environment", allowed_values=["development", "production"])])
    assert evaluate(resource({"Environment": "Production"}), policy, NOW).compliance_status == ComplianceStatus.COMPLIANT
    bad = evaluate(resource({"Environment": "qa"}), policy, NOW)
    assert bad.invalid_tags[0].reason == InvalidTagReason.VALUE_NOT_ALLOWED


def test_inspection_failure_is_never_classified_as_untagged():
    err = InspectionError(kind=ErrorKind.ACCESS_DENIED, code="AccessDenied", message="no", operation="lambda:ListTags")
    f = evaluate(resource(tag_error=err), DEFAULT_POLICY, NOW)
    assert f.compliance_status == ComplianceStatus.INSPECTION_FAILED
    assert f.missing_tags == [] and f.existing_tags is None
    assert f.inspection_error == err


def test_resource_must_have_tags_or_error():
    with pytest.raises(ValidationError):
        resource()


def test_finding_id_is_stable_per_arn():
    a = evaluate(resource({}), DEFAULT_POLICY, NOW)
    b = evaluate(resource(FULL), DEFAULT_POLICY, datetime.now(UTC))
    assert a.finding_id == b.finding_id == finding_id_for(a.resource_arn)
    other = evaluate(resource({}, arn="arn:aws:lambda:ap-south-1:123456789012:function:other"), DEFAULT_POLICY, NOW)
    assert other.finding_id != a.finding_id


def test_compliance_percentage_excludes_inspection_failures():
    err = InspectionError(kind=ErrorKind.THROTTLED, code="Throttling", message="", operation="lambda:ListTags")
    findings = [
        evaluate(resource(FULL, arn="arn:1"), DEFAULT_POLICY, NOW),
        evaluate(resource(FULL, arn="arn:2"), DEFAULT_POLICY, NOW),
        evaluate(resource({}, arn="arn:3"), DEFAULT_POLICY, NOW),
        evaluate(resource(tag_error=err, arn="arn:4"), DEFAULT_POLICY, NOW),
    ]
    s = summarize(findings, [])
    assert (s.total_resources, s.compliant_resources, s.non_compliant_resources, s.inspection_failed_resources) == (4, 2, 1, 1)
    assert s.compliance_percentage == 66.7


def test_compliance_percentage_none_when_nothing_evaluable():
    assert summarize([], []).compliance_percentage is None


def test_policy_rejects_duplicate_keys_case_insensitively():
    with pytest.raises(ValidationError):
        TagPolicy(required_tags=[RequiredTag(key="Owner"), RequiredTag(key="owner")])


def test_policy_loads_from_json(tmp_path):
    p = tmp_path / "policy.json"
    p.write_text('{"required_tags": [{"key": "Owner"}, {"key": "DataClassification"}]}', encoding="utf-8")
    assert TagPolicy.load(p).keys == ["Owner", "DataClassification"]
