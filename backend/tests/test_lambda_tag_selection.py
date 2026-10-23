"""Lambda tagging selection through preview_tag_changes: all Lambdas, one Lambda by exact name, and name patterns,
with duplicates across regions, exact-before-partial matching, empty matches and the preview summary. Fixture data
(custom-tagging-demo.json: 'worker' exists in ap-south-1 and us-east-1, 'report-worker' contains 'worker',
'billing-api' already has Owner=payments-team). A real MCP session; no AWS, nothing stored."""

from pathlib import Path

import anyio
import pytest
from mcp.shared.memory import create_connected_server_and_client_session

from cloudguard.governance.custom_tagging import CustomTaggingEngine
from cloudguard.governance.custom_tagging.models import TaggingOperation, TaggingRequest, TargetCriteria
from cloudguard.governance.discovery import FixtureSource
from cloudguard.governance.policy_store import LocalPolicyStore, PolicyManager
from cloudguard.server.app import create_server
from cloudguard.server.context import CostContext, GovernanceContext

FIXTURE = Path(__file__).resolve().parents[1] / "fixtures" / "custom-tagging-demo.json"
ACCT = "123456789012"
LAMBDAS = {"ServiceWRK", "billing-api", "report-worker", "image-resizer", "shared-name", "worker"}
TAGS = {"Environment": "Production", "Team": "backend"}


def preview(tmp_path, **args):
    ctx = GovernanceContext.fixture(FIXTURE, policy_dir=tmp_path / "policies")

    async def main():
        async with create_connected_server_and_client_session(create_server(CostContext.fixture(), ctx)) as c:
            return await c.call_tool("preview_tag_changes", {"tags": TAGS} | args)
    r = anyio.run(main)
    assert not r.isError, r.content
    return r.structuredContent


# ---------- 1. all Lambdas ----------

def test_all_lambdas_in_every_region(tmp_path):
    out = preview(tmp_path, service="lambda")
    s = out["summary"]
    assert out["status"] == "PROPOSAL_READY" and out["proposal"]["account_id"] == ACCT
    assert s["matched"] == 7 and set(s["resource_names"]) == LAMBDAS  # 'worker' twice, one per region
    assert s["regions"] == ["ap-south-1", "us-east-1"] and s["proposed_tags"] == TAGS
    assert {r["resource_type"] for r in out["proposal"]["records"]} == {"AWS::Lambda::Function"}
    assert out["stored"] is False and out["changes_made"] is False


def test_all_lambdas_in_one_region(tmp_path):
    out = preview(tmp_path, service="lambda", region="us-east-1")
    assert out["summary"]["matched"] == 1 and out["summary"]["resource_names"] == ["worker"]


def test_existing_tags_are_shown_and_existing_values_never_silently_overwritten(tmp_path):
    out = preview(tmp_path, service="lambda", tags={"Environment": "Production"})
    records = {r["resource_name"]: r for r in out["proposal"]["records"]}
    assert records["ServiceWRK"]["current_tags"] == {"Project": "CloudGuard", "Environment": "Development"}
    assert records["ServiceWRK"]["outcome"] == "CONFLICT" and not records["ServiceWRK"]["changes"]
    assert out["summary"]["overwrites"] == [] and out["summary"]["conflicts"] == 1


# ---------- 2. one Lambda by exact name ----------

def test_one_lambda_by_exact_name(tmp_path):
    out = preview(tmp_path, service="lambda", resource_name="image-resizer")
    assert out["status"] == "PROPOSAL_READY" and out["summary"]["resource_names"] == ["image-resizer"]
    assert out["request"]["operation"] == "ADD_TAG" and out["request"]["target"]["name"] == "image-resizer"


def test_exact_match_is_preferred_over_partial(tmp_path):
    # 'worker' is exactly two functions; 'report-worker' merely contains it and is never included.
    out = preview(tmp_path, service="lambda", resource_name="worker")
    assert out["status"] == "NEEDS_CLARIFICATION" and out["proposal"] is None
    assert sorted((c["resource_name"], c["region"]) for c in out["candidates"]) == [
        ("worker", "ap-south-1"), ("worker", "us-east-1")]


def test_duplicate_names_across_regions_resolve_when_the_region_is_given(tmp_path):
    out = preview(tmp_path, service="lambda", resource_name="worker", region="us-east-1")
    assert out["status"] == "PROPOSAL_READY"
    (record,) = out["proposal"]["records"]
    assert record["region"] == "us-east-1" and record["resource_arn"].endswith(":function:worker")


