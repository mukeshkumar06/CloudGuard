"""Regression tests for the Phase 2 review corrections."""

import json
import re
from pathlib import Path

import boto3
import pytest
from moto import mock_aws
from pydantic import ValidationError

from cloudguard.governance.__main__ import main
from cloudguard.governance.compliance import evaluate, run_scan
from cloudguard.governance.discovery import FixtureSource
from cloudguard.governance.models import (
    ComplianceStatus,
    DiscoveredResource,
    ErrorKind,
    Evidence,
    InspectionError,
    InvalidTagReason,
    NoticeKind,
    PolicyRef,
    TagFinding,
    TagNotice,
)
from cloudguard.governance.policy import (
    DEFAULT_POLICY_DOCUMENT,
    PolicyDocument,
    PolicyIntegrityError,
    TagRule,
    parse_policy,
)
from cloudguard.governance.policy_store import DynamoPolicyStore, LocalPolicyStore, PolicyManager

ROOT = Path(__file__).resolve().parents[2]
FIXTURE = ROOT / "backend" / "fixtures" / "governance-demo.json"
CORE_TEMPLATE = (ROOT / "infra" / "cloudguard-core.yaml").read_text(encoding="utf-8")
NOW = "2026-10-14T04:30:00Z"


# =================================================================== 1. table rename / new table

def test_template_defines_single_table_with_agreed_keys():
    assert "TableName: cloudguard-governance" in CORE_TEMPLATE
    assert "cloudguard-scans" not in CORE_TEMPLATE
    for attr in ("PK", "SK"):
        assert re.search(rf"AttributeName: {attr}\b", CORE_TEMPLATE)
    assert not re.search(r"AttributeName: (pk|sk)\b", CORE_TEMPLATE)
    assert "PointInTimeRecoveryEnabled: true" in CORE_TEMPLATE


def test_persistence_doc_states_new_table_without_migration():
    doc = (ROOT / "docs" / "governance-persistence.md").read_text(encoding="utf-8").lower()
    assert "new-table deployment" in doc and "no data migration" in doc
    assert "cloudguard-scans" in doc  # names the old, never-deployed table explicitly


# =================================================================== 2. IAM for TransactWriteItems

def test_no_nonexistent_transactwriteitems_iam_action_anywhere():
    files = [CORE_TEMPLATE, *(p.read_text(encoding="utf-8") for p in (ROOT / "infra" / "iam").glob("*.json"))]
    assert not any("dynamodb:TransactWriteItems" in f for f in files)


def test_policy_store_example_policy_grants_transaction_put_permissions():
    policy = json.loads((ROOT / "infra" / "iam" / "governance-policy-store-policy.json").read_text(encoding="utf-8"))
    actions = set(policy["Statement"][0]["Action"])
    # Put inside TransactWriteItems is authorized as PutItem; reads use GetItem/Query.
    assert {"dynamodb:PutItem", "dynamodb:GetItem", "dynamodb:Query"} <= actions
    assert "dynamodb:ConditionCheckItem" not in actions  # only for standalone ConditionCheck entries
    for action in ("dynamodb:GetItem", "dynamodb:PutItem", "dynamodb:Query"):
        assert action in CORE_TEMPLATE


def test_permissions_doc_cites_transaction_authorization_model():
    doc = (ROOT / "docs" / "aws-permissions.md").read_text(encoding="utf-8")
    assert "transaction-apis-iam" in doc
    assert "no `dynamodb:TransactWriteItems` IAM action" in doc


# =================================================================== 3. historical reproducibility

@pytest.fixture(params=["local", "dynamodb"])
def store(request, tmp_path):
    if request.param == "local":
        yield LocalPolicyStore(tmp_path)
        return
    with mock_aws():
        client = boto3.client("dynamodb", region_name="ap-south-1", aws_access_key_id="t", aws_secret_access_key="t")
        client.create_table(
            TableName="t", BillingMode="PAY_PER_REQUEST",
            AttributeDefinitions=[{"AttributeName": "PK", "AttributeType": "S"}, {"AttributeName": "SK", "AttributeType": "S"}],
            KeySchema=[{"AttributeName": "PK", "KeyType": "HASH"}, {"AttributeName": "SK", "KeyType": "RANGE"}],
        )
        yield DynamoPolicyStore(client, "t")


def with_override(rtype, rules):
    data = DEFAULT_POLICY_DOCUMENT.model_dump()
    data["overrides"] = {rtype: {"rules": rules}}
    return parse_policy(data)


