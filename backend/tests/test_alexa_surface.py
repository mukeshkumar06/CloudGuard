"""The Alexa-facing MCP surface of the deployment prototype: an `alexa:read` principal may list and call exactly
the fourteen read-only tools in ALEXA_TOOLS. Fixture data, or live mode with every botocore call mocked."""

import hashlib
import json

import pytest
from mcp.types import ToolAnnotations
from starlette.testclient import TestClient

import cloudguard.deploy.http_app as http_app
from cloudguard.deploy.http_app import (
    ALEXA_SCOPE, ALEXA_TOOLS, ConfigError, Principal, authorize_tool, build_app, check_alexa_surface,
)
from cloudguard.server.app import create_server
from cloudguard.server.context import CostContext
from cloudguard.server.cost_tools import COST_TOOL_NAMES
from cloudguard.server.governance_tools import GOVERNANCE_TOOL_NAMES
from cloudguard.server.identity_tools import IDENTITY_TOOL_NAMES
from test_lambda_prototype import DIGEST, MCP_HEADERS, TOKEN, aws_calls, call_tool, config, rpc  # noqa: F401

ALEXA_TOKEN = "alexa-test-token-not-a-secret-987654"
ALEXA_DIGEST = hashlib.sha256(ALEXA_TOKEN.encode()).hexdigest()
# Every registered tool: fourteen read-only tools, including the tagging preview (a dry run that stores nothing).
ELEVEN = set(COST_TOOL_NAMES) | set(GOVERNANCE_TOOL_NAMES) | set(IDENTITY_TOOL_NAMES)
INTERNAL = ("approve_fix", "propose_fix", "execute_remediation")
DEMO_NAMES = ("checkout-api-handler", "legacy-report-generator", "orders", "acme-marketing-uploads",
              "batch-worker-1", "acme-finance-archive", "nightly-etl", "audit-events")


def alexa_config(**env):
    return config(CLOUDGUARD_ALEXA_TOKEN_SHA256=ALEXA_DIGEST, **env)


@pytest.fixture
def probes(monkeypatch):
    """Registers write-like 'internal' tools next to the real ones and records whether any of them ever ran."""
    ran = []
    real = http_app.create_server

    def with_internal_tools(*args, **kwargs):
        server = real(*args, **kwargs)
        for name in INTERNAL:
            server.add_tool(lambda name=name: ran.append(name) or "done", name=name,
                            annotations=ToolAnnotations(readOnlyHint=False, destructiveHint=True))
        return server
    monkeypatch.setattr(http_app, "create_server", with_internal_tools)
    return ran


def listed(c, token):
    return {t["name"] for t in rpc(c, "tools/list", {}, token=token).json()["result"]["tools"]}


def refused(c, name, token, args=None):
    result = call_tool(c, name, args or {}, token=token).json()["result"]
    return result["isError"] is True and result["content"][0]["text"] == f"not authorized to call {name}"


# ---------- allowlist ----------

def test_allowlist_is_exactly_the_fourteen_read_only_tools():
    assert ALEXA_TOOLS == ELEVEN and len(ALEXA_TOOLS) == 14
    assert not any(w in t for t in ALEXA_TOOLS for w in ("propose", "approve", "revoke", "abort", "execute"))


def test_startup_refuses_a_missing_or_writable_alexa_tool():
    with pytest.raises(ConfigError, match="missing or not annotated read-only"):
        check_alexa_surface(create_server(CostContext.fixture()))  # no governance tools


# ---------- discovery ----------

def test_alexa_lists_all_fourteen_and_no_internal_tool(probes):
    with TestClient(build_app(alexa_config()), base_url="http://testserver") as c:
        assert listed(c, ALEXA_TOKEN) == ELEVEN
        assert listed(c, TOKEN) == ELEVEN | set(INTERNAL)  # operator listing is unfiltered
    assert probes == []


# ---------- invocation ----------

def test_alexa_cannot_call_internal_or_unknown_tools(probes):
    with TestClient(build_app(alexa_config()), base_url="http://testserver") as c:
        for name in (*INTERNAL, "no_such_tool", "tools/list"):
            assert refused(c, name, ALEXA_TOKEN)
        for name in INTERNAL:  # the operator can't call them either: no scope maps to them (fails closed)
            assert refused(c, name, TOKEN)
    assert probes == []


def test_json_rpc_batch_cannot_bypass_the_allowlist(probes):
    batch = [{"jsonrpc": "2.0", "id": i, "method": "tools/call", "params": {"name": n, "arguments": {}}}
             for i, n in enumerate(("approve_fix", "get_monthly_aws_bill", "execute_remediation"), 1)]
    headers = MCP_HEADERS | {"mcp-protocol-version": "2025-11-25", "authorization": f"Bearer {ALEXA_TOKEN}"}
    with TestClient(build_app(alexa_config()), base_url="http://testserver") as c:
        r = c.post("/mcp", headers=headers, content=json.dumps(batch))
    assert probes == [] and "done" not in r.text
    assert r.status_code == 400  # the MCP SDK refuses JSON-RPC batches (removed from the 2025-06-18 spec)


