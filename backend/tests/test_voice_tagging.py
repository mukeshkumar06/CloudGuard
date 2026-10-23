"""Personal voice tagging (/operator): operator sessions, spoken confirmation challenges, and execution through the
existing proposal, approval and executor workflow.

Live mode runs against FakeAws (test_live_tagging): an in-memory account behind the same client calls
LiveTagAccount makes, with STS identities faked. Fixture mode uses the simulated FixtureAccount. No test calls AWS.
"""

import hashlib
import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from starlette.testclient import TestClient

from authz_helpers import FakeSts, seeded_authz
from cloudguard.deploy.http_app import ConfigError, DeployConfig, build_app, build_voice_app
from cloudguard.governance.discovery import FixtureSource
from cloudguard.governance.remediation.approval import DecisionService, PersonalAccountWaiver
from cloudguard.governance.remediation.authz import LocalAuthzStore
from cloudguard.governance.remediation.live_writer import LiveTagAccount
from cloudguard.governance.remediation.models import OperatingMode
from cloudguard.voice_tagging.backends import LiveBackend
from cloudguard.voice_tagging.confirm import is_overwrite_ack, read_confirmation
from test_live_tagging import ACCT, FIXTURE, FakeAws, _err

TOKEN = "mcp-operator-token-not-a-secret-0123456789abcdef"
ALEXA = "alexa-token-not-a-secret-0123456789abcdefghijk"
VOICE = "voice-operator-credential-not-a-secret-9876543210"
sha = lambda t: hashlib.sha256(t.encode()).hexdigest()  # noqa: E731
OPERATOR_ROLE = f"arn:aws:sts::{ACCT}:assumed-role/Operator/s1"
OPERATOR_IAM = f"arn:aws:iam::{ACCT}:role/Operator"
TAGGER = f"arn:aws:iam::{ACCT}:role/CloudGuardTaggerRole"
BASE = "http://127.0.0.1:8080"
H = {"x-cloudguard-voice": "1", "content-type": "application/json"}


class Clock:
    def __init__(self):
        self.now = datetime(2026, 10, 23, 9, 0, tzinfo=UTC)

    def __call__(self):
        return self.now


def env(tmp_path, **extra):
    e = {"CLOUDGUARD_TOKEN_SHA256": sha(TOKEN), "CLOUDGUARD_ALEXA_TOKEN_SHA256": sha(ALEXA),
         "CLOUDGUARD_VOICE_TAGGING_ENABLED": "true", "CLOUDGUARD_VOICE_OPERATOR_TOKEN_SHA256": sha(VOICE),
         "CLOUDGUARD_VOICE_TAGGING_ACCOUNTS": ACCT, "CLOUDGUARD_VOICE_TAGGING_STORE": str(tmp_path / "store"),
         "CLOUDGUARD_VOICE_TAGGING_KILL_FILE": str(tmp_path / "kill"),
         "CLOUDGUARD_POLICY_STORE": str(tmp_path / "policies")}
    e.update(extra)
    return {k: v for k, v in e.items() if v is not None}


class AwsSource:
    """Discovery over FakeAws's current state (so a deleted resource or a changed tag is seen on re-resolution)."""

    mode = "live"

    def __init__(self, aws, region, service):
        self.aws, self.region, self.service = aws, region, service

    def discover(self):
        d = FixtureSource(FIXTURE).discover()
        res = [r.model_copy(update={"tags": self.aws.res[r.resource_arn]["tags"]}) for r in d.resources
               if r.resource_arn in self.aws.res and r.region == self.region and r.service == self.service]
        return d.model_copy(update={"resources": res, "regions": [self.region], "service_errors": []})