def results(scan):
    """Compliance-relevant content of a scan, independent of scan id and timestamps."""
    return sorted(
        (f.resource_arn, f.compliance_status, tuple(f.required_tags), tuple(f.missing_tags),
         tuple((i.key, i.reason, i.mandatory) for i in f.invalid_tags),
         tuple((n.kind, n.key) for n in f.notices))
        for f in scan.findings
    )


def test_historical_scan_reproduces_after_restore_and_later_edits(store):
    m = PolicyManager(store)
    original = run_scan(FixtureSource(FIXTURE), m.ensure_default())                              # v1
    m.save(with_override("AWS::DynamoDB::Table", [{"key": "DataClassification"}]),
           expected_version=1, created_by="alice")                                            # v2
    v3 = m.restore(1, expected_version=2, created_by="bob")                                   # v3 == v1 doc
    m.save(with_override("AWS::S3::Bucket", [{"key": "Retention"}]),
           expected_version=3, created_by="carol")                                            # v4

    assert [v.version for v in m.history()] == [1, 2, 3, 4]
    assert v3.restored_from == 1 and v3.content_hash == original.policy.content_hash

    exact = m.resolve(original.policy)
    assert exact.version == 1 and exact.document == DEFAULT_POLICY_DOCUMENT
    assert results(run_scan(FixtureSource(FIXTURE), exact)) == results(original)

    # A scan under the restored version records v3, not v1, even though the documents match.
    restored_scan = run_scan(FixtureSource(FIXTURE), m.get_version("default", 3))
    assert restored_scan.policy.version == 3
    assert results(restored_scan) == results(original)
    # And the current policy really does differ.
    assert results(run_scan(FixtureSource(FIXTURE), m.get_active())) != results(original)


def test_resolve_rejects_hash_mismatch(store):
    m = PolicyManager(store)
    v1 = m.ensure_default()
    forged = PolicyRef(policy_id="default", version=1, content_hash="sha256:" + "0" * 64)
    with pytest.raises(PolicyIntegrityError):
        m.resolve(forged)
    assert m.resolve(v1.ref) == v1


def test_tampered_local_version_fails_verification(tmp_path):
    PolicyManager(LocalPolicyStore(tmp_path)).ensure_default()
    path = next(tmp_path.glob("policy-*.json"))
    data = json.loads(path.read_text(encoding="utf-8"))
    data["versions"]["1"]["document"]["defaults"][1]["key"] = "Projekt"
    path.write_text(json.dumps(data), encoding="utf-8")
    with pytest.raises(PolicyIntegrityError):
        PolicyManager(LocalPolicyStore(tmp_path)).get_version("default", 1)


def test_tampered_dynamodb_version_fails_verification():
    with mock_aws():
        client = boto3.client("dynamodb", region_name="ap-south-1", aws_access_key_id="t", aws_secret_access_key="t")
        client.create_table(
            TableName="t", BillingMode="PAY_PER_REQUEST",
            AttributeDefinitions=[{"AttributeName": "PK", "AttributeType": "S"}, {"AttributeName": "SK", "AttributeType": "S"}],
            KeySchema=[{"AttributeName": "PK", "KeyType": "HASH"}, {"AttributeName": "SK", "KeyType": "RANGE"}],
        )
        m = PolicyManager(DynamoPolicyStore(client, "t"))
        m.ensure_default()
        tampered = PolicyDocument(defaults=[TagRule(key="Owner")]).model_dump_json()
        client.update_item(
            TableName="t", Key={"PK": {"S": "POLICY#default"}, "SK": {"S": "VERSION#0000000001"}},
            UpdateExpression="SET #d = :d", ExpressionAttributeNames={"#d": "document"},
            ExpressionAttributeValues={":d": {"S": tampered}},
        )
        with pytest.raises(PolicyIntegrityError):
            m.get_version("default", 1)


def test_hash_ignores_explicit_defaults_but_not_real_changes():
    implicit = parse_policy({"defaults": [{"key": "Owner"}]})
    explicit = parse_policy({"defaults": [{"key": "Owner", "required": True, "description": "", "allowed_values": [], "aliases": {}}]})
    assert implicit.content_hash() == explicit.content_hash()
    optional = parse_policy({"defaults": [{"key": "Owner"}, {"key": "Team", "required": False}]})
    assert optional.content_hash() != parse_policy({"defaults": [{"key": "Owner"}, {"key": "Team"}]}).content_hash()


