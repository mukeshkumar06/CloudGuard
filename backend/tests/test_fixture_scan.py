import json
from pathlib import Path

from cloudguard.governance.__main__ import main
from cloudguard.governance.compliance import run_scan
from cloudguard.governance.discovery import FixtureSource
from cloudguard.governance.models import ComplianceStatus, GovernanceScan, InvalidTagReason, NoticeKind
from cloudguard.governance.policy import DEFAULT_POLICY_DOCUMENT
from cloudguard.governance.policy_store import LocalPolicyStore, PolicyManager

FIXTURE = Path(__file__).resolve().parents[1] / "fixtures" / "governance-demo.json"


def scan():
    return run_scan(FixtureSource(FIXTURE), PolicyManager(LocalPolicyStore()).ensure_default())


def by_name(s, name):
    return next(f for f in s.findings if f.resource_name == name)


def test_fixture_scan_summary():
    s = scan()
    assert s.mode == "fixture" and s.account_id == "123456789012" and s.regions == ["ap-south-1"]
    assert all(f.account_id == "123456789012" for f in s.findings)
    assert (s.summary.total_resources, s.summary.compliant_resources,
            s.summary.non_compliant_resources, s.summary.inspection_failed_resources) == (8, 2, 5, 1)
    assert s.summary.compliance_percentage == 28.6  # 2 / 7 inspected
    assert s.scan_id.startswith("scan-")
    assert (s.policy.policy_id, s.policy.version) == ("default", 1)
    assert s.policy.content_hash == DEFAULT_POLICY_DOCUMENT.content_hash()


def test_fixture_scenarios():
    s = scan()
    checkout = by_name(s, "checkout-api-handler")  # "production" is valid; canonical "Production" recommended
    assert checkout.compliance_status == ComplianceStatus.COMPLIANT
    assert [(n.kind, n.canonical_value) for n in checkout.notices] == [(NoticeKind.NON_CANONICAL_VALUE, "Production")]
    etl = by_name(s, "nightly-etl")  # alias "prod"
    assert etl.compliance_status == ComplianceStatus.COMPLIANT
    assert etl.notices[0].value == "prod" and etl.existing_tags["Environment"] == "prod"
    assert by_name(s, "legacy-report-generator").missing_tags == ["Environment", "Project", "Owner", "CostCenter"]
    assert by_name(s, "orders").missing_tags == ["Owner", "CostCenter"]
    assert by_name(s, "acme-marketing-uploads").missing_tags == ["Environment", "Project", "Owner", "CostCenter"]
    worker = by_name(s, "batch-worker-1")  # lowercase 'environment' counts; blank Owner is invalid
    assert worker.missing_tags == [] and [i.key for i in worker.invalid_tags] == ["Owner"]
    assert NoticeKind.KEY_CASE_MISMATCH in [n.kind for n in worker.notices]
    audit = by_name(s, "audit-events")
    assert [(i.key, i.reason) for i in audit.invalid_tags] == [("Environment", InvalidTagReason.VALUE_NOT_ALLOWED)]
    denied = by_name(s, "acme-finance-archive")
    assert denied.compliance_status == ComplianceStatus.INSPECTION_FAILED
    assert denied.inspection_error.code == "AccessDenied"


def test_cli_fixture_scan_and_policy_lifecycle(tmp_path, capsys):
    store = ["--policy-store", str(tmp_path)]
    assert main(["scan", "--mode", "fixture", "--fixture", str(FIXTURE), *store]) == 0
    first = GovernanceScan.model_validate_json(capsys.readouterr().out)
    assert first.summary.total_resources == 8 and first.policy.version == 1

    policy_file = tmp_path / "p.json"
    data = DEFAULT_POLICY_DOCUMENT.model_dump()
    data["overrides"] = {"AWS::S3::Bucket": {"remove": ["CostCenter"]}}
    policy_file.write_text(json.dumps(data), encoding="utf-8")
    assert main(["policy", "validate", str(policy_file)]) == 0
    assert main(["policy", "save", str(policy_file), "--expected-version", "1", "--author", "alice", *store]) == 0
    assert main(["policy", "save", str(policy_file), "--expected-version", "1", "--author", "bob", *store]) == 1  # stale
    capsys.readouterr()

    assert main(["scan", "--mode", "fixture", "--fixture", str(FIXTURE), *store]) == 0
    second = GovernanceScan.model_validate_json(capsys.readouterr().out)
    assert second.policy.version == 2
    assert "CostCenter" not in by_name(second, "acme-marketing-uploads").missing_tags

    assert main(["scan", "--mode", "fixture", "--fixture", str(FIXTURE), "--policy-version", "1", *store]) == 0
    assert GovernanceScan.model_validate_json(capsys.readouterr().out).policy.version == 1


def test_cli_rejects_invalid_policy_file(tmp_path, capsys):
    bad = tmp_path / "bad.json"
    bad.write_text('{"defaults": [{"key": "Env", "aliases": {"p": "Prod"}}]}', encoding="utf-8")
    assert main(["policy", "validate", str(bad)]) == 2
    err = json.loads(capsys.readouterr().err)
    assert err["valid"] is False and "aliases require allowed_values" in err["errors"][0]
