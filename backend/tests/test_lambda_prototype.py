"""Lambda Web Adapter prototype (cloudguard.deploy): the existing MCP server behind bearer auth, tool
authorization and a host allow-list, exercised over ASGI exactly as the adapter would forward Function URL
requests (and once over real loopback HTTP). Fixture data, or live mode with every botocore call mocked:
nothing reaches AWS.
"""

import hashlib
import json
import logging
import os
import subprocess
import sys
import threading
import time
from pathlib import Path

import pytest
from starlette.testclient import TestClient

from cloudguard.deploy.http_app import (
    KNOWN_SCOPES, LOCAL_HOSTS, BearerAuthApp, ConfigError, DeployConfig, Principal, StaticTokenAuthenticator,
    authorize_tool, build_app,
)
from cloudguard.server.context import CostContext, GovernanceContext
from cloudguard.server.cost_tools import COST_TOOL_NAMES
from cloudguard.server.governance_tools import GOVERNANCE_TOOL_NAMES

BACKEND = Path(__file__).resolve().parents[1]
TOKEN = "test-token-not-a-secret-0123456789"
DIGEST = hashlib.sha256(TOKEN.encode()).hexdigest()
VERSIONS = ["2025-03-26", "2025-06-18", "2025-11-25"]
MCP_HEADERS = {"accept": "application/json, text/event-stream", "content-type": "application/json"}


def config(**env):
    base = {"CLOUDGUARD_TOKEN_SHA256": DIGEST, "CLOUDGUARD_ALLOWED_HOSTS": "testserver"}
    return DeployConfig.from_env(base | env)


@pytest.fixture
def client():
    with TestClient(build_app(config()), base_url="http://testserver") as c:
        yield c


def rpc(client, method, params=None, *, rid=1, version="2025-11-25", token=TOKEN, host=None):
    headers = dict(MCP_HEADERS)
    if version:
        headers["mcp-protocol-version"] = version
    if token is not None:
        headers["authorization"] = f"Bearer {token}"
    if host:
        headers["host"] = host
    body = {"jsonrpc": "2.0", "id": rid, "method": method, **({"params": params} if params is not None else {})}
    return client.post("/mcp", headers=headers, json=body)


def initialize(client, version):
    return rpc(client, "initialize", {"protocolVersion": version, "capabilities": {},
                                      "clientInfo": {"name": "prototype-test", "version": "0"}}, version=None)


# ---------- MCP over the adapter-facing HTTP interface ----------

@pytest.mark.parametrize("version", VERSIONS)
def test_initialize_negotiates_each_protocol_version(client, version):
    r = initialize(client, version)
    assert r.status_code == 200 and r.headers["content-type"].startswith("application/json")
    result = r.json()["result"]
    assert result["protocolVersion"] == version and result["serverInfo"]["name"] == "cloudguard-ai"


@pytest.mark.parametrize("version", VERSIONS)
def test_tools_list_and_fixture_tool_call(client, version):
    tools = rpc(client, "tools/list", {}, version=version).json()["result"]["tools"]
    assert len(tools) == 14 and all(t["annotations"]["readOnlyHint"] for t in tools)
    r = rpc(client, "tools/call", {"name": "get_monthly_aws_bill", "arguments": {}}, rid=3, version=version)
    result = r.json()["result"]
    assert result["isError"] is False
    assert result["structuredContent"]["mode"] == "fixture" and result["structuredContent"]["currency"] == "USD"


def test_sequential_requests_are_independent_and_stateless(client):
    assert initialize(client, "2025-11-25").status_code == 200
    names = ["get_monthly_aws_bill", "get_current_month_spending", "get_cost_breakdown", "compare_monthly_costs",
             "investigate_cost_increase", "get_cost_anomalies"]
    for i, name in enumerate(names * 2, start=10):
        r = rpc(client, "tools/call", {"name": name, "arguments": {}}, rid=i)
        assert r.status_code == 200 and r.json()["id"] == i and r.json()["result"]["isError"] is False
        assert "mcp-session-id" not in r.headers  # stateless: no server session to pin to an instance


def test_tool_argument_validation_still_applies(client):
    r = rpc(client, "tools/call", {"name": "get_monthly_aws_bill", "arguments": {"account_id": "x"}})
    assert r.json()["result"]["isError"] is True