def test_cli_rejects_scan_against_tampered_history(tmp_path, capsys):
    store = ["--policy-store", str(tmp_path)]
    assert main(["policy", "init", *store]) == 0
    path = next(tmp_path.glob("policy-*.json"))
    data = json.loads(path.read_text(encoding="utf-8"))
    data["versions"]["1"]["content_hash"] = "sha256:" + "f" * 64
    path.write_text(json.dumps(data), encoding="utf-8")
    capsys.readouterr()
    assert main(["scan", "--mode", "fixture", "--fixture", str(FIXTURE), "--policy-version", "1", *store]) == 1
    assert "failed verification" in capsys.readouterr().err


# =================================================================== 4. optional tag semantics

OPTIONAL_RULES = [
    *DEFAULT_POLICY_DOCUMENT.defaults,
    TagRule(key="DataClass", required=False, allowed_values=["Public", "Internal"], aliases={"pub": "Public"}),
]
FULL = {"Environment": "Production", "Project": "p", "Owner": "o", "CostCenter": "c"}


def res(tags):
    return DiscoveredResource(
        account_id="123456789012", resource_type="AWS::Lambda::Function", service="lambda", resource_id="fn",
        resource_name="fn", resource_arn="arn:aws:lambda:ap-south-1:123456789012:function:fn",
        region="ap-south-1", discovered_at=NOW, tags=tags, tag_source="lambda:ListTags",
    )


def test_absent_optional_tag_is_not_missing_and_not_mandatory():
    f = evaluate(res(FULL), OPTIONAL_RULES, NOW)
    assert f.compliance_status == ComplianceStatus.COMPLIANT
    assert "DataClass" not in f.missing_tags and "DataClass" not in f.required_tags
    assert f.missing_tags == [] and f.invalid_tags == []


def test_absent_optional_tag_with_missing_mandatory_reports_only_mandatory():
    f = evaluate(res({"Project": "p"}), OPTIONAL_RULES, NOW)
    assert f.missing_tags == ["Environment", "Owner", "CostCenter"]


@pytest.mark.parametrize(("value", "reason"), [("", InvalidTagReason.EMPTY_VALUE), ("  ", InvalidTagReason.EMPTY_VALUE),
                                               ("Secret", InvalidTagReason.VALUE_NOT_ALLOWED)])
def test_present_optional_tag_is_still_validated(value, reason):
    f = evaluate(res({**FULL, "DataClass": value}), OPTIONAL_RULES, NOW)
    assert f.compliance_status == ComplianceStatus.NON_COMPLIANT
    assert f.missing_tags == []
    (inv,) = f.invalid_tags
    assert (inv.key, inv.reason, inv.mandatory) == ("DataClass", reason, False)
    assert "Invalid optional: DataClass" in f.evidence.summary
    assert "4 of 4 mandatory tags are valid" in f.evidence.summary


def test_present_optional_alias_is_valid_with_notice():
    f = evaluate(res({**FULL, "dataclass": "pub"}), OPTIONAL_RULES, NOW)
    assert f.compliance_status == ComplianceStatus.COMPLIANT
    assert {n.kind for n in f.notices} == {NoticeKind.NON_CANONICAL_VALUE, NoticeKind.KEY_CASE_MISMATCH}


def test_override_can_make_a_default_tag_optional_for_one_type():
    doc = with_override("AWS::S3::Bucket", [{"key": "CostCenter", "required": False}])
    s3_rules = doc.rules_for("AWS::S3::Bucket")
    bucket = DiscoveredResource(
        account_id="123456789012", resource_type="AWS::S3::Bucket", service="s3", resource_id="b", resource_name="b",
        resource_arn="arn:aws:s3:::b", region="ap-south-1", discovered_at=NOW,
        tags={"Environment": "Production", "Project": "p", "Owner": "o"}, tag_source="s3:GetBucketTagging",
    )
    f = evaluate(bucket, s3_rules, NOW)
    assert f.compliance_status == ComplianceStatus.COMPLIANT and "CostCenter" not in f.required_tags
    assert "CostCenter" in [r.key for r in doc.rules_for("AWS::Lambda::Function") if r.required]


# =================================================================== 5. categories stay separate

