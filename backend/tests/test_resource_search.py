"""search_resources (name search) and preview_tag_changes (read-only tagging preview), over a real MCP session on
fixture data, plus the Alexa projection and scopes. No AWS."""

import json

import anyio
import pytest
from mcp.shared.memory import create_connected_server_and_client_session
from starlette.testclient import TestClient

from cloudguard.deploy.alexa_projection import project_search
from cloudguard.deploy.http_app import ALEXA_TOOLS, TOOL_SCOPES
from cloudguard.server.app import create_server
from cloudguard.server.context import GOVERNANCE_FIXTURE, CostContext, GovernanceContext

DEMO = json.loads(GOVERNANCE_FIXTURE.read_text(encoding="utf-8"))


def run(tmp_path, *steps, fixture=None):
    ctx = GovernanceContext.fixture(fixture, policy_dir=tmp_path / "policies")

    async def main():
        async with create_connected_server_and_client_session(create_server(CostContext.fixture(), ctx)) as c:
            return [await c.call_tool(name, args) for name, args in steps]
    return anyio.run(main)


def ok(r):
    assert not r.isError, r.content
    return r.structuredContent


def names(r):
    return [x["name"] for x in ok(r)["resources"]]


# ---------- matching ----------

@pytest.mark.parametrize("args,expected", [
    ({"text": "report", "match": "contains"}, ["legacy-report-generator"]),
    ({"text": "ACME", "match": "starts_with"}, ["acme-finance-archive", "acme-marketing-uploads"]),
    ({"text": "-1", "match": "ends_with"}, ["batch-worker-1"]),
    ({"text": "ORDERS", "match": "exact"}, ["orders"]),
    ({"text": "order", "match": "exact"}, []),
    ({"text": "a", "match": "starts_with", "services": ["dynamodb"]}, ["audit-events"]),
    ({"text": "e", "services": ["lambda"]}, ["checkout-api-handler", "legacy-report-generator", "nightly-etl"]),
    ({"text": "acme", "region": "ap-south-1", "services": ["s3"]}, ["acme-finance-archive", "acme-marketing-uploads"]),
])
def test_name_matching_is_case_insensitive_and_filtered(tmp_path, args, expected):
    (r,) = run(tmp_path, ("search_resources", args))
    assert names(r) == expected


def test_results_carry_type_region_and_identifiers_and_speech_never_echoes(tmp_path):
    (r,) = run(tmp_path, ("search_resources", {"text": "nightly", "services": ["lambda"]}))
    out = ok(r)
    (hit,) = out["resources"]
    assert hit["resource_type"] == "AWS::Lambda::Function" and hit["region"] == "ap-south-1"
    assert hit["resource_arn"].endswith(":function:nightly-etl") and hit["finding_id"].startswith("tagcomp-")
    assert out["speech"] == "1 Lambda function has a name that contains that text, in 1 region."
    assert "nightly" not in out["speech"]


def test_a_region_that_is_not_enabled_is_refused(tmp_path):
    (r,) = run(tmp_path, ("search_resources", {"text": "a", "region": "eu-west-1"}))
    assert r.isError and "region not enabled" in r.content[0].text


def test_duplicate_names_are_listed_separately(tmp_path):
    data = json.loads(json.dumps(DEMO))
    twin = dict(next(x for x in data["resources"] if x["resource_name"] == "nightly-etl"))
    twin.update(region="us-east-1", resource_arn=twin["resource_arn"].replace("ap-south-1", "us-east-1"))
    data["resources"].append(twin)
    data["regions"] = ["ap-south-1", "us-east-1"]
    path = tmp_path / "dupes.json"
    path.write_text(json.dumps(data), encoding="utf-8")
    (r,) = run(tmp_path, ("search_resources", {"text": "nightly-etl", "match": "exact"}), fixture=path)
    out = ok(r)
    assert [(x["name"], x["region"]) for x in out["resources"]] == [("nightly-etl", "ap-south-1"),
                                                                    ("nightly-etl", "us-east-1")]
    assert out["duplicate_names"] == ["nightly-etl"] and "shared by more than one" in out["speech"]
    assert len({x["finding_id"] for x in out["resources"]}) == 2