def test_unsupported_protocol_version_header_rejected(client):
    assert rpc(client, "tools/list", {}, version="1999-01-01").status_code == 400


# ---------- authentication ----------

@pytest.mark.parametrize("token,headers", [
    (None, {}),
    ("wrong-token", {}),
    ("", {}),
])
def test_missing_or_invalid_token_is_401(client, token, headers):
    r = rpc(client, "tools/list", {}, token=token)
    assert r.status_code == 401 and r.headers["www-authenticate"].startswith("Bearer")
    assert r.json() == {"error": "unauthorized"}


@pytest.mark.parametrize("value", ["Basic dXNlcjpwYXNz", f"Token {TOKEN}", "Bearer", f"bearer  {TOKEN}x"])
def test_wrong_scheme_or_malformed_header_is_401(client, value):
    headers = MCP_HEADERS | {"authorization": value}
    r = client.post("/mcp", headers=headers, json={"jsonrpc": "2.0", "id": 1, "method": "tools/list"})
    assert r.status_code == 401


def test_get_and_delete_also_require_auth(client):
    assert client.get("/mcp").status_code == 401
    assert client.delete("/mcp").status_code == 401


def test_rejected_requests_never_reach_tools(monkeypatch):
    calls = []
    import cloudguard.cost.service as service
    monkeypatch.setattr(service, "month_costs", lambda *a, **k: calls.append(a))
    with TestClient(build_app(config()), base_url="http://testserver") as c:
        rpc(c, "tools/call", {"name": "get_monthly_aws_bill", "arguments": {}}, token="wrong")
        rpc(c, "tools/call", {"name": "get_monthly_aws_bill", "arguments": {}}, token=None)
    assert calls == []


def test_token_never_logged(client, caplog):
    caplog.set_level(logging.DEBUG)
    rpc(client, "tools/list", {})
    rpc(client, "tools/list", {}, token="some-wrong-token")
    text = caplog.text
    assert TOKEN not in text and "some-wrong-token" not in text and DIGEST not in text
    lines = [json.loads(r.getMessage()) for r in caplog.records if r.name == "cloudguard.deploy"]
    assert [(l["status"], l["auth"]) for l in lines] == [(200, "ok"), (401, "invalid")]
    assert all(set(l) == {"event", "method", "path", "status", "auth", "subject", "duration_ms"} for l in lines)
    assert [l["subject"] for l in lines] == ["static-token", None]


def test_health_needs_no_token_and_reveals_nothing(client):
    r = client.get("/healthz")
    assert r.status_code == 200 and r.json() == {"status": "ok", "mode": "fixture"}


# ---------- host validation ----------

def test_unexpected_host_rejected_even_with_valid_token(client):
    r = rpc(client, "tools/list", {}, host="attacker.example")
    assert r.status_code == 421


def test_default_hosts_are_localhost_only():
    cfg = DeployConfig.from_env({"CLOUDGUARD_TOKEN_SHA256": DIGEST})
    assert cfg.allowed_hosts == LOCAL_HOSTS
    with TestClient(build_app(cfg), base_url="http://127.0.0.1:8080") as c:
        assert rpc(c, "tools/list", {}).status_code == 200
        assert rpc(c, "tools/list", {}, host="abc123.lambda-url.ap-south-1.on.aws").status_code == 421


def test_future_https_hostname_can_be_allowed_explicitly():
    host = "abc123.lambda-url.ap-south-1.on.aws"
    with TestClient(build_app(config(CLOUDGUARD_ALLOWED_HOSTS=host)), base_url=f"https://{host}") as c:
        assert rpc(c, "tools/list", {}).status_code == 200
        assert rpc(c, "tools/list", {}, host="other.lambda-url.ap-south-1.on.aws").status_code == 421