def _finding(**over):
    base = dict(
        finding_id="tagcomp-x", account_id="123456789012", resource_type="AWS::Lambda::Function", service="lambda",
        resource_id="fn", resource_name="fn", resource_arn="arn:x", region="ap-south-1", required_tags=["Owner"],
        compliance_status=ComplianceStatus.COMPLIANT, existing_tags={"Owner": "o"}, missing_tags=[], invalid_tags=[],
        evidence=Evidence(source_api="lambda:ListTags", retrieved_at=NOW, summary="s"), scanned_at=NOW,
    )
    return TagFinding(**{**base, **over})


ERR = InspectionError(kind=ErrorKind.ACCESS_DENIED, code="AccessDenied", message="m", operation="lambda:ListTags")
NOTICE = TagNotice(kind=NoticeKind.KEY_CASE_MISMATCH, key="Owner", actual_keys=["owner"])


@pytest.mark.parametrize("over", [
    {"missing_tags": ["Owner"]},                                                          # compliant with a violation
    {"compliance_status": ComplianceStatus.NON_COMPLIANT},                                 # non-compliant without one
    {"compliance_status": ComplianceStatus.NON_COMPLIANT, "notices": [NOTICE]},            # a notice is not a violation
    {"inspection_error": ERR},                                                             # error on an evaluated finding
    {"compliance_status": ComplianceStatus.INSPECTION_FAILED, "existing_tags": None},      # failure without error
    {"compliance_status": ComplianceStatus.INSPECTION_FAILED, "existing_tags": None,
     "inspection_error": ERR, "notices": [NOTICE]},                                        # failure with a notice
    {"compliance_status": ComplianceStatus.INSPECTION_FAILED, "existing_tags": None,
     "inspection_error": ERR, "missing_tags": ["Owner"]},                                  # failure with a violation
])
def test_finding_rejects_mixed_categories(over):
    with pytest.raises(ValidationError):
        _finding(**over)


def test_compliant_finding_may_carry_notices():
    assert _finding(notices=[NOTICE]).compliance_status == ComplianceStatus.COMPLIANT


def test_scan_summary_counts_categories_separately():
    scan = run_scan(FixtureSource(FIXTURE), PolicyManager(LocalPolicyStore()).ensure_default())
    s = scan.summary
    assert s.total_violations == sum(len(f.missing_tags) + len(f.invalid_tags) for f in scan.findings) == 12
    assert s.total_notices == sum(len(f.notices) for f in scan.findings) == 5
    # checkout + orders ("production" case), nightly-etl (alias), batch-worker (key case + value case)
    assert s.resources_with_notices == 4
    assert s.inspection_failed_resources == 1
    # Notices on compliant resources don't reduce compliance.
    assert s.compliance_percentage == 28.6


def test_cli_json_keeps_categories_in_separate_fields(tmp_path, capsys):
    assert main(["scan", "--mode", "fixture", "--fixture", str(FIXTURE), "--policy-store", str(tmp_path)]) == 0
    out = json.loads(capsys.readouterr().out)
    denied = next(f for f in out["findings"] if f["compliance_status"] == "INSPECTION_FAILED")
    assert denied["inspection_error"]["code"] == "AccessDenied"
    assert denied["missing_tags"] == [] and denied["invalid_tags"] == [] and denied["notices"] == []
    etl = next(f for f in out["findings"] if f["resource_name"] == "nightly-etl")
    assert etl["compliance_status"] == "COMPLIANT" and etl["notices"] and not etl["invalid_tags"]


def test_cli_summary_output_has_separate_sections(tmp_path, capsys):
    assert main(["scan", "--mode", "fixture", "--fixture", str(FIXTURE), "--policy-store", str(tmp_path),
                 "--output", "summary"]) == 0
    text = capsys.readouterr().out
    v, n, i, e = (text.index(h) for h in ("VIOLATIONS (12)", "NOTICES (5)", "INSPECTION FAILURES (1)", "SERVICE ERRORS (0)"))
    assert v < n < i < e
    violations, notices, failures = text[v:n], text[n:i], text[i:e]
    assert "'legacy-report-generator'" in violations and "nightly-etl" not in violations
    assert "'nightly-etl'" in notices and "canonical value is 'Production'" in notices
    assert "'acme-finance-archive'" in failures and "AccessDenied" in failures
    assert "acme-finance-archive" not in violations + notices
    # Untrusted tag text is quoted, never presented as instructions.
    assert "SYSTEM NOTE" not in text
