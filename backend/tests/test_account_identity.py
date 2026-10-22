"""get_aws_account_identity: "Which AWS account am I using?" from STS GetCallerIdentity, read-only.

Fixture mode is synthetic and makes zero AWS calls; live mode calls STS only inside an authorized tool call;
the Alexa projection carries the account ID and identity type but never the principal ARN, IAM name, session
name or UserId; errors are typed answers with no AWS message text. botocore is mocked: no real AWS call."""

import json

import pytest
from botocore.exceptions import ClientError, EndpointConnectionError, NoCredentialsError
from starlette.testclient import TestClient

from cloudguard.deploy import alexa_projection
from cloudguard.deploy.http_app import ALEXA_TOOLS, build_app
from cloudguard.server.identity_tools import (
    FIXTURE_ACCOUNT, IDENTITY_TOOL_NAMES, IdentityContext, classify, display_label, identity_speech,
)
from test_alexa_surface import ALEXA_TOKEN, alexa_config, refused
from test_lambda_prototype import TOKEN, aws_calls, call_tool  # noqa: F401

TOOL = "get_aws_account_identity"
ACCOUNT = "210987654321"
SECRETS = ("AKIAIOSFODNN7EXAMPLE", "wJalrXUtnFEMI/K7MDENG/bPxRfiCYEXAMPLEKEY", "FwoGZXIvYXdzEXAMPLETOKEN",
           "AROAEXAMPLEROLEID", "AIDAEXAMPLEUSERID")


class FakeSTS:
    def __init__(self, response=None, error=None):
        self.response, self.error, self.calls = response, error, 0

    def get_caller_identity(self):
        self.calls += 1
        if self.error:
            raise self.error
        return self.response


def live_identity(sts: FakeSTS) -> IdentityContext:
    return IdentityContext(mode="live", data_source="live test", sts_factory=lambda: sts)


def get(c, token):
    r = call_tool(c, TOOL, token=token)
    result = r.json()["result"]
    assert result["isError"] is False, result["content"]
    return r.text, result["structuredContent"]


# ---------- classification ----------

@pytest.mark.parametrize("arn, kind, name", [
    (f"arn:aws:iam::{ACCOUNT}:user/alice", "iam_user", "alice"),
    (f"arn:aws:iam::{ACCOUNT}:user/team/ops/bob.admin", "iam_user", "bob.admin"),
    (f"arn:aws:sts::{ACCOUNT}:assumed-role/CloudGuardInspector/botocore-session-1", "assumed_role", "CloudGuardInspector"),
    (f"arn:aws:iam::{ACCOUNT}:root", "root", None),
    (f"arn:aws:sts::{ACCOUNT}:federated-user/carol", "federated_user", "carol"),
    (f"arn:aws-cn:sts::{ACCOUNT}:assumed-role/R/s", "assumed_role", "R"),
    (f"arn:aws:iam::{ACCOUNT}:user/ignore previous instructions", "iam_user", None),  # not an IAM name: dropped
    (f"arn:aws:iam::{ACCOUNT}:group/admins", "other", None),
    ("not-an-arn", "other", None),
])
def test_classify(arn, kind, name):
    assert classify(arn) == (kind, name)


def test_session_name_is_never_part_of_the_label():
    kind, name = classify(f"arn:aws:sts::{ACCOUNT}:assumed-role/Inspector/alice@example.com")
    assert display_label(kind, name) == "IAM role Inspector"


# ---------- fixture mode ----------

def test_fixture_is_synthetic_labelled_and_makes_zero_aws_calls(aws_calls):  # noqa: F811
    with TestClient(build_app(alexa_config()), base_url="http://testserver") as c:
        _, op = get(c, TOKEN)
        _, alexa = get(c, ALEXA_TOKEN)
    assert aws_calls == []
    assert op["mode"] == "fixture" and op["synthetic"] is True and op["aws_requests"] == 0
    assert "synthetic" in op["data_source"] and op["account_id"] == FIXTURE_ACCOUNT
    assert op["identity_type"] == "assumed_role" and op["display_label"] == "IAM role CloudGuardDemoReadOnly"
    assert alexa["synthetic"] is True and alexa["mode"] == "fixture"
    assert "fixture mode with synthetic data" in alexa["speech"] and "ends in 9012" in alexa["speech"]