@pytest.mark.parametrize("env,needle", [
    ({}, "CLOUDGUARD_TOKEN_SHA256"),
    ({"CLOUDGUARD_TOKEN_SHA256": "abc"}, "CLOUDGUARD_TOKEN_SHA256"),
    ({"CLOUDGUARD_TOKEN_SHA256": DIGEST, "CLOUDGUARD_MODE": "prod"}, "fixture or live"),
    ({"CLOUDGUARD_TOKEN_SHA256": DIGEST, "CLOUDGUARD_AUTH_MODE": "oauth"}, "oauth is not implemented"),
    ({"CLOUDGUARD_TOKEN_SHA256": DIGEST, "CLOUDGUARD_AUTH_MODE": "none"}, "must be static-token"),
    ({"CLOUDGUARD_TOKEN_SHA256": DIGEST, "CLOUDGUARD_TOKEN_SCOPES": "cost:read admin"}, "unknown scope"),
    ({"CLOUDGUARD_TOKEN_SHA256": DIGEST, "CLOUDGUARD_TOKEN_SUBJECT": "bad subject"}, "CLOUDGUARD_TOKEN_SUBJECT"),
    ({"CLOUDGUARD_TOKEN_SHA256": DIGEST, "CLOUDGUARD_ALLOWED_HOSTS": "*"}, "invalid allowed host"),
    ({"CLOUDGUARD_TOKEN_SHA256": DIGEST, "CLOUDGUARD_ALLOWED_HOSTS": "*:*"}, "invalid allowed host"),
    ({"CLOUDGUARD_TOKEN_SHA256": DIGEST, "CLOUDGUARD_ALLOWED_HOSTS": "evil.example/path"}, "invalid allowed host"),
])
def test_unsafe_configuration_refused(env, needle):
    with pytest.raises(ConfigError, match=needle):
        DeployConfig.from_env(env)


# ---------- isolation ----------

def test_prototype_imports_no_boto3_and_stdio_entry_is_unchanged():
    script = ("import sys, hashlib, os\n"
              f"os.environ['CLOUDGUARD_TOKEN_SHA256'] = '{DIGEST}'\n"
              "from cloudguard.deploy.http_app import build_app\n"
              "build_app()\n"
              "assert 'boto3' not in sys.modules, 'boto3 imported'\n"
              "from cloudguard.server.__main__ import main\n"
              "print('OK')\n")
    proc = subprocess.run([sys.executable, "-B", "-c", script], cwd=BACKEND, capture_output=True, text=True,
                          timeout=120)
    assert proc.returncode == 0 and "OK" in proc.stdout, proc.stderr

# Tools whose required arguments have no default (everything else is called with {}).
REQUIRED_ARGS = {"search_resources": {"text": "e"}, "preview_tag_changes": {"tags": {"Team": "x"}, "service": "lambda"}}


# ---------- all fourteen tools ----------

ALL_TOOLS = set(COST_TOOL_NAMES) | set(GOVERNANCE_TOOL_NAMES) | {"get_aws_account_identity"}


def call_tool(client, name, arguments=None, **kw):
    return rpc(client, "tools/call", {"name": name, "arguments": arguments or {}}, **kw)


def test_all_fourteen_tools_registered_and_each_runs_in_fixture_mode(client):
    tools = {t["name"]: t for t in rpc(client, "tools/list", {}).json()["result"]["tools"]}
    assert set(tools) == ALL_TOOLS and len(ALL_TOOLS) == 14
    assert all(t["annotations"]["readOnlyHint"] and not t["annotations"]["destructiveHint"] for t in tools.values())
    out = {}
    needs_finding = ["explain_finding", "recommend_tags"]
    for name in sorted(ALL_TOOLS - set(needs_finding)) + needs_finding:
        finding_id = out["list_untagged_resources"]["resources"][0]["finding_id"] if name in needs_finding else None
        args = ({"finding_id": finding_id} if name == "explain_finding"
                else {"resource": finding_id} if name == "recommend_tags" else REQUIRED_ARGS.get(name, {}))
        result = call_tool(client, name, args).json()["result"]
        assert result["isError"] is False, (name, result["content"])
        out[name] = result["structuredContent"]
        assert out[name]["tool"] == name and out[name]["mode"] == "fixture"


# ---------- tool authorization ----------

class CountingSource:
    def __init__(self):
        from cloudguard.governance.discovery import FixtureSource
        from cloudguard.server.context import GOVERNANCE_FIXTURE
        self.inner, self.mode, self.calls = FixtureSource(GOVERNANCE_FIXTURE), "fixture", 0

    def discover(self):
        self.calls += 1
        return self.inner.discover()