class LiveWorld:
    """A live-mode server whose AWS is FakeAws, with the operator allow-listed for create/approve/execute."""

    def __init__(self, tmp_path, account=ACCT, caller=OPERATOR_ROLE, grants=None, **extra):
        self.clock, self.aws = Clock(), FakeAws()
        self.sts = FakeSts(caller, account)
        cfg = DeployConfig.from_env(env(tmp_path, CLOUDGUARD_MODE="live", CLOUDGUARD_VOICE_TAGGING_ROLE_ARN=TAGGER,
                                        **extra))
        seeded_authz({"accounts": {ACCT: grants or {p: [OPERATOR_IAM] for p in
                                                    ("create", "approve", "reject", "execute")}}},
                     store=LocalAuthzStore(Path(cfg.voice.store) / "authz"))
        self.tag_account = lambda acct: LiveTagAccount(session=None, account_id=acct, client_factory=self.aws.factory)
        backend = LiveBackend(None, TAGGER, sts_client=self.sts,
                              source_factory=lambda region, service: AwsSource(self.aws, region, service),
                              tag_account_factory=lambda acct: self.tag_account(acct))
        self.app = build_voice_app(cfg, backend, clock=self.clock)
        self.cfg = cfg
        self.c = TestClient(self.app, base_url=BASE)

    def login(self, credential=VOICE):
        return self.c.post("/operator/login", data={"credential": credential}, follow_redirects=False)

    def prepare(self, **spec):
        body = {"service": "lambda", "region": "ap-south-1", "match": "contains", "text": "worker",
                "tags": {"Team": "mukesh"}, "channel": "voice"} | spec
        return self.c.post("/operator/voice-tagging/prepare", json=body, headers=H)

    def confirm(self, challenge, transcript="yes", **kw):
        return self.c.post("/operator/voice-tagging/confirm", headers=H,
                           json={"challenge_id": challenge, "transcript": transcript, "channel": "voice", **kw})


