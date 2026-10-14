"""Phase 3 review: IAM compatibility design and approval-readiness documentation."""

import json
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
PROPOSED = ROOT / "infra" / "iam" / "proposed"
TEMPLATE = (ROOT / "infra" / "cloudguard-core.yaml").read_text(encoding="utf-8")


def load(name):
    return json.loads((PROPOSED / name).read_text(encoding="utf-8"))


def statements(policy):
    return {s["Sid"]: s for s in policy["Statement"]}


def actions(stmt):
    a = stmt["Action"]
    return [a] if isinstance(a, str) else a


@pytest.mark.parametrize("name", ["tagger-role-policy.json", "tagger-trust-policy.json", "tagger-session-policy.example.json"])
def test_proposed_policies_are_valid_json_policy_documents(name):
    doc = load(name)
    assert doc["Version"] == "2012-10-17" and doc["Statement"]
    assert all({"Sid", "Effect", "Action"} <= set(s) for s in doc["Statement"])


def test_tagger_role_never_allows_wildcard_actions_or_untagging():
    role = statements(load("tagger-role-policy.json"))
    allowed = [a for s in role.values() if s["Effect"] == "Allow" for a in actions(s)]
    assert not any(a.endswith("*") for a in allowed)
    assert "tag:TagResources" not in allowed  # F6
    assert not {"lambda:UntagResource", "dynamodb:UntagResource", "ec2:DeleteTags"} & set(allowed)
    assert set(actions(role["NeverRemoveTags"])) == {"lambda:UntagResource", "dynamodb:UntagResource", "ec2:DeleteTags"}
    assert role["NeverRemoveTags"]["Effect"] == "Deny"


def test_tagger_role_scopes_ec2_to_existing_instances_and_s3_to_own_account():
    role = statements(load("tagger-role-policy.json"))
    ec2 = role["TagExistingEc2InstancesOnly"]
    assert ec2["Resource"].endswith(":instance/*") and ec2["Condition"] == {"Null": {"ec2:CreateAction": "true"}}  # F5
    s3 = role["S3BucketTagsReadMergeWrite"]
    assert s3["Condition"]["StringEquals"] == {"s3:ResourceAccount": "<ACCOUNT_ID>"}
    assert "s3:GetBucketTagging" in actions(s3)  # needed for read-merge-write


def test_tagger_role_has_no_key_allow_list_but_denies_protected_keys_and_resources():
    role = statements(load("tagger-role-policy.json"))
    allow_conditions = json.dumps([s.get("Condition", {}) for s in role.values() if s["Effect"] == "Allow"])
    assert "aws:TagKeys" not in allow_conditions  # F1: custom keys allowed at the ceiling
    protected = role["NeverWriteProtectedTagKeys"]
    assert protected["Effect"] == "Deny" and "cloudguard:*" in protected["Condition"]["ForAnyValue:StringLike"]["aws:TagKeys"]
    assert "s3:PutBucketTagging" not in actions(protected)  # S3 doesn't support aws:TagKeys
    res = role["NeverTouchProtectedResources"]
    assert "s3:PutBucketTagging" in actions(res) and "aws:ResourceTag/cloudguard:protected" in res["Condition"]["StringEquals"]


def test_trust_policy_is_not_the_account_root():
    (stmt,) = load("tagger-trust-policy.json")["Statement"]
    principal = stmt["Principal"]["AWS"]
    assert not principal.endswith(":root") and ":role/" in principal  # F7
    assert stmt["Condition"]["StringLike"]["sts:RoleSessionName"] == "cg-proposal-*"


def test_session_policy_example_binds_exact_resources_keys_and_values():
    s = statements(load("tagger-session-policy.example.json"))["ExactlyTheApprovedLambdaChanges"]
    assert all("*" not in arn for arn in s["Resource"])
    assert s["Condition"]["ForAllValues:StringEquals"]["aws:TagKeys"] == ["Project"]
    assert s["Condition"]["StringEquals"] == {"aws:RequestTag/Project": "CloudGuard"}
    assert len(json.dumps(load("tagger-session-policy.example.json"), separators=(",", ":"))) <= 2048


def test_current_template_restriction_is_documented_not_removed():
    assert "Sid: GovernanceTaggingOnly" in TEMPLATE and "aws:TagKeys: [Owner, Project, Environment, CostCenter]" in TEMPLATE
    assert "KNOWN ISSUES (Phase 3 review)" in TEMPLATE and "docs/phase4-iam-design.md" in TEMPLATE


def test_design_doc_covers_findings_and_verified_condition_keys():
    doc = (ROOT / "docs" / "phase4-iam-design.md").read_text(encoding="utf-8")
    for finding in ("F1", "F2", "F3", "F4", "F5", "F6", "F7", "F8"):
        assert f"| {finding} |" in doc
    assert "s3:PutBucketTagging` supports no `aws:TagKeys`/`aws:RequestTag`" in doc
    assert "list_s3.html" in doc and "Not yet verified" in doc


def test_approval_readiness_doc_lists_binding_and_revalidation():
    doc = (ROOT / "docs" / "approval-readiness.md").read_text(encoding="utf-8")
    for required in ("approval_binding()", "expected_current_value", "single-use approval nonce",
                     "Re-read current tags", "Proposals are immutable", "Not implemented in Phase 3"):
        assert required in doc