def contexts_with(source, tmp_path):
    gov = GovernanceContext(mode="fixture", source=source, regions=("ap-south-1",), data_source="test fixture",
                            policy_dir=tmp_path / "policies")
    return CostContext.fixture(), gov


def test_scopes_limit_which_tools_a_caller_may_use(tmp_path):
    src = CountingSource()
    app = build_app(config(CLOUDGUARD_TOKEN_SCOPES="cost:read"), contexts=contexts_with(src, tmp_path))
    with TestClient(app, base_url="http://testserver") as c:
        assert call_tool(c, "get_monthly_aws_bill").json()["result"]["isError"] is False
        for name in GOVERNANCE_TOOL_NAMES:
            args = {"finding_id": "tagcomp-0000000000000000"} if name == "explain_finding" else {}
            result = call_tool(c, name, args).json()["result"]
            assert result["isError"] is True and result["content"][0]["text"] == f"not authorized to call {name}"
    assert src.calls == 0  # refused before any discovery


def test_governance_only_token_cannot_use_cost_tools(monkeypatch):
    calls = []
    import cloudguard.cost.service as service
    monkeypatch.setattr(service, "month_costs", lambda *a, **k: calls.append(a))
    with TestClient(build_app(config(CLOUDGUARD_TOKEN_SCOPES="governance:read")), base_url="http://testserver") as c:
        result = call_tool(c, "get_monthly_aws_bill").json()["result"]
        assert result["isError"] is True and "not authorized" in result["content"][0]["text"]
        assert call_tool(c, "get_resource_inventory").json()["result"]["isError"] is False
    assert calls == []


def test_authorization_is_checked_before_argument_validation(client):
    app = build_app(config(CLOUDGUARD_TOKEN_SCOPES="cost:read"))
    with TestClient(app, base_url="http://testserver") as c:
        text = call_tool(c, "scan_tag_compliance", {"account_id": "x"}).json()["result"]["content"][0]["text"]
    assert text == "not authorized to call scan_tag_compliance"


def test_unknown_tool_is_refused(client):
    result = call_tool(client, "delete_everything").json()["result"]
    assert result["isError"] is True and "not authorized" in result["content"][0]["text"]


def test_token_without_cloudguard_scopes_is_403():
    with TestClient(build_app(config(CLOUDGUARD_TOKEN_SCOPES="")), base_url="http://testserver") as c:
        r = rpc(c, "tools/list", {})
    assert r.status_code == 403 and r.json() == {"error": "forbidden"}
    assert 'error="insufficient_scope"' in r.headers["www-authenticate"]


def test_authorizer_unit():
    p = Principal("x", frozenset({"cost:read"}), "static-token")
    assert authorize_tool(p, "get_monthly_aws_bill") and not authorize_tool(p, "scan_tag_compliance")
    assert not authorize_tool(None, "get_monthly_aws_bill") and not authorize_tool(p, "nope")
    assert not authorize_tool({"scopes": ["cost:read"]}, "get_monthly_aws_bill")  # only a real Principal


# ---------- OAuth metadata, origins, safe errors ----------

def test_oauth_metadata_is_not_advertised(client):
    r = client.get("/.well-known/oauth-protected-resource")
    assert r.status_code == 404 and r.json() == {"error": "not_found"}
    unauth = rpc(client, "tools/list", {}, token=None)
    assert "resource_metadata" not in unauth.headers["www-authenticate"]


def test_foreign_browser_origin_rejected(client):
    headers = MCP_HEADERS | {"authorization": f"Bearer {TOKEN}", "origin": "https://attacker.example",
                             "mcp-protocol-version": "2025-11-25"}
    r = client.post("/mcp", headers=headers, json={"jsonrpc": "2.0", "id": 1, "method": "tools/list"})
    assert r.status_code == 403


def test_unexpected_errors_are_generic_500(caplog):
    async def broken(scope, receive, send):
        raise RuntimeError(r"boom at C:\secret\path arn:aws:iam::999988887777:user/bob")
    auth = StaticTokenAuthenticator(bytes.fromhex(DIGEST), "static-token", KNOWN_SCOPES)
    c = TestClient(BearerAuthApp(broken, auth, "fixture"), base_url="http://testserver")  # no lifespan
    r = c.post("/mcp", headers={"authorization": f"Bearer {TOKEN}"}, json={})
    assert r.status_code == 500 and r.json() == {"error": "internal_error"}
    assert "secret" not in r.text and "bob" not in r.text and "secret" not in caplog.text


