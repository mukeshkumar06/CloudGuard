"""Cost MCP server over authenticated Streamable HTTP (cloudguard.deploy): bearer auth, cost:read authorization,
host/origin checks, body limit and safe errors, exercised over ASGI exactly as Lambda Web Adapter would forward
Function URL requests, and once over real loopback HTTP. Fixture data, or live mode with every botocore call
mocked: nothing reaches AWS.
"""

import hashlib
import json
import logging
import os
import socket
import subprocess
import sys
import threading
import time
from pathlib import Path

import pytest
from starlette.testclient import TestClient

from cloudguard.deploy.http_app import (
    KNOWN_SCOPES, LOCAL_HOSTS, MAX_BODY_BYTES, BearerAuthApp, ConfigError, DeployConfig, Principal,
    StaticTokenAuthenticator, authorize_tool, build_app, new_token, token_digest,
)
from cloudguard.server.cost_tools import COST_TOOL_NAMES

BACKEND = Path(__file__).resolve().parents[1]
TOKEN = "c4-test-token-not-a-secret-0123456789abcdefghijk"
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


def rpc(client, method, params=None, *, rid=1, version="2025-11-25", token=TOKEN, host=None, origin=None):
    headers = dict(MCP_HEADERS)
    if version:
        headers["mcp-protocol-version"] = version
    if token is not None:
        headers["authorization"] = f"Bearer {token}"
    if host:
        headers["host"] = host
    if origin:
        headers["origin"] = origin
    body = {"jsonrpc": "2.0", "id": rid, "method": method, **({"params": params} if params is not None else {})}
    return client.post("/mcp", headers=headers, json=body)


def initialize(client, version="2025-11-25"):
    return rpc(client, "initialize", {"protocolVersion": version, "capabilities": {},
                                      "clientInfo": {"name": "c4-test", "version": "0"}}, version=None)


def call_tool(client, name, arguments=None, **kw):
    return rpc(client, "tools/call", {"name": name, "arguments": arguments or {}}, **kw)


# ---------- MCP over the adapter-facing HTTP interface ----------

@pytest.mark.parametrize("version", VERSIONS)
def test_initialize_negotiates_each_protocol_version(client, version):
    r = initialize(client, version)
    assert r.status_code == 200 and r.json()["result"]["protocolVersion"] == version
    assert "mcp-session-id" not in r.headers  # stateless


def test_exactly_the_six_cost_tools_are_listed_and_each_runs_in_fixture_mode(client):
    tools = rpc(client, "tools/list", {}).json()["result"]["tools"]
    assert sorted(t["name"] for t in tools) == sorted(COST_TOOL_NAMES) and len(tools) == 6
    for t in tools:
        assert t["annotations"]["readOnlyHint"] is True and t["annotations"]["destructiveHint"] is False
        assert t["inputSchema"]["additionalProperties"] is False
    for name in COST_TOOL_NAMES:
        result = call_tool(client, name).json()["result"]
        assert result["isError"] is False and result["structuredContent"]["tool"] == name
        assert result["structuredContent"]["mode"] == "fixture"


def test_results_carry_no_account_identifiers_or_credentials(client):
    text = json.dumps([call_tool(client, n).json() for n in COST_TOOL_NAMES])
    assert "arn:aws" not in text and "123456789012" not in text and TOKEN not in text


# ---------- authentication ----------

@pytest.mark.parametrize("token", [None, "wrong", "", TOKEN + "x", TOKEN.upper()])
def test_missing_or_invalid_token_is_a_generic_401(client, token):
    r = call_tool(client, "get_monthly_aws_bill", token=token)
    assert r.status_code == 401 and r.json() == {"error": "unauthorized"}
    assert r.headers["www-authenticate"] == 'Bearer realm="cloudguard"'


@pytest.mark.parametrize("value", ["Basic abc", "Bearer", "Bearer   ", TOKEN, f"Token {TOKEN}"])
def test_wrong_scheme_or_malformed_header_is_401(client, value):
    headers = dict(MCP_HEADERS, authorization=value)
    assert client.post("/mcp", headers=headers, json={}).status_code == 401