def test_pagination_with_a_cursor(tmp_path):
    first, = run(tmp_path, ("search_resources", {"text": "a", "limit": 2}))
    out = ok(first)
    assert out["total"] == 6 and out["returned"] == 2 and out["next_cursor"]

    async def main():
        ctx = GovernanceContext.fixture(policy_dir=tmp_path / "p2")
        async with create_connected_server_and_client_session(create_server(CostContext.fixture(), ctx)) as c:
            pages, cursor = [], None
            while True:
                args = {"text": "a", "limit": 2} | ({"cursor": cursor} if cursor else {})
                r = (await c.call_tool("search_resources", args)).structuredContent
                pages.append([x["name"] for x in r["resources"]])
                cursor = r["next_cursor"]
                if not cursor:
                    return pages, (await c.call_tool("search_resources", {"text": "a", "cursor": "scan-20200101T000000Z-abcdef:2"}))
    pages, stale = anyio.run(main)
    flat = [n for p in pages for n in p]
    assert len(pages) == 3 and len(flat) == len(set(flat)) == 6
    assert stale.isError and "cursor has expired" in stale.content[0].text


def test_search_is_never_a_tag_filter(tmp_path):
    # "Project" is a tag key on several resources, never part of a name: no match.
    (r,) = run(tmp_path, ("search_resources", {"text": "Project"}))
    assert names(r) == []


def test_alexa_projection_sends_only_safe_names_and_no_arns(tmp_path):
    (r,) = run(tmp_path, ("search_resources", {"text": "acme"}))
    projected = project_search(ok(r)).model_dump(mode="json")
    assert [x["display_name"] for x in projected["resources"]] == ["acme-finance-archive", "acme-marketing-uploads"]
    assert "arn:" not in json.dumps(projected) and "resource_id" not in json.dumps(projected)


# ---------- preview_tag_changes ----------

def test_preview_builds_the_proposal_without_storing_or_writing_anything(tmp_path):
    (r,) = run(tmp_path, ("preview_tag_changes", {"tags": {"Team": "platform", "Environment": "Production"},
                                                  "service": "lambda", "name_match": "contains", "name_text": "e"}))
    out = ok(r)
    assert out["changes_made"] is False and out["stored"] is False
    assert out["status"] == "PROPOSAL_READY" and out["proposal"]["account_id"] == "123456789012"
    records = {x["resource_name"]: x for x in out["proposal"]["records"]}
    assert set(records) == {"checkout-api-handler", "legacy-report-generator", "nightly-etl"}
    # Existing tags are shown; an existing different value is a conflict, never overwritten by an add.
    nightly = next(x for x in DEMO["resources"] if x["resource_name"] == "nightly-etl")
    assert records["nightly-etl"]["current_tags"] == nightly["tags"]  # the real current tags, as scanned
    assert out["request"]["target"]["name_filter"] == {"match": "contains", "text": "e"}
    assert "proposal approve" in out["next_step"] and not (tmp_path / "policies" / "proposals").exists()


def test_preview_selection_by_finding_ids(tmp_path):
    search, = run(tmp_path, ("search_resources", {"text": "acme", "services": ["s3"]}))
    ids = [x["finding_id"] for x in ok(search)["resources"]]
    (r,) = run(tmp_path, ("preview_tag_changes", {"tags": {"Team": "web"}, "finding_ids": ids}))
    out = ok(r)
    assert {x["resource_name"] for x in out["proposal"]["records"]} == {"acme-finance-archive", "acme-marketing-uploads"}


@pytest.mark.parametrize("args,error", [
    ({"tags": {"aws:createdBy": "x"}, "service": "lambda"}, None),  # a REJECTED preview, not an error
    ({"tags": {"Team": "x"}}, "say which resources"),
    ({"tags": {"Team": "x"}, "name_text": "a"}, "name_match and name_text together"),
    ({"tags": {"A": "1", "B": "2"}, "service": "lambda", "overwrite": True}, "exactly one tag"),
    ({"tags": {"Team": "x"}, "finding_ids": ["tagcomp-0000000000000000"]}, "not in the latest scan"),
])
def test_preview_validates_its_request(tmp_path, args, error):
    (r,) = run(tmp_path, ("preview_tag_changes", args))
    if error is None:
        assert ok(r)["status"] == "REJECTED" and ok(r)["proposal"] is None
    else:
        assert r.isError and error in r.content[0].text


def test_no_mcp_tool_writes_and_alexa_gets_search_and_the_read_only_preview_but_no_proposal_tool(tmp_path):
    async def main():
        ctx = GovernanceContext.fixture(policy_dir=tmp_path / "p")
        async with create_connected_server_and_client_session(create_server(CostContext.fixture(), ctx)) as c:
            return (await c.list_tools()).tools
    tools = anyio.run(main)
    assert all(t.annotations.readOnlyHint and not t.annotations.destructiveHint for t in tools)
    # The preview is a read-only dry run (projected without ARNs or current values); saving, approving and executing
    # are not MCP tools at all, so no token can reach them.
    assert {"search_resources", "preview_tag_changes"} <= ALEXA_TOOLS
    assert TOOL_SCOPES["preview_tag_changes"] == "governance:read"
    assert not {"approve", "execute", "create_proposal", "apply_tags"} & {t.name for t in tools}