def test_a_case_mismatch_is_asked_about_not_guessed(tmp_path):
    out = preview(tmp_path, service="lambda", resource_name="Image-Resizer")
    assert out["status"] == "NEEDS_CLARIFICATION" and out["proposal"] is None
    assert [c["resource_name"] for c in out["candidates"]] == ["image-resizer"]


def test_no_exact_name_offers_similar_names_without_selecting_them(tmp_path):
    out = preview(tmp_path, service="lambda", resource_name="billing")
    assert out["status"] == "NOT_FOUND" and out["proposal"] is None and out["summary"] is None
    assert [c["resource_name"] for c in out["candidates"]] == ["billing-api"]


def test_a_name_is_matched_within_the_service_only(tmp_path):
    # 'shared-name' is both a Lambda and a DynamoDB table; asking for the Lambda selects only the function.
    out = preview(tmp_path, service="lambda", resource_name="shared-name")
    (record,) = out["proposal"]["records"]
    assert record["resource_type"] == "AWS::Lambda::Function"


def test_an_overwrite_is_explicit_and_listed(tmp_path):
    out = preview(tmp_path, service="lambda", resource_name="billing-api", tags={"Owner": "backend"}, overwrite=True)
    assert out["status"] == "PROPOSAL_READY" and out["proposal"]["requires_overwrite_confirmation"] is True
    assert out["summary"]["overwrites"] == [{"resource_name": "billing-api", "region": "ap-south-1", "key": "Owner",
                                             "current_value": "payments-team", "new_value": "backend"}]
    add = preview(tmp_path, service="lambda", resource_name="billing-api", tags={"Owner": "backend"})
    assert add["status"] == "NO_CHANGES" and add["summary"]["changing"] == 0  # an add never overwrites
    assert add["summary"]["conflicts"] == 1 and add["summary"]["overwrites"] == []


# ---------- 3. name patterns ----------

@pytest.mark.parametrize("match,text,expected", [
    ("contains", "work", ["report-worker", "worker", "worker"]),
    ("contains", "WRK", ["ServiceWRK"]),
    ("starts_with", "image", ["image-resizer"]),
    ("ends_with", "API", ["billing-api"]),
    ("exact", "SERVICEWRK", ["ServiceWRK"]),
])
def test_name_patterns_are_case_insensitive(tmp_path, match, text, expected):
    out = preview(tmp_path, service="lambda", name_match=match, name_text=text)
    assert sorted(out["summary"]["resource_names"]) == sorted(expected)


def test_an_empty_match_proposes_nothing(tmp_path):
    out = preview(tmp_path, service="lambda", name_match="contains", name_text="ecommerce")
    assert out["status"] == "NOT_FOUND" and out["proposal"] is None and out["summary"] is None


def test_selection_arguments_are_exclusive(tmp_path):
    ctx = GovernanceContext.fixture(FIXTURE, policy_dir=tmp_path / "policies")

    async def main():
        async with create_connected_server_and_client_session(create_server(CostContext.fixture(), ctx)) as c:
            return await c.call_tool("preview_tag_changes", {"tags": TAGS, "resource_name": "worker",
                                                             "name_match": "contains", "name_text": "w"})
    r = anyio.run(main)
    assert r.isError and "not both" in r.content[0].text


# ---------- identities are resolved server-side ----------

def test_a_saved_request_naming_an_undiscovered_arn_proposes_nothing():
    pm = PolicyManager(LocalPolicyStore())
    pm.ensure_default()
    forged = f"arn:aws:lambda:ap-south-1:{ACCT}:function:not-really-there"
    request = TaggingRequest(operation=TaggingOperation.BULK_ADD_TAG, tags={"Team": "x"}, raw_text="t", parsed_by="t",
                             target=TargetCriteria(all_of_type=True, arns=(forged,)))
    outcome = CustomTaggingEngine(FixtureSource(FIXTURE), pm.get_active()).prepare_request(request)
    assert outcome.status.value == "NOT_FOUND" and outcome.proposal is None


def test_another_accounts_target_is_out_of_scope():
    pm = PolicyManager(LocalPolicyStore())
    pm.ensure_default()
    request = TaggingRequest(operation=TaggingOperation.BULK_ADD_TAG, tags={"Team": "x"}, raw_text="t", parsed_by="t",
                             target=TargetCriteria(resource_type="AWS::Lambda::Function", all_of_type=True,
                                                   account_id="210987654321"))
    outcome = CustomTaggingEngine(FixtureSource(FIXTURE), pm.get_active()).prepare_request(request)
    assert outcome.status.value == "REJECTED" and outcome.proposal is None