def test_alexa_can_call_every_allowed_tool_with_speech_safe_results():
    with TestClient(build_app(alexa_config()), base_url="http://testserver") as c:
        fid = call_tool(c, "list_untagged_resources", token=ALEXA_TOKEN).json()["result"]["structuredContent"][
            "resources"][0]["finding_id"]
        for name in sorted(ALEXA_TOOLS):
            args = ({"finding_id": fid} if name == "explain_finding" else
                    {"resource": fid} if name == "recommend_tags" else
                    {"text": "e"} if name == "search_resources" else
                    {"tags": {"Owner": "team-a"}, "service": "lambda"} if name == "preview_tag_changes" else {})
            result = call_tool(c, name, args, token=ALEXA_TOKEN).json()["result"]
            assert result["isError"] is False, (name, result["content"])
            out = result["structuredContent"]
            speech = out["speech"]
            assert speech and len(speech) < 600, name
            assert "123456789012" not in speech and "arn:" not in speech, name
            assert not any(n in speech for n in DEMO_NAMES), (name, speech)
            assert out["schema_version"] == "alexa-1"  # the Alexa projection
            # Only the identity tool answers with an account ID (its whole purpose); see test_account_identity.
            assert ("account_id" in out) == (name == "get_aws_account_identity"), name


def test_alexa_tool_errors_are_sanitized():
    with TestClient(build_app(alexa_config()), base_url="http://testserver") as c:
        foreign = "arn:aws:lambda:ap-south-1:999988887777:function:secret-fn"
        result = call_tool(c, "recommend_tags", {"resource": foreign}, token=ALEXA_TOKEN).json()["result"]
    text = result["content"][0]["text"]
    assert result["isError"] is True and "999988887777" not in text and "secret-fn" not in text


# ---------- authentication and scopes ----------

def test_invalid_or_missing_tokens_are_401():
    with TestClient(build_app(alexa_config()), base_url="http://testserver") as c:
        for token in (None, "", "wrong", ALEXA_DIGEST, ALEXA_TOKEN + "x"):
            assert call_tool(c, "get_monthly_aws_bill", token=token).status_code == 401


def test_alexa_token_is_not_accepted_when_not_configured():
    with TestClient(build_app(config()), base_url="http://testserver") as c:
        assert call_tool(c, "get_monthly_aws_bill", token=ALEXA_TOKEN).status_code == 401


def test_token_without_any_cloudguard_scope_is_403():
    with TestClient(build_app(config(CLOUDGUARD_TOKEN_SCOPES="")), base_url="http://testserver") as c:
        assert rpc(c, "tools/list", {}).status_code == 403


@pytest.mark.parametrize("env,needle", [
    ({"CLOUDGUARD_TOKEN_SCOPES": "alexa:write"}, "unknown scope"),
    ({"CLOUDGUARD_TOKEN_SCOPES": "alexa:read cost:read"}, "can't be combined"),
    ({"CLOUDGUARD_ALEXA_TOKEN_SHA256": "abc"}, "64-character hex"),
    ({"CLOUDGUARD_ALEXA_TOKEN_SHA256": DIGEST}, "must differ"),
])
def test_wrong_scope_or_alexa_token_configuration_is_refused(env, needle):
    with pytest.raises(ConfigError, match=needle):
        config(**env)


def test_authorizer_requires_exactly_the_alexa_scope():
    alexa = Principal("alexa", frozenset({ALEXA_SCOPE}), "static-token")
    assert all(authorize_tool(alexa, t) for t in ALEXA_TOOLS)
    assert not any(authorize_tool(alexa, t) for t in (*INTERNAL, "unknown"))
    for scopes in (frozenset(), frozenset({"alexa:admin"}), frozenset({"ALEXA:READ"})):
        assert not any(authorize_tool(Principal("x", scopes, "static-token"), t) for t in ALEXA_TOOLS)
    assert not authorize_tool(None, "get_monthly_aws_bill")
    assert not authorize_tool({"scopes": [ALEXA_SCOPE]}, "get_monthly_aws_bill")  # not a Principal


def test_operator_scopes_are_unchanged():
    cost = Principal("op", frozenset({"cost:read"}), "static-token")
    gov = Principal("op", frozenset({"governance:read"}), "static-token")
    assert all(authorize_tool(cost, t) for t in COST_TOOL_NAMES)
    assert not any(authorize_tool(cost, t) for t in GOVERNANCE_TOOL_NAMES)
    assert all(authorize_tool(gov, t) for t in GOVERNANCE_TOOL_NAMES)
    assert not any(authorize_tool(gov, t) for t in COST_TOOL_NAMES)


# ---------- live mode: forbidden calls never reach AWS ----------

def test_forbidden_alexa_calls_make_zero_aws_calls(aws_calls, probes, tmp_path):  # noqa: F811
    cfg = alexa_config(CLOUDGUARD_MODE="live", CLOUDGUARD_POLICY_STORE=str(tmp_path / "policies"))
    with TestClient(build_app(cfg), base_url="http://testserver") as c:
        for name in (*INTERNAL, "no_such_tool"):
            assert refused(c, name, ALEXA_TOKEN)
        assert call_tool(c, "get_resource_inventory", token="wrong").status_code == 401
        assert aws_calls == [] and probes == []
        inv = call_tool(c, "get_resource_inventory", token=ALEXA_TOKEN).json()["result"]
    assert inv["isError"] is False
    assert aws_calls and all(op.split(":")[1].startswith(("List", "Describe", "Get")) for op in aws_calls), aws_calls