def test_deploy_entry_binds_loopback_only(monkeypatch):
    import cloudguard.deploy.__main__ as entry
    seen = {}
    monkeypatch.setenv("CLOUDGUARD_TOKEN_SHA256", DIGEST)
    monkeypatch.setattr(entry.uvicorn, "run", lambda app, **kw: seen.update(kw))
    assert entry.main(["--port", "8099"]) == 0
    assert seen["host"] == "127.0.0.1" and seen["port"] == 8099


# ---------- live mode: no AWS call before authentication ----------

LIVE_RESPONSES = {
    "DescribeRegions": {"Regions": [{"RegionName": "ap-south-1", "OptInStatus": "opt-in-not-required"}]},
    "GetCallerIdentity": {"Account": "123456789012", "Arn": "arn:aws:iam::123456789012:user/test", "UserId": "x"},
    "ListFunctions": {"Functions": [{"FunctionName": "f1",
                                     "FunctionArn": "arn:aws:lambda:ap-south-1:123456789012:function:f1"}]},
    "ListTags": {"Tags": {}},
    "ListTables": {"TableNames": []},
    "ListBuckets": {"Buckets": []},
}
DENIED = "User: arn:aws:iam::999988887777:user/bob.admin is not authorized to perform: ec2:DescribeInstances"


@pytest.fixture
def aws_calls(monkeypatch):
    """Mock every botocore call: record it, answer the read-only discovery calls, deny DescribeInstances."""
    import botocore.client
    from botocore.exceptions import ClientError
    for key in ("AWS_PROFILE", "AWS_DEFAULT_PROFILE", "AWS_SESSION_TOKEN"):
        monkeypatch.delenv(key, raising=False)
    monkeypatch.setenv("AWS_ACCESS_KEY_ID", "testing")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "testing")
    monkeypatch.setenv("AWS_DEFAULT_REGION", "ap-south-1")
    monkeypatch.setenv("AWS_CONFIG_FILE", os.devnull)
    monkeypatch.setenv("AWS_SHARED_CREDENTIALS_FILE", os.devnull)
    calls = []

    def fake(self, operation, params):
        calls.append(f"{self.meta.service_model.service_name}:{operation}")
        if operation == "DescribeInstances":
            raise ClientError({"Error": {"Code": "UnauthorizedOperation", "Message": DENIED},
                               "ResponseMetadata": {"HTTPStatusCode": 403}}, operation)
        if operation not in LIVE_RESPONSES:
            raise AssertionError(f"unexpected AWS call {operation}")
        return LIVE_RESPONSES[operation]
    monkeypatch.setattr(botocore.client.BaseClient, "_make_api_call", fake)
    return calls


def test_live_mode_makes_no_aws_call_before_an_authorized_tool_call(aws_calls, tmp_path):
    cfg = config(CLOUDGUARD_MODE="live", CLOUDGUARD_POLICY_STORE=str(tmp_path / "policies"),
                 CLOUDGUARD_TOKEN_SCOPES="cost:read")
    app = build_app(cfg)
    assert aws_calls == []  # startup: no STS, no DescribeRegions, no scan
    with TestClient(app, base_url="http://testserver") as c:
        assert c.get("/healthz").json() == {"status": "ok", "mode": "live"}
        for token in (None, "wrong", ""):
            assert call_tool(c, "scan_tag_compliance", token=token).status_code == 401
        assert call_tool(c, "scan_tag_compliance", host="attacker.example").status_code == 421
        assert initialize(c, "2025-11-25").status_code == 200
        assert len(rpc(c, "tools/list", {}).json()["result"]["tools"]) == 14
        denied = call_tool(c, "get_resource_inventory").json()["result"]  # token lacks governance:read
        assert denied["isError"] is True
    assert aws_calls == []


