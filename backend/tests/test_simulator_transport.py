"""What the web simulator (web/) relies on from the deployment prototype, as a browser behind the Vite same-origin
proxy sends it: loopback Origin headers, the MCP handshake at each protocol version, the initialized notification,
Alexa-projected results, and NO CORS (cross-origin browser calls are refused, so the proxy is required).
The backend is unchanged by the simulator; these tests pin the behaviour it depends on."""

import json

import pytest
from starlette.testclient import TestClient

from cloudguard.deploy.http_app import ALEXA_TOOLS, build_app
from test_alexa_surface import ALEXA_TOKEN, alexa_config
from test_lambda_prototype import MCP_HEADERS, VERSIONS

SIM_ORIGIN = "http://127.0.0.1:5173"  # the Vite dev server


def post(c, body, *, token=ALEXA_TOKEN, origin=SIM_ORIGIN, version="2025-11-25"):
    headers = dict(MCP_HEADERS)
    if origin:
        headers["origin"] = origin
    if token:
        headers["authorization"] = f"Bearer {token}"
    if version:
        headers["mcp-protocol-version"] = version
    return c.post("/mcp", headers=headers, content=json.dumps(body))


def init(version):
    return {"jsonrpc": "2.0", "id": 1, "method": "initialize",
            "params": {"protocolVersion": version, "capabilities": {},
                       "clientInfo": {"name": "cloudguard-mcp-simulator", "version": "0.1.0"}}}


@pytest.fixture
def client():
    # The default allow-list (127.0.0.1:*, localhost:*, [::1]:*) plus the test client's own host name.
    with TestClient(build_app(alexa_config(CLOUDGUARD_ALLOWED_HOSTS="testserver,127.0.0.1:*,localhost:*")),
                    base_url="http://testserver") as c:
        yield c


@pytest.mark.parametrize("version", VERSIONS)
def test_handshake_from_the_simulator_origin(client, version):
    r = post(client, init(version), version=None)
    assert r.status_code == 200 and r.json()["result"]["protocolVersion"] == version
    assert "mcp-session-id" not in r.headers  # stateless: the client must not depend on a session id
    note = post(client, {"jsonrpc": "2.0", "method": "notifications/initialized"}, version=version)
    assert note.status_code == 202


def test_unknown_protocol_version_is_answered_with_the_latest(client):
    r = post(client, init("2099-01-01"), version=None)
    assert r.status_code == 200 and r.json()["result"]["protocolVersion"] == "2025-11-25"


def test_discovery_and_projected_call_from_the_simulator(client):
    tools = post(client, {"jsonrpc": "2.0", "id": 2, "method": "tools/list"}).json()["result"]["tools"]
    assert {t["name"] for t in tools} == ALEXA_TOOLS
    assert all(t["outputSchema"]["properties"]["schema_version"]["default"] == "alexa-1" for t in tools)
    r = post(client, {"jsonrpc": "2.0", "id": 3, "method": "tools/call",
                      "params": {"name": "get_monthly_aws_bill", "arguments": {}}}).json()["result"]
    assert r["isError"] is False and r["structuredContent"]["schema_version"] == "alexa-1"
    assert json.loads(r["content"][0]["text"]) == r["structuredContent"]


def test_tool_error_reaches_the_client_as_is_error(client):
    r = post(client, {"jsonrpc": "2.0", "id": 4, "method": "tools/call",
                      "params": {"name": "explain_finding", "arguments": {"finding_id": "tagcomp-0000000000000000"}}})
    result = r.json()["result"]
    assert r.status_code == 200 and result["isError"] is True and "not in the latest scan" in result["content"][0]["text"]


def test_foreign_browser_origin_is_refused(client):
    assert post(client, init("2025-11-25"), origin="https://evil.example", version=None).status_code == 403


def test_no_cors_so_cross_origin_browsers_need_the_same_origin_proxy(client):
    r = client.options("/mcp", headers={"origin": SIM_ORIGIN, "access-control-request-method": "POST",
                                        "access-control-request-headers": "authorization,content-type"})
    assert r.status_code == 401 and "access-control-allow-origin" not in r.headers


def test_token_only_counts_in_the_authorization_header(client):
    body = init("2025-11-25") | {"authorization": f"Bearer {ALEXA_TOKEN}"}
    body["params"]["_meta"] = {"token": ALEXA_TOKEN}
    assert post(client, body, token=None, version=None).status_code == 401
    assert post(client, init("2025-11-25"), token="wrong", version=None).status_code == 401