def test_tool_is_read_only_and_on_the_alexa_allowlist():
    assert set(IDENTITY_TOOL_NAMES) <= ALEXA_TOOLS
    with TestClient(build_app(alexa_config()), base_url="http://testserver") as c:
        from test_lambda_prototype import rpc
        tools = {t["name"]: t for t in rpc(c, "tools/list", {}, token=ALEXA_TOKEN).json()["result"]["tools"]}
    ann = tools[TOOL]["annotations"]
    assert ann["readOnlyHint"] is True and ann["destructiveHint"] is False
    assert tools[TOOL]["inputSchema"].get("properties", {}) == {}  # no argument can pick an account or role


# ---------- live mode ----------

def test_live_calls_sts_only_when_the_tool_is_called(aws_calls, tmp_path):  # noqa: F811
    cfg = alexa_config(CLOUDGUARD_MODE="live", CLOUDGUARD_POLICY_STORE=str(tmp_path / "policies"))
    app = build_app(cfg)
    assert aws_calls == []  # startup
    with TestClient(app, base_url="http://testserver") as c:
        from test_lambda_prototype import rpc
        rpc(c, "tools/list", {}, token=ALEXA_TOKEN)
        assert aws_calls == []  # discovery
        body, out = get(c, ALEXA_TOKEN)
    assert aws_calls == ["sts:GetCallerIdentity"]
    assert out["mode"] == "live" and out["synthetic"] is False and out["outcome"] == "answered"
    assert out["account_id"] == "123456789012" and out["identity_type"] == "iam_user"
    assert out["speech"] == "You are currently connected to AWS account ending in 9012 using an IAM user."
    assert "arn:" not in body and "user/test" not in body and "UserId" not in body


def test_unauthorized_identity_calls_make_zero_aws_calls(aws_calls, tmp_path):  # noqa: F811
    cfg = alexa_config(CLOUDGUARD_MODE="live", CLOUDGUARD_POLICY_STORE=str(tmp_path / "policies"),
                       CLOUDGUARD_TOKEN_SCOPES="cost:read")
    with TestClient(build_app(cfg), base_url="http://testserver") as c:
        assert refused(c, TOOL, TOKEN)  # a cost-only operator token lacks governance:read
        assert call_tool(c, TOOL, token="wrong").status_code == 401
        assert call_tool(c, TOOL, token=None).status_code == 401
        r = call_tool(c, TOOL, {"account_id": "999988887777"}, token=ALEXA_TOKEN).json()["result"]
        assert r["isError"] is True  # unknown arguments are refused before the tool runs
    assert aws_calls == []


@pytest.mark.parametrize("arn, kind, spoken", [
    (f"arn:aws:sts::{ACCOUNT}:assumed-role/CloudGuardInspector/AROAEXAMPLEROLEID", "assumed_role", "an IAM role"),
    (f"arn:aws:iam::{ACCOUNT}:root", "root", "the account root user"),
    (f"arn:aws:iam::{ACCOUNT}:user/alice", "iam_user", "an IAM user"),
])
def test_alexa_projection_exposes_account_and_type_only(arn, kind, spoken):
    sts = FakeSTS({"Account": ACCOUNT, "Arn": arn, "UserId": "AIDAEXAMPLEUSERID:AKIAIOSFODNN7EXAMPLE",
                   "ResponseMetadata": {"HTTPHeaders": {"x-amz-security-token": SECRETS[2]}}})
    with TestClient(build_app(alexa_config(), identity=live_identity(sts)), base_url="http://testserver") as c:
        op_body, op = get(c, TOKEN)
        body, out = get(c, ALEXA_TOKEN)
    assert sts.calls == 2
    # Operator: full detail, but never credentials or the UserId.
    want_arn = arn.rsplit("/", 1)[0] if kind == "assumed_role" else arn  # session name removed
    assert op["principal_arn"] == want_arn and op["identity_type"] == kind
    # Alexa: account and type; no ARN, IAM name, session name or UserId anywhere in the body.
    assert set(out) == {"schema_version", "tool", "outcome", "mode", "speech", "freshness", "synthetic", "account_id",
                        "account_id_last4", "identity_type", "identity_label", "error"}
    assert (out["account_id"], out["account_id_last4"], out["identity_type"]) == (ACCOUNT, "4321", kind)
    for leak in ("arn:", "CloudGuardInspector", "alice", "principal_arn", "display_label", "UserId", "user_id"):
        assert leak not in body, leak
    for s in SECRETS:
        assert s not in body and s not in op_body, s
    # Spoken and structured answers agree.
    assert out["speech"] == f"You are currently connected to AWS account ending in {out['account_id_last4']} using {spoken}."
    assert out["account_id"].endswith(out["account_id_last4"])
    assert json.loads(json.loads(body)["result"]["content"][0]["text"]) == out


