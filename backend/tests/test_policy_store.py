"""Policy versioning against both stores. DynamoDB runs under moto (no AWS, real condition semantics)."""

from pathlib import Path

import boto3
import pytest
from moto import mock_aws

from cloudguard.governance.compliance import run_scan
from cloudguard.governance.discovery import FixtureSource
from cloudguard.governance.models import ComplianceStatus
from cloudguard.governance.policy import DEFAULT_POLICY_DOCUMENT, PolicyDocument, TagRule, parse_policy
from cloudguard.governance.policy_store import (
    DynamoPolicyStore,
    LocalPolicyStore,
    PolicyConflictError,
    PolicyManager,
    PolicyNotFoundError,
)
FIXTURE = Path(__file__).resolve().parents[1] / "fixtures" / "governance-demo.json"
TABLE = "cloudguard-test"


@pytest.fixture(params=["local", "dynamodb"])
def manager(request):
    if request.param == "local":
        yield PolicyManager(LocalPolicyStore())
        return
    with mock_aws():
        client = boto3.client("dynamodb", region_name="ap-south-1", aws_access_key_id="t", aws_secret_access_key="t")
        client.create_table(
            TableName=TABLE,
            BillingMode="PAY_PER_REQUEST",
            AttributeDefinitions=[{"AttributeName": "PK", "AttributeType": "S"}, {"AttributeName": "SK", "AttributeType": "S"}],
            KeySchema=[{"AttributeName": "PK", "KeyType": "HASH"}, {"AttributeName": "SK", "KeyType": "RANGE"}],
        )
        yield PolicyManager(DynamoPolicyStore(client, TABLE))


def stricter():
    """Default policy plus a DynamoDB-only DataClassification requirement."""
    data = DEFAULT_POLICY_DOCUMENT.model_dump()
    data["overrides"] = {"AWS::DynamoDB::Table": {"rules": [{"key": "DataClassification"}]}}
    return parse_policy(data)


def test_ensure_default_creates_version_one_once(manager):
    v1 = manager.ensure_default()
    assert (v1.version, v1.created_by) == (1, "cloudguard-bootstrap")
    assert v1.content_hash == DEFAULT_POLICY_DOCUMENT.content_hash()
    assert manager.ensure_default().version == 1
    assert len(manager.history()) == 1


def test_update_creates_new_version_and_keeps_history(manager):
    manager.ensure_default()
    v2 = manager.save(stricter(), expected_version=1, created_by="alice", note="classify tables")
    assert v2.version == 2 and manager.get_active().version == 2
    assert [v.version for v in manager.history()] == [1, 2]
    old = manager.get_version("default", 1)
    assert old.document == DEFAULT_POLICY_DOCUMENT  # historical versions are unchanged
    assert "DataClassification" not in [r.key for r in old.document.rules_for("AWS::DynamoDB::Table")]


def test_stale_expected_version_is_rejected(manager):
    manager.ensure_default()
    manager.save(stricter(), expected_version=1, created_by="alice")
    other = PolicyDocument(defaults=[*DEFAULT_POLICY_DOCUMENT.defaults, TagRule(key="Team")])
    with pytest.raises(PolicyConflictError):
        manager.save(other, expected_version=1, created_by="bob")  # bob didn't see version 2
    assert manager.get_active().version == 2


def test_creating_existing_policy_without_expected_version_conflicts(manager):
    manager.ensure_default()
    with pytest.raises(PolicyConflictError):
        manager.save(stricter(), expected_version=None, created_by="alice")


def test_unchanged_policy_is_not_saved_again(manager):
    manager.ensure_default()
    with pytest.raises(ValueError, match="unchanged"):
        manager.save(DEFAULT_POLICY_DOCUMENT, expected_version=1, created_by="alice")


def test_invalid_document_is_rejected_before_saving(manager):
    manager.ensure_default()
    bypassed = PolicyDocument.model_construct(defaults=[], overrides={})  # skips validators
    with pytest.raises(Exception, match="at least one tag rule"):
        manager.save(bypassed, expected_version=1, created_by="alice")
    assert [v.version for v in manager.history()] == [1]


def test_author_is_required(manager):
    with pytest.raises(ValueError, match="created_by"):
        manager.save(DEFAULT_POLICY_DOCUMENT, expected_version=None, created_by="  ")


def test_restore_reactivates_old_document_as_new_version(manager):
    manager.ensure_default()
    manager.save(stricter(), expected_version=1, created_by="alice")
    v3 = manager.restore(1, expected_version=2, created_by="bob")
    assert v3.version == 3 and v3.content_hash == DEFAULT_POLICY_DOCUMENT.content_hash()
    assert v3.note == "restored from version 1"


def test_missing_policy_and_version_raise(manager):
    with pytest.raises(PolicyNotFoundError):
        manager.get_active("nope")
    manager.ensure_default()
    with pytest.raises(PolicyNotFoundError):
        manager.get_version("default", 9)


def test_separate_policy_ids_are_independent(manager):
    manager.ensure_default()
    manager.ensure_default(policy_id="acct-123456789012")
    manager.save(stricter(), expected_version=1, created_by="alice", policy_id="acct-123456789012")
    assert manager.get_active().version == 1
    assert manager.get_active("acct-123456789012").version == 2


def test_scan_records_exact_policy_version_and_historical_versions_reproduce(manager):
    v1 = manager.ensure_default()
    v2 = manager.save(stricter(), expected_version=1, created_by="alice")
    orders_v1 = next(f for f in run_scan(FixtureSource(FIXTURE), v1).findings if f.resource_name == "orders")
    scan_v2 = run_scan(FixtureSource(FIXTURE), manager.get_active())
    orders_v2 = next(f for f in scan_v2.findings if f.resource_name == "orders")

    assert scan_v2.policy.model_dump() == {"policy_id": "default", "version": 2, "content_hash": v2.content_hash}
    assert "DataClassification" in orders_v2.missing_tags
    assert "DataClassification" not in orders_v1.missing_tags
    # Re-running against the historical version gives the original result.
    again = run_scan(FixtureSource(FIXTURE), manager.get_version("default", 1))
    assert again.policy.version == 1
    assert next(f for f in again.findings if f.resource_name == "orders").missing_tags == orders_v1.missing_tags


def test_local_store_persists_to_disk(tmp_path):
    PolicyManager(LocalPolicyStore(tmp_path)).ensure_default()
    PolicyManager(LocalPolicyStore(tmp_path)).save(stricter(), expected_version=1, created_by="alice")
    reloaded = PolicyManager(LocalPolicyStore(tmp_path))
    assert [v.version for v in reloaded.history()] == [1, 2]
    assert reloaded.get_active().document == stricter()


def test_policy_change_never_touches_resource_tags():
    """Evaluating under a new policy reports differently but leaves existing tags exactly as found."""
    manager = PolicyManager(LocalPolicyStore())
    scan = run_scan(FixtureSource(FIXTURE), manager.ensure_default())
    raw = FixtureSource(FIXTURE).discover()
    by_arn = {r.resource_arn: r.tags for r in raw.resources}
    for f in scan.findings:
        if f.compliance_status != ComplianceStatus.INSPECTION_FAILED:
            assert f.existing_tags == by_arn[f.resource_arn]