def test_live_mode_authorized_call_reaches_the_tools_read_only(aws_calls, tmp_path):
    cfg = config(CLOUDGUARD_MODE="live", CLOUDGUARD_POLICY_STORE=str(tmp_path / "policies"))
    with TestClient(build_app(cfg), base_url="http://testserver") as c:
        assert aws_calls == []
        inv = call_tool(c, "get_resource_inventory").json()["result"]
        scan = call_tool(c, "scan_tag_compliance").json()["result"]
    assert inv["isError"] is False and scan["isError"] is False
    r = inv["structuredContent"]
    assert r["mode"] == "live" and r["regions_scanned"] == ["ap-south-1"] and r["total_resources"] == 1
    assert r["coverage_complete"] is False and r["discovery_errors"][0]["error"] == {
        "kind": "ACCESS_DENIED", "code": "UnauthorizedOperation", "operation": "ec2:DescribeInstances"}
    assert scan["structuredContent"]["scan_id"] == r["scan_id"]  # reused, one discovery
    # Services are discovered concurrently, so only the first two calls have a fixed order.
    assert aws_calls[:2] == ["ec2:DescribeRegions", "sts:GetCallerIdentity"]
    assert sorted(aws_calls[2:]) == sorted(["lambda:ListFunctions", "lambda:ListTags", "dynamodb:ListTables",
                                            "s3:ListBuckets", "ec2:DescribeInstances"])
    assert aws_calls.index("lambda:ListFunctions") < aws_calls.index("lambda:ListTags")
    text = json.dumps([inv, scan])
    assert "bob.admin" not in text and "999988887777" not in text and "is not authorized" not in text
    assert not (tmp_path / "policies").exists()


# ---------- long-running scans ----------

class BlockingSource(CountingSource):
    def __init__(self):
        super().__init__()
        self.entered, self.release = threading.Event(), threading.Event()

    def discover(self):
        self.entered.set()
        assert self.release.wait(10)
        return super().discover()


def test_long_scan_is_bounded_for_other_callers_and_cost_tools_keep_answering(tmp_path, monkeypatch):
    import cloudguard.server.governance_tools as gt
    monkeypatch.setattr(gt, "SCAN_WAIT_SECONDS", 0.2)
    src = BlockingSource()
    with TestClient(build_app(config(), contexts=contexts_with(src, tmp_path)), base_url="http://testserver") as c:
        out = {}
        first = threading.Thread(target=lambda: out.update(first=call_tool(c, "scan_tag_compliance")))
        first.start()
        assert src.entered.wait(10)
        out["second"] = call_tool(c, "get_resource_inventory", rid=2)
        out["cost"] = call_tool(c, "get_current_month_spending", rid=3)
        src.release.set()
        first.join(10)
    assert out["second"].status_code == 200
    second = out["second"].json()["result"]
    assert second["isError"] is True and "already running" in second["content"][0]["text"]
    assert out["cost"].json()["result"]["isError"] is False
    assert out["first"].json()["result"]["isError"] is False and src.calls == 1


# ---------- real Streamable HTTP over loopback ----------

def test_streamable_http_client_over_loopback(tmp_path):
    import socket

    import anyio
    import uvicorn
    from mcp import ClientSession
    from mcp.client.streamable_http import create_mcp_http_client, streamable_http_client

    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
    server = uvicorn.Server(uvicorn.Config(build_app(config(CLOUDGUARD_ALLOWED_HOSTS="127.0.0.1:*")),
                                           host="127.0.0.1", port=port, log_level="warning", lifespan="on"))
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    try:
        for _ in range(100):
            if server.started:
                break
            time.sleep(0.05)

        async def run(token):
            headers = {"authorization": f"Bearer {token}"}
            async with create_mcp_http_client(headers=headers) as http,                     streamable_http_client(f"http://127.0.0.1:{port}/mcp", http_client=http) as (r, w, _):
                async with ClientSession(r, w) as session:
                    init = await session.initialize()
                    tools = (await session.list_tools()).tools
                    result = await session.call_tool("get_resource_inventory", {})
                    return init, tools, result
        init, tools, result = anyio.run(run, TOKEN)
        assert init.protocolVersion == "2025-11-25" and len(tools) == 14
        assert result.isError is False and result.structuredContent["tool"] == "get_resource_inventory"
        with pytest.raises(Exception):
            anyio.run(run, "wrong-token")
    finally:
        server.should_exit = True
        thread.join(10)