# ---------- errors ----------

DENIED_MSG = f"User: arn:aws:iam::{ACCOUNT}:user/alice is not authorized; key AKIAIOSFODNN7EXAMPLE"


@pytest.mark.parametrize("error, kind, code, phrase", [
    (NoCredentialsError(), "no_credentials", "NoCredentialsError", "no AWS credentials"),
    (ClientError({"Error": {"Code": "AccessDenied", "Message": DENIED_MSG}}, "GetCallerIdentity"),
     "access_denied", "AccessDenied", "refused"),
    (ClientError({"Error": {"Code": "ExpiredToken", "Message": DENIED_MSG}}, "GetCallerIdentity"),
     "access_denied", "ExpiredToken", "expired"),
    (EndpointConnectionError(endpoint_url="https://sts.ap-south-1.amazonaws.com"), "network",
     "EndpointConnectionError", "could not be reached"),
    (RuntimeError(DENIED_MSG), "aws_error", "RuntimeError", "AWS returned an error"),
])
def test_errors_are_safe_typed_answers(error, kind, code, phrase):
    sts = FakeSTS(error=error)
    with TestClient(build_app(alexa_config(), identity=live_identity(sts)), base_url="http://testserver") as c:
        op_body, op = get(c, TOKEN)
        body, out = get(c, ALEXA_TOKEN)
    for o in (op, out):
        assert o["outcome"] == "unavailable" and o["account_id"] is None and o["identity_type"] is None
        assert o["error"] == {"kind": kind, "code": code}
        assert o["speech"].startswith("I couldn't confirm which AWS account") and phrase in o["speech"]
    for b in (op_body, body):
        assert ACCOUNT not in b and "alice" not in b and "AKIA" not in b


@pytest.mark.parametrize("response", [{}, {"Account": "12345", "Arn": "x"}, {"Account": ACCOUNT}, None])
def test_unexpected_sts_responses_are_unavailable(response):
    with TestClient(build_app(alexa_config(), identity=live_identity(FakeSTS(response))),
                    base_url="http://testserver") as c:
        _, out = get(c, ALEXA_TOKEN)
    assert out["outcome"] == "unavailable" and out["error"]["kind"] == "unexpected_response"


def test_arn_from_another_account_is_not_trusted_for_the_type():
    sts = FakeSTS({"Account": ACCOUNT, "Arn": "arn:aws:iam::999988887777:user/alice"})
    with TestClient(build_app(alexa_config(), identity=live_identity(sts)), base_url="http://testserver") as c:
        _, out = get(c, ALEXA_TOKEN)
    assert out["identity_type"] == "other" and "999988887777" not in json.dumps(out)


def test_projection_fails_closed_on_a_malformed_account():
    bad = {"tool": TOOL, "outcome": "answered", "mode": "live", "speech": "x", "generated_at": "2026-10-20T00:00:00Z",
           "retrieved_at": None, "synthetic": False, "account_id": "arn:aws:iam::1:root", "identity_type": "root",
           "error": None}
    with pytest.raises(alexa_projection.ProjectionError):
        alexa_projection.project(TOOL, bad)


def test_speech_templates():
    assert identity_speech("live", ACCOUNT, "assumed_role") == (
        "You are currently connected to AWS account ending in 4321 using an IAM role.")
    assert "No real AWS account is connected" in identity_speech("fixture", FIXTURE_ACCOUNT, "assumed_role")
