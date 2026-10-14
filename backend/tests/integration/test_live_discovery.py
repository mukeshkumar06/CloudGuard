"""Live discovery against an explicitly configured AWS test account. Read-only.

Skipped unless CLOUDGUARD_IT_REGION is set. Optionally set CLOUDGUARD_IT_PROFILE.
    CLOUDGUARD_IT_REGION=ap-south-1 uv run pytest -m integration
"""

import os

import boto3
import pytest

from cloudguard.governance.compliance import run_scan
from cloudguard.governance.discovery import SUPPORTED_RESOURCE_TYPES, LiveSource
from cloudguard.governance.models import UNRESOLVED_REGION, ComplianceStatus
from cloudguard.governance.policy_store import LocalPolicyStore, PolicyManager

REGION = os.environ.get("CLOUDGUARD_IT_REGION")

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(not REGION, reason="set CLOUDGUARD_IT_REGION to run live AWS integration tests"),
]


@pytest.fixture(scope="module")
def discovery():
    session = boto3.Session(profile_name=os.environ.get("CLOUDGUARD_IT_PROFILE"))
    return LiveSource(session, [REGION]).discover()


def test_discovery_uses_authenticated_account(discovery):
    session = boto3.Session(profile_name=os.environ.get("CLOUDGUARD_IT_PROFILE"))
    expected = session.client("sts", region_name=REGION).get_caller_identity()["Account"]
    assert discovery.account_id == expected
    assert all(r.account_id == expected for r in discovery.resources)


def test_discovered_resources_are_well_formed(discovery):
    for r in discovery.resources:
        assert r.resource_type in SUPPORTED_RESOURCE_TYPES
        assert r.resource_arn.startswith("arn:")
        assert r.region in (REGION, UNRESOLVED_REGION)
        assert (r.tags is None) != (r.tag_error is None)


def test_scan_accounts_for_every_discovered_resource(discovery):
    class Replay:
        mode = "live"

        def discover(self):
            return discovery

    scan = run_scan(Replay(), PolicyManager(LocalPolicyStore()).ensure_default())
    s = scan.summary
    assert s.total_resources == len(discovery.resources)
    assert s.compliant_resources + s.non_compliant_resources + s.inspection_failed_resources == s.total_resources
    failed = [f for f in scan.findings if f.compliance_status == ComplianceStatus.INSPECTION_FAILED]
    assert all(f.inspection_error is not None and f.missing_tags == [] for f in failed)