@pytest.fixture
def live(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    w = LiveWorld(tmp_path)
    assert w.login().status_code == 303
    return w


def ready(w, **spec):
    r = w.prepare(**spec)
    assert r.status_code == 200, r.text
    out = r.json()
    assert out["status"] == "READY", out
    return out


# ================================================================ configuration and authentication

def test_disabled_by_default_and_operator_routes_answer_404(tmp_path):
    cfg = DeployConfig.from_env(env(tmp_path, CLOUDGUARD_VOICE_TAGGING_ENABLED=None))
    assert cfg.voice.enabled is False
    with TestClient(build_app(cfg), base_url=BASE) as c:
        assert c.get("/operator/session").status_code == 404
        assert c.post("/operator/login", data={"credential": VOICE}).status_code == 404


@pytest.mark.parametrize("extra,match", [
    ({"CLOUDGUARD_VOICE_OPERATOR_TOKEN_SHA256": None}, "OPERATOR_TOKEN"),
    ({"CLOUDGUARD_VOICE_OPERATOR_TOKEN_SHA256": sha(ALEXA)}, "must differ"),
    ({"CLOUDGUARD_VOICE_OPERATOR_TOKEN_SHA256": sha(TOKEN)}, "must differ"),
    ({"CLOUDGUARD_VOICE_TAGGING_ACCOUNTS": None}, "ACCOUNTS"),
    ({"CLOUDGUARD_VOICE_TAGGING_ACCOUNTS": "12345"}, "ACCOUNTS"),
    ({"CLOUDGUARD_MODE": "live"}, "ROLE_ARN"),
    ({"CLOUDGUARD_MODE": "live", "CLOUDGUARD_VOICE_TAGGING_ROLE_ARN": "arn:aws:iam::999988887777:role/T"},
     "allow-listed"),
])
def test_enabled_but_incomplete_configuration_refuses_to_start(tmp_path, extra, match):
    with pytest.raises(ConfigError, match=match):
        DeployConfig.from_env(env(tmp_path, **extra))


def test_valid_operator_session_is_an_httponly_cookie_and_never_echoes_secrets(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    w = LiveWorld(tmp_path)
    r = w.login()
    assert r.status_code == 303 and r.headers["location"] == "/?operator=ok"
    cookie = r.headers["set-cookie"]
    assert "HttpOnly" in cookie and "SameSite=Strict" in cookie and "Path=/operator" in cookie
    assert VOICE not in r.text and VOICE not in cookie
    s = w.c.get("/operator/session").json()
    assert s == s | {"enabled": True, "authenticated": True, "operator": "operator", "mode": "live"}
    assert VOICE not in json.dumps(s) and cookie.split(";")[0].split("=", 1)[1] not in json.dumps(s)


def test_invalid_operator_credential_gets_no_session(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    w = LiveWorld(tmp_path)
    for bad in (ALEXA, TOKEN, "", "x" * 60):
        r = w.login(bad)
        assert r.status_code == 303 and r.headers["location"] == "/?operator=failed" and "set-cookie" not in r.headers
    assert w.c.get("/operator/session").json()["authenticated"] is False
    assert w.prepare().status_code == 401
    assert w.aws.writes == []


def test_the_alexa_and_mcp_bearer_tokens_never_authorize_voice_tagging(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    w = LiveWorld(tmp_path)
    for token in (ALEXA, TOKEN, VOICE):
        r = w.c.post("/operator/voice-tagging/prepare", headers=H | {"authorization": f"Bearer {token}"},
                     json={"service": "lambda", "region": "ap-south-1", "match": "contains", "text": "worker",
                           "tags": {"Team": "x"}, "channel": "voice"})
        assert r.status_code == 401
    assert w.aws.writes == []


def test_expired_session_is_refused_and_voids_its_confirmation(live):
    challenge = ready(live)["challenge"]["id"]
    live.clock.now += timedelta(minutes=31)
    assert live.c.get("/operator/session").json()["authenticated"] is False
    assert live.confirm(challenge).status_code == 401
    assert live.aws.writes == []


def test_logout_voids_pending_confirmations(live):
    challenge = ready(live)["challenge"]["id"]
    assert live.c.post("/operator/logout", headers=H).status_code == 200
    assert live.app.service._challenges[challenge].state == "CANCELLED"
    live.login()
    r = live.confirm(challenge)  # a new session can't even see the old session's challenge
    assert r.status_code == 404 and r.json()["error"] == "NO_CHALLENGE"
    assert live.aws.writes == []


def test_requests_need_the_voice_header_channel_and_an_allowed_origin(live):
    body = {"service": "lambda", "region": "ap-south-1", "match": "contains", "text": "worker", "tags": {"T": "v"}}
    assert live.c.post("/operator/voice-tagging/prepare", json=body | {"channel": "voice"}).status_code == 403
    assert live.c.post("/operator/voice-tagging/prepare", json=body, headers=H).status_code == 403  # typed chat
    assert live.c.post("/operator/voice-tagging/prepare", json=body | {"channel": "voice"},
                       headers=H | {"origin": "https://evil.example"}).status_code == 403
    assert live.c.post("/operator/voice-tagging/prepare", json=body | {"channel": "voice"},
                       headers=H | {"origin": "http://127.0.0.1:5173"}).status_code == 200
    assert live.c.post("/operator/voice-tagging/prepare", json=body | {"channel": "voice"},
                       headers=H | {"origin": "null"}).status_code == 403
    assert live.c.post("/operator/voice-tagging/prepare", json=body | {"channel": "voice"},
                       headers=H | {"origin": "null", "sec-fetch-site": "same-origin"}).status_code == 200
    assert live.c.post("/operator/voice-tagging/prepare", json=body | {"channel": "voice"},
                       headers=H | {"sec-fetch-site": "cross-site"}).status_code == 403


def test_feature_disabled_by_kill_switch_refuses_everything(live):
    challenge = ready(live)["challenge"]["id"]
    (Path(live.cfg.voice.kill_file)).write_text("off")
    assert live.prepare().json()["error"] == "DISABLED"
    assert live.confirm(challenge).json()["error"] == "DISABLED"
    assert live.c.get("/operator/session").json()["kill_switch"] is True
    assert live.aws.writes == []


def test_kill_endpoint_engages_the_switch(live):
    challenge = ready(live)["challenge"]["id"]
    assert live.c.post("/operator/voice-tagging/kill", json={}, headers=H).status_code == 200
    assert Path(live.cfg.voice.kill_file).exists()
    assert live.confirm(challenge).status_code in (403, 409)
    assert live.aws.writes == []


def test_wrong_aws_account_is_refused(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    w = LiveWorld(tmp_path, account="999988887777", caller="arn:aws:sts::999988887777:assumed-role/Operator/s1")
    w.login()
    r = w.prepare()
    assert r.status_code == 403 and r.json()["error"] == "ACCOUNT_NOT_ALLOWED"
    assert w.aws.writes == []


def test_unverifiable_caller_identity_is_refused(live):
    def boom():
        raise RuntimeError("ExpiredToken")
    live.sts.get_caller_identity = boom
    r = live.prepare()
    assert r.status_code == 403 and r.json()["error"] == "IDENTITY_UNVERIFIED"


def test_missing_execution_role_or_permission_writes_nothing(live):
    challenge = ready(live)["challenge"]["id"]

    def denied(acct):
        raise _err("AccessDenied", 403)
    live.tag_account = denied
    r = live.confirm(challenge)
    assert r.status_code == 403 and r.json()["error"] == "EXECUTION_ROLE"
    assert live.aws.writes == []


def test_missing_tagging_permission_is_reported_per_resource(live):
    out = ready(live)
    arns = [a for a, r in live.aws.res.items() if r["name"] in ("report-worker", "worker") and r["region"] == "ap-south-1"]
    for arn in arns:
        live.aws.fail_write[arn] = ["AccessDeniedException"]
    result = live.confirm(out["challenge"]["id"]).json()
    assert {r["category"] for r in result["results"]} == {"failed"}
    assert all(r["error"] == "ACCESS_DENIED" for r in result["results"])
    assert "Failed 2" in result["speech"] and "Tagged" not in result["speech"]


def test_operator_without_execute_permission_is_refused(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    w = LiveWorld(tmp_path, grants={"create": [OPERATOR_IAM], "approve": [OPERATOR_IAM]})
    w.login()
    r = w.confirm(ready(w)["challenge"]["id"])
    assert r.status_code == 403 and r.json()["error"] == "NOT_AUTHORIZED"
    assert w.aws.writes == []


# ================================================================ spoken confirmation

@pytest.mark.parametrize("said", ["yes", "Yes.", "proceed", "Yes, proceed", "go ahead", "confirm", "yes please"])
def test_server_accepts_only_exact_affirmatives(said):
    assert read_confirmation(said) == "yes"


@pytest.mark.parametrize("said", ["sounds good", "that's fine", "", "   ", "ye", "yes but wait", "yes, later",
                                  "maybe", "ok", "sure", "alright", None, "yes " * 60])
def test_ambiguous_silent_and_partial_transcripts_are_not_confirmations(said):
    assert read_confirmation(said) == "unclear"


def test_overwrite_ack_is_distinct_from_yes():
    assert is_overwrite_ack("I confirm the overwrite") and not is_overwrite_ack("yes") and not is_overwrite_ack("proceed")


def test_prepare_reports_exact_names_and_count_and_writes_nothing(live):
    out = ready(live)
    assert sorted(r["name"] for r in out["resources"]) == ["report-worker", "worker"]
    assert out["region"] == "ap-south-1" and out["account_ref"] == "…9012" and ACCT not in json.dumps(out)
    assert "2 Lambda functions would get Team equals mukesh" in out["speech"]
    assert live.aws.writes == []


def test_explicit_yes_executes_bulk_lambda_with_read_after_write_verification(live):
    out = ready(live)
    result = live.confirm(out["challenge"]["id"], "Yes.").json()
    assert result["status"] == "EXECUTED" and result["outcome"] == "SUCCEEDED"
    assert {r["name"]: r["status"] for r in result["results"]} == {"report-worker": "APPLIED_VERIFIED",
                                                                    "worker": "APPLIED_VERIFIED"}
    assert "Tagged and verified 2" in result["speech"]
    written = {a for a, _ in live.aws.writes}
    assert len(written) == 2 and all(live.aws.res[a]["tags"]["Team"] == "mukesh" for a in written)
    # The us-east-1 "worker" is another region: never touched.
    assert all(live.aws.res[a]["region"] == "ap-south-1" for a in written)


@pytest.mark.parametrize("said", ["sounds good", "that's fine", "", "ye"])
def test_ambiguous_answer_leaves_the_challenge_pending_and_writes_nothing(live, said):
    challenge = ready(live)["challenge"]["id"]
    r = live.confirm(challenge, said)
    assert r.status_code == 400 and r.json()["error"] == "NOT_CONFIRMED"
    assert live.aws.writes == []
    assert live.confirm(challenge, "yes").json()["status"] == "EXECUTED"  # still the same, unexpired challenge


def test_explicit_no_cancels(live):
    challenge = ready(live)["challenge"]["id"]
    assert live.confirm(challenge, "no").json()["status"] == "CANCELLED"
    assert live.confirm(challenge, "yes").json()["error"] == "CHALLENGE_CANCELLED"
    assert live.aws.writes == []


def test_cancel_and_disconnect_void_the_challenge(live):
    challenge = ready(live)["challenge"]["id"]
    assert live.c.post("/operator/voice-tagging/cancel", json={"challenge_id": challenge}, headers=H).json() == {
        "status": "CANCELLED"}
    assert live.confirm(challenge).json()["error"] == "CHALLENGE_CANCELLED"
    assert live.aws.writes == []


def test_expired_confirmation_never_executes(live):
    challenge = ready(live)["challenge"]["id"]
    live.clock.now += timedelta(minutes=3)
    assert live.confirm(challenge).json()["error"] == "CHALLENGE_EXPIRED"
    assert live.aws.writes == []


def test_duplicate_transcript_and_replay_never_execute_twice(live):
    challenge = ready(live)["challenge"]["id"]
    first = live.confirm(challenge).json()
    writes = list(live.aws.writes)
    again = live.confirm(challenge).json()
    assert again["replayed"] is True and again["execution_id"] == first["execution_id"]
    assert live.aws.writes == writes and len(writes) == 2


def test_a_new_prepare_replaces_the_earlier_confirmation(live):
    old = ready(live)["challenge"]["id"]
    ready(live, tags={"Team": "dev"})
    assert live.confirm(old).json()["error"] == "CHALLENGE_CANCELLED"
    assert live.aws.writes == []


def test_a_challenge_from_another_session_is_unknown(live):
    challenge = ready(live)["challenge"]["id"]
    other = TestClient(live.app, base_url=BASE)
    other.post("/operator/login", data={"credential": VOICE}, follow_redirects=False)
    r = other.post("/operator/voice-tagging/confirm", headers=H,
                   json={"challenge_id": challenge, "transcript": "yes", "channel": "voice"})
    assert r.status_code == 404 and live.aws.writes == []


# ================================================================ execution

def by_name(w, name, rtype):
    return next(a for a, r in w.aws.res.items() if r["name"] == name and r["type"] == rtype
                and r["region"] == "ap-south-1")


@pytest.mark.parametrize("service,name,rtype", [
    ("lambda", "image-resizer", "AWS::Lambda::Function"),
    ("dynamodb", "sessions", "AWS::DynamoDB::Table"),
    ("s3", "acme-logs", "AWS::S3::Bucket"),
    ("ec2", "build-agent", "AWS::EC2::Instance"),  # an instance is named by its Name tag
])
def test_single_resource_of_each_supported_type(live, service, name, rtype):
    out = ready(live, service=service, match="exact", text=name)
    assert [r["name"] for r in out["resources"]] == [name]
    if service == "ec2":
        name = "i-0123456789abcdef0"
    result = live.confirm(out["challenge"]["id"]).json()
    assert [r["status"] for r in result["results"]] == ["APPLIED_VERIFIED"]
    assert live.aws.res[by_name(live, name, rtype)]["tags"]["Team"] == "mukesh"


def test_starts_with_and_ends_with_filters(live):
    assert [r["name"] for r in ready(live, match="starts_with", text="report")["resources"]] == ["report-worker"]
    assert sorted(r["name"] for r in ready(live, match="ends_with", text="er")["resources"]) == [
        "image-resizer", "report-worker", "worker"]


def test_existing_tag_with_the_same_value_is_already_correct(live):
    out = live.prepare(match="exact", text="report-worker", tags={"Project": "CloudGuard"}).json()
    assert out["status"] == "NO_CHANGES" and out["challenge"] is None
    assert "already has that tag" in out["speech"] and live.aws.writes == []


def test_existing_tag_with_a_different_value_is_a_conflict_until_an_acknowledged_overwrite(live):
    conflict = live.prepare(match="exact", text="billing-api", tags={"Owner": "mukesh"}).json()
    assert conflict["status"] == "NO_CHANGES" and "billing-api has Owner equals payments-team" in conflict["speech"]
    out = ready(live, match="exact", text="billing-api", tags={"Owner": "mukesh"}, overwrite=True)
    assert out["requires_overwrite_ack"] is True
    assert out["overwrites"] == [{"name": "billing-api", "key": "Owner", "action": "OVERWRITE",
                                  "old_value": "payments-team", "new_value": "mukesh"}]
    assert "from payments-team to mukesh" in out["speech"]
    challenge = out["challenge"]["id"]
    assert live.confirm(challenge, "yes").json()["error"] == "OVERWRITE_NOT_ACKNOWLEDGED"
    assert live.confirm(challenge, "yes", overwrite_transcript="yes").json()["error"] == "OVERWRITE_NOT_ACKNOWLEDGED"
    assert live.aws.writes == []
    result = live.confirm(challenge, "yes", overwrite_transcript="I confirm the overwrite").json()
    assert [r["status"] for r in result["results"]] == ["APPLIED_VERIFIED"]
    assert live.aws.res[by_name(live, "billing-api", "AWS::Lambda::Function")]["tags"]["Owner"] == "mukesh"


@pytest.mark.parametrize("tags", [{"aws:owner": "x"}, {"cloudguard:owner": "x"}, {"Team": "x" * 257},
                                  {"k" * 129: "x"}, {" Team": "x"}])
def test_invalid_tag_keys_and_values_are_refused(live, tags):
    r = live.prepare(tags=tags)
    assert r.status_code == 400
    assert live.aws.writes == []


def test_resource_disappearing_before_execution_invalidates_the_confirmation(live):
    challenge = ready(live)["challenge"]["id"]
    del live.aws.res[by_name(live, "worker", "AWS::Lambda::Function")]
    r = live.confirm(challenge)
    assert r.status_code == 409 and r.json()["error"] == "PROPOSAL_CHANGED"
    assert live.aws.writes == []
    assert live.confirm(challenge).json()["error"] == "CHALLENGE_INVALIDATED"  # never retried with the old yes


def test_a_tag_changed_after_confirmation_invalidates_it(live):
    challenge = ready(live)["challenge"]["id"]
    live.aws.res[by_name(live, "worker", "AWS::Lambda::Function")]["tags"]["Team"] = "someone-else"
    assert live.confirm(challenge).json()["error"] == "PROPOSAL_CHANGED"
    assert live.aws.writes == []


def test_partial_failure_is_reported_per_resource(live):
    out = ready(live)
    live.aws.fail_write[by_name(live, "worker", "AWS::Lambda::Function")] = ["ValidationException"]
    result = live.confirm(out["challenge"]["id"]).json()
    assert result["outcome"] == "PARTIAL"
    assert {r["name"]: r["category"] for r in result["results"]} == {"report-worker": "tagged", "worker": "failed"}
    assert "Tagged and verified 1: report-worker" in result["speech"] and "Failed 1: worker" in result["speech"]


def test_a_write_that_does_not_verify_is_never_reported_as_success(live):
    out = ready(live)
    live.aws.silent.add(by_name(live, "worker", "AWS::Lambda::Function"))  # AWS says OK but nothing changed
    result = live.confirm(out["challenge"]["id"]).json()
    worker = next(r for r in result["results"] if r["name"] == "worker")
    assert worker["status"] == "FAILED_VERIFICATION" and worker["category"] == "failed"


def test_execution_records_a_complete_audit_without_secrets(live):
    live.confirm(ready(live)["challenge"]["id"])
    line = (Path(live.cfg.voice.store) / "voice-tagging-audit.jsonl").read_text(encoding="utf-8").strip()
    entry = json.loads(line)
    assert entry["operator"] == "operator" and entry["account_ref"] == "…9012" and entry["service"] == "lambda"
    assert entry["region"] == "ap-south-1" and entry["tags"] == {"Team": "mukesh"}
    assert entry["resources"] == ["report-worker", "worker"] and entry["proposal_hash"].startswith("sha256:")
    assert entry["confirmed_at"] and entry["executed_at"] and len(entry["results"]) == 2
    assert entry["execution_identity"].endswith(":role/CloudGuardTaggerRole")
    for secret in (VOICE, TOKEN, ALEXA, ACCT):
        assert secret not in line


def test_kill_switch_stops_a_running_execution_before_its_next_write(live):
    out = ready(live)
    kill = Path(live.cfg.voice.kill_file)
    real = live.aws.write

    def write_then_kill(arn, tags):
        kill.write_text("off")
        return real(arn, tags)
    live.aws.write = write_then_kill
    result = live.confirm(out["challenge"]["id"]).json()
    assert len(live.aws.writes) == 1
    assert sorted(r["category"] for r in result["results"]) == ["skipped", "tagged"]


def test_account_mismatch_between_caller_and_tagging_role_writes_nothing(live):
    challenge = ready(live)["challenge"]["id"]
    live.tag_account = lambda acct: LiveTagAccount(session=None, account_id="999988887777",
                                                   client_factory=live.aws.factory)
    r = live.confirm(challenge)
    assert r.status_code == 403 and r.json()["error"] == "ACCOUNT_MISMATCH"
    assert live.aws.writes == []


# ================================================================ separation of duties is unchanged elsewhere

def test_without_a_waiver_the_same_person_still_cannot_approve_their_own_live_proposal(live):
    svc = DecisionService(live.app.service.proposals, live.app.service.authz,
                          live.app.service.backend.identity())
    assert svc.waived(ACCT) is False
    waiver = PersonalAccountWaiver(account_id=ACCT, mode=OperatingMode.LIVE)
    assert waiver.covers(ACCT, OperatingMode.LIVE) and not waiver.covers("999988887777", OperatingMode.LIVE)
    assert not waiver.covers(ACCT, OperatingMode.FIXTURE)


# ================================================================ fixture mode (simulated account, no AWS)

def test_fixture_mode_tags_the_simulated_account_end_to_end(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    cfg = DeployConfig.from_env(env(tmp_path))
    with TestClient(build_app(cfg), base_url=BASE) as c:
        c.post("/operator/login", data={"credential": VOICE}, follow_redirects=False)
        assert c.get("/operator/session").json()["mode"] == "fixture"
        out = c.post("/operator/voice-tagging/prepare", headers=H, json={
            "service": "lambda", "region": "ap-south-1", "match": "contains", "text": "legacy",
            "tags": {"owner": "mukesh"}, "channel": "voice"}).json()
        assert out["mode"] == "fixture" and [r["name"] for r in out["resources"]] == ["legacy-report-generator"]
        result = c.post("/operator/voice-tagging/confirm", headers=H, json={
            "challenge_id": out["challenge"]["id"], "transcript": "yes", "channel": "voice"}).json()
        assert result["mode"] == "fixture" and [r["status"] for r in result["results"]] == ["APPLIED_VERIFIED"]
        # The general Alexa token's MCP surface is unchanged: still read-only, no voice-tagging tool.
        tools = c.post("/mcp", headers={"authorization": f"Bearer {ALEXA}", "accept": "application/json, text/event-stream",
                                        "content-type": "application/json", "mcp-protocol-version": "2025-11-25"},
                       json={"jsonrpc": "2.0", "id": 1, "method": "tools/list", "params": {}}).text
        names = {t["name"] for t in json.loads(tools)["result"]["tools"]}
        assert len(names) == 14 and not any(w in n for n in names for w in ("voice", "approve", "execute", "save"))


def test_live_backend_assumes_the_tagging_role_with_a_trust_policy_session_name_and_checks_its_account():
    calls = {}

    class Sts:
        def assume_role(self, **kw):
            calls.update(kw)
            return {"Credentials": {"AccessKeyId": "AKIATEST", "SecretAccessKey": "s", "SessionToken": "t"}}

    class Session:
        region_name = "ap-south-1"

    import boto3

    class Writer:
        def __init__(self, **kw):
            pass

        def client(self, *a, **kw):
            return FakeSts(f"arn:aws:sts::{ACCT}:assumed-role/CloudGuardTaggerRole/x", ACCT)

    real = boto3.Session
    boto3.Session = Writer
    try:
        account = LiveBackend(Session(), TAGGER, sts_client=Sts()).tag_account(ACCT)
    finally:
        boto3.Session = real
    assert calls["RoleArn"] == TAGGER and calls["RoleSessionName"].startswith("cg-proposal-voice-")
    assert calls["DurationSeconds"] == 900 and account.account_id == ACCT