def test_get_and_delete_also_require_auth(client):
    assert client.get("/mcp").status_code == 401
    assert client.delete("/mcp").status_code == 401


def test_fixture_mode_still_requires_authentication(client):
    assert initialize(client).status_code == 200
    assert rpc(client, "tools/list", {}, token=None).status_code == 401


def test_token_without_cost_read_is_403():
    with TestClient(build_app(config(CLOUDGUARD_TOKEN_SCOPES="")), base_url="http://testserver") as c:
        r = call_tool(c, "get_monthly_aws_bill")
    assert r.status_code == 403 and r.json() == {"error": "forbidden"}
    assert 'error="insufficient_scope"' in r.headers["www-authenticate"]


def test_token_never_logged(client, caplog):
    caplog.set_level(logging.DEBUG)
    call_tool(client, "get_monthly_aws_bill")
    call_tool(client, "get_monthly_aws_bill", token="wrong-guess-token")
    assert TOKEN not in caplog.text and "wrong-guess-token" not in caplog.text
    lines = [json.loads(r.getMessage()) for r in caplog.records if r.name == "cloudguard.deploy"]
    assert {(line["status"], line["auth"]) for line in lines} >= {(200, "ok"), (401, "invalid")}


def test_authenticator_compares_in_constant_time_against_the_digest_only():
    auth = StaticTokenAuthenticator(bytes.fromhex(DIGEST), "s", KNOWN_SCOPES)
    assert TOKEN not in repr(vars(auth))
    assert isinstance(auth.authenticate(f"Bearer {TOKEN}"), Principal)
    assert auth.authenticate(None) == "missing" and auth.authenticate("Bearer x") == "invalid"


# ---------- token strength ----------

@pytest.mark.parametrize("weak", ["", "token", "changeme", "test-token", "password"])
def test_weak_token_digest_is_refused_at_startup(weak):
    with pytest.raises(ConfigError, match="known weak token"):
        config(CLOUDGUARD_TOKEN_SHA256=hashlib.sha256(weak.encode()).hexdigest())


@pytest.mark.parametrize("weak", ["short", "a" * 64, "0123456789" * 3])
def test_token_digest_refuses_short_or_low_variety_tokens(weak):
    with pytest.raises(ConfigError, match="at least 32 bytes"):
        token_digest(weak)


def test_new_token_is_random_32_bytes_and_its_digest_is_accepted():
    (a, da), (b, _) = new_token(), new_token()
    assert a != b and len(a) >= 43 and da == hashlib.sha256(a.encode()).hexdigest()
    assert config(CLOUDGUARD_TOKEN_SHA256=da).token_sha256.hex() == da


def test_new_token_cli_prints_token_and_digest(capsys):
    import cloudguard.deploy.__main__ as entry
    assert entry.main(["--new-token"]) == 0
    out = capsys.readouterr().out
    token = out.split("): ")[1].splitlines()[0]
    assert f"CLOUDGUARD_TOKEN_SHA256={hashlib.sha256(token.encode()).hexdigest()}" in out


# ---------- tool authorization ----------

def test_unknown_tool_is_refused_before_lookup(client):
    result = call_tool(client, "get_resource_inventory").json()["result"]
    assert result["isError"] is True and "not authorized" in result["content"][0]["text"]


def test_unknown_arguments_are_refused(client):
    for args in ({"account_id": "1"}, {"role_arn": "x"}, {"region": "us-west-2"}):
        result = call_tool(client, "get_monthly_aws_bill", args).json()["result"]
        assert result["isError"] is True and "does not accept" in result["content"][0]["text"]


def test_tool_argument_validation_still_applies(client):
    result = call_tool(client, "get_cost_breakdown", {"limit": 0}).json()["result"]
    assert result["isError"] is True


def test_authorization_is_checked_before_argument_validation():
    import cloudguard.deploy.http_app as http_app
    with TestClient(build_app(config()), base_url="http://testserver") as c:
        original = http_app.TOOL_SCOPES.copy()
        http_app.TOOL_SCOPES.clear()  # nothing authorized: even an invalid argument gets "not authorized"
        try:
            result = call_tool(c, "get_cost_breakdown", {"limit": 0, "account_id": "1"}).json()["result"]
        finally:
            http_app.TOOL_SCOPES.update(original)
    assert result["isError"] is True and "not authorized" in result["content"][0]["text"]


