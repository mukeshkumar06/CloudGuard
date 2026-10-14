from pathlib import Path

from cloudguard.governance.__main__ import main
from cloudguard.governance.compliance import run_scan
from cloudguard.governance.discovery import FixtureSource
from cloudguard.governance.models import ComplianceStatus, GovernanceScan
from cloudguard.governance.policy import DEFAULT_POLICY

FIXTURE = Path(__file__).resolve().parents[1] / "fixtures" / "governance-demo.json"


def scan():
    return run_scan(FixtureSource(FIXTURE), DEFAULT_POLICY)


def by_name(s, name):
    return next(f for f in s.findings if f.resource_name == name)


def test_fixture_scan_summary():
    s = scan()
    assert s.mode == "fixture" and s.account_id == "123456789012" and s.regions == ["ap-south-1"]
    assert all(f.account_id == "123456789012" for f in s.findings)
    assert s.summary.total_resources == 6
    assert s.summary.compliant_resources == 1
    assert s.summary.non_compliant_resources == 4
    assert s.summary.inspection_failed_resources == 1
    assert s.summary.compliance_percentage == 20.0
    assert s.scan_id.startswith("scan-")


def test_fixture_scenarios():
    s = scan()
    assert by_name(s, "checkout-api-handler").compliance_status == ComplianceStatus.COMPLIANT
    assert by_name(s, "legacy-report-generator").missing_tags == ["Environment", "Project", "Owner", "CostCenter"]
    assert by_name(s, "orders").missing_tags == ["Owner", "CostCenter"]
    assert by_name(s, "acme-marketing-uploads").missing_tags == DEFAULT_POLICY.keys
    worker = by_name(s, "batch-worker-1")  # lowercase 'environment' counts; blank Owner is invalid
    assert worker.missing_tags == [] and [i.key for i in worker.invalid_tags] == ["Owner"]
    denied = by_name(s, "acme-finance-archive")
    assert denied.compliance_status == ComplianceStatus.INSPECTION_FAILED
    assert denied.inspection_error.code == "AccessDenied"


def test_cli_fixture_mode_prints_valid_scan_json(capsys):
    assert main(["scan", "--mode", "fixture", "--fixture", str(FIXTURE)]) == 0
    parsed = GovernanceScan.model_validate_json(capsys.readouterr().out)
    assert parsed.summary.total_resources == 6