def test_authorizer_unit():
    p = Principal("s", frozenset({"cost:read"}), "static-token")
    assert all(authorize_tool(p, t) for t in COST_TOOL_NAMES)
    assert not authorize_tool(p, "scan_tag_compliance") and not authorize_tool(None, "get_monthly_aws_bill")
    assert not authorize_tool(Principal("s", frozenset(), "static-token"), "get_monthly_aws_bill")
    assert not authorize_tool({"subject": "spoofed", "scopes": ["cost:read"]}, "get_monthly_aws_bill")


# ---------- HTTP safety ----------

def test_health_needs_no_token_and_reveals_nothing(client):
    r = client.get("/healthz")
    assert r.status_code == 200 and r.json() == {"status": "ok", "mode": "fixture"}


def test_oauth_metadata_is_not_advertised(client):
    assert client.get("/.well-known/oauth-protected-resource").status_code == 404


def test_unexpected_host_rejected_even_with_valid_token(client):
    assert rpc(client, "tools/list", {}, host="attacker.example").status_code == 421


def test_foreign_browser_origin_rejected(client):
    assert rpc(client, "tools/list", {}, origin="https://evil.example").status_code == 403


def test_no_cors_headers(client):
    r = rpc(client, "tools/list", {}, origin="http://testserver")
    assert not any(k.lower().startswith("access-control-") for k in r.headers)


def test_default_hosts_are_localhost_only():
    assert DeployConfig.from_env({"CLOUDGUARD_TOKEN_SHA256": DIGEST}).allowed_hosts == LOCAL_HOSTS


def test_function_url_hostname_can_be_allowed_explicitly():
    host = "abc123.lambda-url.ap-south-1.on.aws"
    with TestClient(build_app(config(CLOUDGUARD_ALLOWED_HOSTS=host)), base_url=f"https://{host}") as c:
        assert rpc(c, "tools/list", {}).status_code == 200
        assert rpc(c, "tools/list", {}, host="other.lambda-url.ap-south-1.on.aws").status_code == 421


@pytest.mark.parametrize("body", [b"{not json", b"", b"[1, 2", b'{"jsonrpc": "2.0", "id": 1}'])
def test_malformed_body_is_a_client_error_without_echo(client, body):
    headers = dict(MCP_HEADERS, authorization=f"Bearer {TOKEN}")
    r = client.post("/mcp", headers=headers, content=body)
    assert 400 <= r.status_code < 500
    assert TOKEN not in r.text and "Traceback" not in r.text


def test_oversized_body_is_413_by_declared_length(client):
    headers = dict(MCP_HEADERS, authorization=f"Bearer {TOKEN}")
    r = client.post("/mcp", headers=headers, content=b"x" * (MAX_BODY_BYTES + 1))
    assert r.status_code == 413 and r.json() == {"error": "payload_too_large"}


def test_oversized_streamed_body_is_413(client):
    def chunks():  # no Content-Length: the limit is enforced while the body is read
        for _ in range(3):
            yield b"x" * (MAX_BODY_BYTES // 2 + 1)
    headers = dict(MCP_HEADERS, authorization=f"Bearer {TOKEN}")
    r = client.post("/mcp", headers=headers, content=chunks())
    assert "content-length" not in {k.lower() for k in r.request.headers}
    assert r.status_code == 413 and r.json() == {"error": "payload_too_large"}


def test_oversized_body_without_token_is_still_401(client):
    r = client.post("/mcp", headers=MCP_HEADERS, content=b"x" * (MAX_BODY_BYTES + 1))
    assert r.status_code == 401


def test_unexpected_errors_are_generic_500(caplog):
    async def broken(scope, receive, send):
        raise RuntimeError(r"boom at C:\secret\path arn:aws:iam::999988887777:user/bob")
    auth = StaticTokenAuthenticator(bytes.fromhex(DIGEST), "static-token", KNOWN_SCOPES)
    c = TestClient(BearerAuthApp(broken, auth, "fixture"), base_url="http://testserver")  # no lifespan
    r = c.post("/mcp", headers={"authorization": f"Bearer {TOKEN}"}, json={})
    assert r.status_code == 500 and r.json() == {"error": "internal_error"}
    assert "secret" not in r.text and "bob" not in r.text and "secret" not in caplog.text


# ---------- configuration ----------

@pytest.mark.parametrize("env,needle", [
    ({}, "CLOUDGUARD_TOKEN_SHA256"),
    ({"CLOUDGUARD_TOKEN_SHA256": "abc"}, "CLOUDGUARD_TOKEN_SHA256"),
    ({"CLOUDGUARD_TOKEN_SHA256": DIGEST, "CLOUDGUARD_MODE": "prod"}, "fixture or live"),
    ({"CLOUDGUARD_TOKEN_SHA256": DIGEST, "CLOUDGUARD_AUTH_MODE": "oauth"}, "oauth is not implemented"),
    ({"CLOUDGUARD_TOKEN_SHA256": DIGEST, "CLOUDGUARD_AUTH_MODE": "none"}, "must be static-token"),
    ({"CLOUDGUARD_TOKEN_SHA256": DIGEST, "CLOUDGUARD_TOKEN_SCOPES": "cost:read governance:read"}, "unknown scope"),
    ({"CLOUDGUARD_TOKEN_SHA256": DIGEST, "CLOUDGUARD_TOKEN_SCOPES": "alexa:read"}, "unknown scope"),
    ({"CLOUDGUARD_TOKEN_SHA256": DIGEST, "CLOUDGUARD_TOKEN_SUBJECT": "bad subject"}, "CLOUDGUARD_TOKEN_SUBJECT"),
    ({"CLOUDGUARD_TOKEN_SHA256": DIGEST, "CLOUDGUARD_ALLOWED_HOSTS": "*"}, "invalid allowed host"),
    ({"CLOUDGUARD_TOKEN_SHA256": DIGEST, "CLOUDGUARD_ALLOWED_HOSTS": "*:*"}, "invalid allowed host"),
    ({"CLOUDGUARD_TOKEN_SHA256": DIGEST, "CLOUDGUARD_ALLOWED_HOSTS": "evil.example/path"}, "invalid allowed host"),
])
def test_unsafe_configuration_refused(env, needle):
    with pytest.raises(ConfigError, match=needle):
        DeployConfig.from_env(env)


def test_deploy_entry_binds_loopback_only(monkeypatch):
    import cloudguard.deploy.__main__ as entry
    seen = {}
    monkeypatch.setenv("CLOUDGUARD_TOKEN_SHA256", DIGEST)
    monkeypatch.setattr(entry.uvicorn, "run", lambda app, **kw: seen.update(kw))
    assert entry.main(["--port", "8099"]) == 0
    assert seen["host"] == "127.0.0.1" and seen["port"] == 8099


def test_deploy_entry_refuses_bad_configuration(monkeypatch, capsys):
    import cloudguard.deploy.__main__ as entry
    monkeypatch.delenv("CLOUDGUARD_TOKEN_SHA256", raising=False)
    assert entry.main([]) == 2 and "configuration error" in capsys.readouterr().err


def test_fixture_app_imports_no_boto3_and_no_later_phase_modules():
    script = ("import sys\nfrom cloudguard.deploy.http_app import build_app, DeployConfig\n"
              f"build_app(DeployConfig.from_env({{'CLOUDGUARD_TOKEN_SHA256': '{DIGEST}'}}))\n"
              "assert 'boto3' not in sys.modules\n"
              "bad = [m for m in sys.modules if m.startswith(('cloudguard.server.governance_tools', "
              "'cloudguard.server.identity_tools', 'cloudguard.deploy.alexa_projection', "
              "'cloudguard.governance.recommendations'))]\n"
              "assert not bad, bad\nprint('OK')")
    proc = subprocess.run([sys.executable, "-B", "-c", script], cwd=BACKEND, capture_output=True, text=True,
                          timeout=120)
    assert proc.returncode == 0 and "OK" in proc.stdout, proc.stderr


def test_stdio_entry_is_unchanged_and_has_no_authorizer():
    from cloudguard.server.app import create_server
    from cloudguard.server.context import CostContext
    assert create_server(CostContext.fixture()).authorizer is None


# ---------- live mode: mocked AWS only ----------

@pytest.fixture
def aws_calls(monkeypatch):
    """Mock every botocore call: record it and answer Cost Explorer with empty, valid responses."""
    import botocore.client
    for key in ("AWS_PROFILE", "AWS_DEFAULT_PROFILE", "AWS_SESSION_TOKEN"):
        monkeypatch.delenv(key, raising=False)
    monkeypatch.setenv("AWS_ACCESS_KEY_ID", "testing")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "testing")
    monkeypatch.setenv("AWS_DEFAULT_REGION", "ap-south-1")
    monkeypatch.setenv("AWS_CONFIG_FILE", os.devnull)
    monkeypatch.setenv("AWS_SHARED_CREDENTIALS_FILE", os.devnull)
    calls = []
    responses = {"GetCostAndUsage": {"ResultsByTime": []}, "GetAnomalyMonitors": {"AnomalyMonitors": []}}

    def fake(self, operation, params):
        calls.append(f"{self.meta.service_model.service_name}:{operation}")
        if operation not in responses:
            raise AssertionError(f"unexpected AWS call {operation}")
        return responses[operation]
    monkeypatch.setattr(botocore.client.BaseClient, "_make_api_call", fake)
    return calls


def test_live_mode_makes_no_aws_call_before_an_authorized_tool_call(aws_calls):
    app = build_app(config(CLOUDGUARD_MODE="live", CLOUDGUARD_TOKEN_SCOPES=""))
    assert aws_calls == []  # startup only builds the client
    with TestClient(app, base_url="http://testserver") as c:
        assert c.get("/healthz").json() == {"status": "ok", "mode": "live"}
        for token in (None, "wrong", ""):
            assert call_tool(c, "get_monthly_aws_bill", token=token).status_code == 401
        assert call_tool(c, "get_monthly_aws_bill").status_code == 403  # no cost:read
    with TestClient(build_app(config(CLOUDGUARD_MODE="live")), base_url="http://testserver") as c:
        assert call_tool(c, "get_monthly_aws_bill", host="attacker.example").status_code == 421
        assert call_tool(c, "get_monthly_aws_bill", {"account_id": "999988887777"}).json()["result"]["isError"]
    assert aws_calls == []


def test_live_mode_authorized_call_reaches_cost_explorer_read_only(aws_calls):
    with TestClient(build_app(config(CLOUDGUARD_MODE="live")), base_url="http://testserver") as c:
        bill = call_tool(c, "get_monthly_aws_bill").json()["result"]
        anomalies = call_tool(c, "get_cost_anomalies").json()["result"]
    assert bill["isError"] is False and bill["structuredContent"]["mode"] == "live"
    assert anomalies["structuredContent"]["outcome"] == "unavailable"  # no monitor
    assert aws_calls == ["ce:GetCostAndUsage", "ce:GetAnomalyMonitors"]


# ---------- real Streamable HTTP over loopback ----------

def test_streamable_http_client_over_loopback():
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
            async with create_mcp_http_client(headers=headers) as http, \
                    streamable_http_client(f"http://127.0.0.1:{port}/mcp", http_client=http) as (r, w, _):
                async with ClientSession(r, w) as session:
                    init = await session.initialize()
                    tools = (await session.list_tools()).tools
                    result = await session.call_tool("get_monthly_aws_bill", {})
                    return init, tools, result
        init, tools, result = anyio.run(run, TOKEN)
        assert init.protocolVersion == "2025-11-25" and sorted(t.name for t in tools) == sorted(COST_TOOL_NAMES)
        assert result.isError is False and result.structuredContent["tool"] == "get_monthly_aws_bill"
        assert result.structuredContent["speech"] and result.structuredContent["report"]
        with pytest.raises(Exception):
            anyio.run(run, "wrong-token")
    finally:
        server.should_exit = True
        thread.join(10)
