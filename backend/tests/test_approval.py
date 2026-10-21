"""Checkpoint 4.2: identity, versioned authorisation and server-side approval decisions.

Decision tests run against the in-memory, on-disk and DynamoDB (moto) repositories. LIVE identities
come from StsCallerIdentityProvider with a fake STS client; all accounts and ARNs are synthetic.
"""

import inspect
import json
import threading
from datetime import UTC, datetime, timedelta
from pathlib import Path

import boto3
import pytest
from moto import mock_aws
from pydantic import ValidationError

from cloudguard.governance.__main__ import main
from cloudguard.governance.custom_tagging import CustomTaggingEngine, RuleBasedParser
from cloudguard.governance.custom_tagging.models import TaggingRequest
from cloudguard.governance.discovery import FixtureSource
from cloudguard.governance.policy import DEFAULT_POLICY_DOCUMENT, parse_policy
from cloudguard.governance.policy_store import LocalPolicyStore, PolicyManager
from cloudguard.governance.remediation import approval as approval_module
from cloudguard.governance.remediation.approval import (
    APPROVAL_TTL,
    AuthorizationError,
    DecisionNotAllowedError,
    DecisionService,
    new_nonce,
)
from cloudguard.governance.remediation.audit import reconstruct_head, verify_decisions
from cloudguard.governance.remediation.authz import (
    BOOTSTRAP_ENV,
    AuthzConflictError,
    AuthzManager,
    AuthzValidationError,
    DynamoAuthzStore,
    LocalAuthzStore,
    parse_authz,
)
from authz_helpers import edit_authz, fake_user_id, seeded_authz, with_seed_admins
from cloudguard.governance.remediation.identity import (
    DevelopmentIdentityProvider,
    IdentityError,
    Principal,
    PrincipalKind,
    StsCallerIdentityProvider,
    principal_from_caller_arn,
    require_identity_for_mode,
)
from cloudguard.governance.remediation.models import (
    Actor,
    ActorType,
    DecisionType,
    EventType,
    OperatingMode,
    Permission,
    ProposalIntegrityError,
    ProposalState,
)
from cloudguard.governance.remediation.repository import (
    DynamoProposalRepository,
    IdempotencyConflictError,
    LocalProposalRepository,
    NonceReplayError,
    ProposalManager,
    TransitionConflictError,
)

def pfc(arn, account, now):
    """principal_from_caller_arn with the UserId STS would return for `arn` (M3)."""
    return principal_from_caller_arn(arn, account, now, fake_user_id(arn, account))


ROOT = Path(__file__).resolve().parents[2]
FIXTURE = ROOT / "backend" / "fixtures" / "custom-tagging-demo.json"
AUTHZ_FIXTURE = ROOT / "backend" / "fixtures" / "authz-demo.json"
NOW = datetime(2026, 10, 14, 6, 30, tzinfo=UTC)
ACCT = "123456789012"
OTHER = "210987654321"
TABLE = "cloudguard-governance-test"

ROLE_REQ = f"arn:aws:iam::{ACCT}:role/Requester"
ROLE_APP = f"arn:aws:iam::{ACCT}:role/Approver"
USER_ALICE = f"arn:aws:iam::{ACCT}:user/alice"
LIVE_CONFIG = {"accounts": {ACCT: {
    "create": [ROLE_REQ, USER_ALICE],
    "approve": [ROLE_APP, USER_ALICE],
    "reject": [ROLE_APP, ROLE_REQ, USER_ALICE],
}}}
DEV_CONFIG = json.loads(AUTHZ_FIXTURE.read_text(encoding="utf-8"))


@pytest.fixture(autouse=True)
def _isolated_cwd(tmp_path, monkeypatch):
    """CLI defaults (.cloudguard/...) are relative to the working directory: keep each test isolated."""
    monkeypatch.chdir(tmp_path)


class Clock:
    def __init__(self, now=NOW + timedelta(minutes=1)):
        self.now = now

    def __call__(self):
        return self.now


class FakeSts:
    def __init__(self, arn, account=ACCT, fail=False):
        self.arn, self.account, self.fail = arn, account, fail

    def get_caller_identity(self):
        if self.fail:
            raise RuntimeError("ExpiredToken")
        return {"Arn": self.arn, "Account": self.account, "UserId": fake_user_id(self.arn, self.account)}


def sts(arn, clock, account=ACCT):
    return StsCallerIdentityProvider(FakeSts(arn, account), clock=clock)


def assumed(role, session="s1", account=ACCT):
    return f"arn:aws:sts::{account}:assumed-role/{role}/{session}"


@pytest.fixture(scope="module")
def policies():
    pm = PolicyManager(LocalPolicyStore())
    pm.ensure_default()
    return pm


def build(policies, text="Update Owner of all Lambda functions to platform-team", ttl=timedelta(minutes=15)):
    return CustomTaggingEngine(FixtureSource(FIXTURE), policies.get_active(), clock=lambda: NOW,
                               proposal_ttl=ttl).prepare(text).proposal


@pytest.fixture(params=["memory", "disk", "dynamodb"])
def repo(request, tmp_path):
    if request.param == "memory":
        yield LocalProposalRepository()
    elif request.param == "disk":
        yield LocalProposalRepository(tmp_path / "proposals")
    else:
        with mock_aws():
            client = boto3.client("dynamodb", region_name="ap-south-1", aws_access_key_id="t", aws_secret_access_key="t")
            client.create_table(TableName=TABLE, BillingMode="PAY_PER_REQUEST",
                                AttributeDefinitions=[{"AttributeName": "PK", "AttributeType": "S"},
                                                      {"AttributeName": "SK", "AttributeType": "S"}],
                                KeySchema=[{"AttributeName": "PK", "KeyType": "HASH"},
                                           {"AttributeName": "SK", "KeyType": "RANGE"}])
            yield DynamoProposalRepository(client, TABLE)


class Env:
    """A proposal store + allow-list + clock shared by several identities."""

    def __init__(self, repo, policies, config, clock=None):
        self.clock = clock or Clock()
        self.proposals = ProposalManager(repo, clock=self.clock, policy_manager=policies)
        self.authz = seeded_authz(config)

    def svc(self, provider):
        return DecisionService(self.proposals, self.authz, provider, clock=self.clock)

    def dev(self, name):
        return self.svc(DevelopmentIdentityProvider(name, clock=self.clock))

    def live(self, arn, account=ACCT):
        return self.svc(sts(arn, self.clock, account))


def approve(svc, p, **kw):
    kw.setdefault("acknowledge_overwrites", p.requires_overwrite_confirmation)
    return svc.approve(p.proposal_id, content_hash=kw.pop("content_hash", p.content_hash), nonce=kw.pop("nonce", new_nonce()),
                       idempotency_key=kw.pop("idempotency_key", f"key-{new_nonce()[:20]}"), **kw)


def reject(svc, p, **kw):
    return svc.reject(p.proposal_id, content_hash=p.content_hash, nonce=kw.pop("nonce", new_nonce()),
                      idempotency_key=kw.pop("idempotency_key", f"key-{new_nonce()[:20]}"), reason=kw.pop("reason", "no"))


# ================================================================ identity

def test_canonical_principals_from_sts_arns():
    user = pfc(f"arn:aws:iam::{ACCT}:user/team/ops/alice", ACCT, NOW)
    assert (user.kind, user.canonical_id, user.authenticated) == (PrincipalKind.IAM_USER, USER_ALICE, True)
    role = pfc(assumed("Approver", "laptop-session"), ACCT, NOW)
    assert (role.kind, role.canonical_id, role.session_name) == (PrincipalKind.ASSUMED_ROLE, ROLE_APP, "laptop-session")
    root = pfc(f"arn:aws:iam::{ACCT}:root", ACCT, NOW)
    assert root.kind == PrincipalKind.ROOT and root.mode == OperatingMode.LIVE
    assert role.as_actor() == Actor(actor_type=ActorType.AWS_PRINCIPAL, id=ROLE_APP) and role.as_actor().authenticated


@pytest.mark.parametrize("arn", [f"arn:aws:sts::{ACCT}:federated-user/bob", f"arn:aws:iam::{OTHER}:user/alice",
                                 "dev:alice", "not-an-arn", f"arn:aws:iam::{ACCT}:group/admins"])
def test_unsupported_or_mismatched_caller_arns_are_rejected(arn):
    with pytest.raises(IdentityError):
        pfc(arn, ACCT, NOW)


def test_sts_failures_mean_unauthenticated():
    with pytest.raises(IdentityError, match="could not verify"):
        StsCallerIdentityProvider(FakeSts(USER_ALICE, fail=True)).current()


def test_development_identity_is_never_authenticated():
    p = DevelopmentIdentityProvider("alice", clock=Clock()).current()
    assert (p.authenticated, p.account_id, p.canonical_id, p.mode) == (False, None, "dev:alice", OperatingMode.FIXTURE)
    assert not p.as_actor().authenticated
    for bad in ("Alice", "a b", "", "x" * 64, "../x"):
        with pytest.raises(IdentityError):
            DevelopmentIdentityProvider(bad)
    with pytest.raises(ValidationError):  # cannot forge an authenticated development principal
        Principal(kind=PrincipalKind.DEVELOPMENT, canonical_id="dev:x", caller_arn=None, account_id=None,
                  authenticated=True, authn_method="development", authenticated_at=NOW)
    with pytest.raises(ValidationError):  # nor an AWS principal without STS evidence
        Principal(kind=PrincipalKind.IAM_USER, canonical_id=USER_ALICE, caller_arn=None, account_id=ACCT,
                  authenticated=True, authn_method="sts:GetCallerIdentity", authenticated_at=NOW)


def test_mode_identity_rules():
    dev = DevelopmentIdentityProvider("alice").current()
    aws = pfc(USER_ALICE, ACCT, NOW)
    with pytest.raises(IdentityError, match="development identities are refused"):
        require_identity_for_mode(dev, OperatingMode.LIVE)
    with pytest.raises(IdentityError):
        require_identity_for_mode(aws, OperatingMode.FIXTURE)


def test_labels_are_never_authenticated():
    assert not Actor(actor_type=ActorType.UNAUTHENTICATED, id="arn:aws:iam::123456789012:user/alice").authenticated
    assert not Actor(actor_type=ActorType.DEVELOPMENT, id="dev:alice").authenticated
    assert not Actor(actor_type=ActorType.SYSTEM, id="x").authenticated


# ================================================================ allow-list configuration

@pytest.mark.parametrize(("config", "fragment"), [
    ({"accounts": {"12345": {}}}, "12 digits"),
    ({"accounts": {ACCT: {"approve": [f"arn:aws:iam::{ACCT}:role/*"]}}}, "no wildcards"),
    ({"accounts": {ACCT: {"approve": ["*"]}}}, "no wildcards"),
    ({"accounts": {ACCT: {"approve": [f"arn:aws:iam::{OTHER}:role/Approver"]}}}, "another account"),
    ({"accounts": {ACCT: {"approve": [ROLE_APP, f"arn:aws:iam::{ACCT}:role/team/Approver"]}}}, "duplicate"),
    ({"accounts": {ACCT: {"approve": ["dev:Bad Name"]}}}, "unsupported entry"),
    ({"accounts": {ACCT: {"delete": [ROLE_APP]}}}, "Extra inputs"),
    ({"schema_version": "cloudguard.authz/v0", "accounts": {}}, "unsupported authz schema"),
])
def test_invalid_allow_lists_are_rejected(config, fragment):
    with pytest.raises(AuthzValidationError) as exc:
        parse_authz(config)
    assert fragment in " | ".join(exc.value.errors)


@pytest.fixture(params=["local", "dynamodb"])
def authz_store(request):
    if request.param == "local":
        yield LocalAuthzStore()
        return
    with mock_aws():
        client = boto3.client("dynamodb", region_name="ap-south-1", aws_access_key_id="t", aws_secret_access_key="t")
        client.create_table(TableName=TABLE, BillingMode="PAY_PER_REQUEST",
                            AttributeDefinitions=[{"AttributeName": "PK", "AttributeType": "S"},
                                                  {"AttributeName": "SK", "AttributeType": "S"}],
                            KeySchema=[{"AttributeName": "PK", "KeyType": "HASH"}, {"AttributeName": "SK", "KeyType": "RANGE"}])
        yield DynamoAuthzStore(client, TABLE)


def test_allow_list_versioning(authz_store):
    m = seeded_authz(LIVE_CONFIG, store=authz_store)
    changed = json.loads(json.dumps(LIVE_CONFIG))
    changed["accounts"][ACCT]["approve"].remove(USER_ALICE)
    v2 = edit_authz(m, changed, OperatingMode.LIVE, note="remove alice")
    assert (v2.version, m.get_active().version) == (2, 2)
    assert [v.version for v in m.history()] == [1, 2]
    assert m.get_version(1).config == parse_authz(with_seed_admins(LIVE_CONFIG))
    admin = m.get_version(1).change.principal
    with pytest.raises(AuthzConflictError):  # stale expectation
        m.save(parse_authz(with_seed_admins(LIVE_CONFIG)), expected_version=1,
               identity=sts(assumed("AuthzSeedAdmin"), Clock()))
    with pytest.raises(ValueError, match="unchanged"):
        edit_authz(m, changed, OperatingMode.LIVE)
    assert m.get_active().version == 2 and admin.id.endswith("role/AuthzSeedAdmin")


def test_evaluation_rules():
    empty = AuthzManager(LocalAuthzStore())
    alice = pfc(f"arn:aws:iam::{ACCT}:user/team/alice", ACCT, NOW)
    approver = pfc(assumed("Approver", "any-session"), ACCT, NOW)
    requester = pfc(assumed("Requester"), ACCT, NOW)
    dev = DevelopmentIdentityProvider("bob").current()
    assert not empty.evaluate(alice, ACCT, Permission.APPROVE, OperatingMode.LIVE).allowed  # no config: deny all
    m = seeded_authz({"accounts": {ACCT: {k: v + DEV_CONFIG["accounts"][ACCT][k]
                                          for k, v in LIVE_CONFIG["accounts"][ACCT].items()}}})
    ok = m.evaluate(approver, ACCT, Permission.APPROVE, OperatingMode.LIVE)
    assert ok.allowed and ok.basis.matched_entry == ROLE_APP and ok.basis.ref.version == m.get_active().version
    assert m.evaluate(alice, ACCT, Permission.APPROVE, OperatingMode.LIVE).allowed  # path ignored
    assert not m.evaluate(requester, ACCT, Permission.APPROVE, OperatingMode.LIVE).allowed
    assert not m.evaluate(approver, OTHER, Permission.APPROVE, OperatingMode.LIVE).allowed
    assert not m.evaluate(dev, ACCT, Permission.APPROVE, OperatingMode.LIVE).allowed   # dev never LIVE
    assert m.evaluate(dev, ACCT, Permission.APPROVE, OperatingMode.FIXTURE).allowed
    assert not m.evaluate(approver, ACCT, Permission.APPROVE, OperatingMode.FIXTURE).allowed
    other_acct = pfc(f"arn:aws:iam::{OTHER}:user/alice", OTHER, NOW)
    assert "is not the target account" in m.evaluate(other_acct, ACCT, Permission.APPROVE, OperatingMode.LIVE).reason


def test_nothing_is_hardcoded_in_authorisation_code():
    import cloudguard.governance.remediation.approval as a
    import cloudguard.governance.remediation.authz as z
    import cloudguard.governance.remediation.identity as i
    import re
    for module in (a, z, i):
        source = inspect.getsource(module)
        assert not re.search(r"\b\d{12}\b", source), module.__name__
        assert "dev:alice" not in source and "dev:bob" not in source


# ================================================================ approve / reject / revoke

def test_fixture_approval_happy_path(repo, policies):
    env = Env(repo, policies, DEV_CONFIG)
    p = build(policies)
    env.dev("alice").create_proposal(p, idempotency_key="key-create01")
    result = approve(env.dev("bob"), p, reason="looks right")
    d, head = result.decision, result.head
    assert result.created and head.state == ProposalState.APPROVED and head.approval_id == d.decision_id
    assert d.decision == DecisionType.APPROVE and d.content_hash == p.content_hash and d.mode == OperatingMode.FIXTURE
    assert d.principal == Actor(actor_type=ActorType.DEVELOPMENT, id="dev:bob") and d.principal_arn is None
    assert d.authorization.matched_entry == "dev:bob" and d.authorization.ref.version == 1
    assert d.overwrite_acknowledged and d.approval_expires_at == p.expires_at  # proposal expiry came first
    ev = env.proposals.history(p.proposal_id)[-1]
    assert ev.event_type == EventType.APPROVED and ev.details["decision_id"] == d.decision_id
    assert ev.details["authz_version"] == 1 and ev.actor == d.principal


def test_fixture_mode_allows_self_approval(repo, policies):
    cfg = {"accounts": {ACCT: {"create": ["dev:alice"], "approve": ["dev:alice"], "reject": []}}}
    env = Env(repo, policies, cfg)
    p = build(policies)
    env.dev("alice").create_proposal(p, idempotency_key="key-selffix")
    assert approve(env.dev("alice"), p).head.state == ProposalState.APPROVED


def test_approval_ttl_is_fifteen_minutes_when_proposal_lives_longer(repo, policies):
    env = Env(repo, policies, DEV_CONFIG)
    p = build(policies, ttl=timedelta(hours=1))
    env.dev("alice").create_proposal(p, idempotency_key="key-longttl")
    d = approve(env.dev("bob"), p).decision
    assert d.approval_expires_at == env.clock.now + APPROVAL_TTL < p.expires_at


@pytest.mark.parametrize(("who", "action"), [("carol", "create"), ("alice", "approve"), ("dave", "reject")])
def test_unauthorised_principals_are_denied(repo, policies, who, action):
    env = Env(repo, policies, DEV_CONFIG)
    p = build(policies)
    if action == "create":
        with pytest.raises(AuthorizationError, match="lacks 'create'"):
            env.dev(who).create_proposal(p, idempotency_key="key-unauth01")
        assert repo.get_stored(p.proposal_id) is None
        return
    env.dev("alice").create_proposal(p, idempotency_key="key-unauth02")
    with pytest.raises(AuthorizationError):
        (approve if action == "approve" else reject)(env.dev(who), p)
    assert env.proposals.head(p.proposal_id).state == ProposalState.PENDING


def test_overwrite_acknowledgement_is_required_and_recorded(repo, policies):
    env = Env(repo, policies, DEV_CONFIG)
    p = build(policies)
    assert p.requires_overwrite_confirmation
    env.dev("alice").create_proposal(p, idempotency_key="key-ack00001")
    with pytest.raises(DecisionNotAllowedError, match="overwrite acknowledgement"):
        approve(env.dev("bob"), p, acknowledge_overwrites=False)
    assert approve(env.dev("bob"), p, acknowledge_overwrites=True).decision.overwrite_acknowledged
    plain = build(policies, "Tag all Lambda functions with Project=CloudGuard")
    env.dev("alice").create_proposal(plain, idempotency_key="key-ack00002")
    assert not approve(env.dev("bob"), plain, acknowledge_overwrites=False).decision.overwrite_acknowledged


def test_content_hash_must_match(repo, policies):
    env = Env(repo, policies, DEV_CONFIG)
    p = build(policies)
    env.dev("alice").create_proposal(p, idempotency_key="key-hash0001")
    other = build(policies)
    with pytest.raises(DecisionNotAllowedError, match="content hash"):
        approve(env.dev("bob"), p, content_hash=other.content_hash)


def test_expired_proposal_cannot_be_approved(repo, policies):
    env = Env(repo, policies, DEV_CONFIG)
    p = build(policies)
    env.dev("alice").create_proposal(p, idempotency_key="key-expprop1")
    env.clock.now = p.expires_at
    with pytest.raises(DecisionNotAllowedError, match="EXPIRED"):
        approve(env.dev("bob"), p)


def test_expired_approval_lapses_and_no_longer_authorises(repo, policies):
    env = Env(repo, policies, DEV_CONFIG)
    p = build(policies, ttl=timedelta(hours=1))
    env.dev("alice").create_proposal(p, idempotency_key="key-expappr1")
    bob = env.dev("bob")
    d = approve(bob, p).decision
    assert bob.verify_for_execution(p.proposal_id).allowed
    env.clock.now = d.approval_expires_at
    check = bob.verify_for_execution(p.proposal_id)
    assert not check.allowed and "EXPIRED" in check.reasons[0]
    events = env.proposals.history(p.proposal_id)
    assert events[-1].event_type == EventType.PROPOSAL_EXPIRED and events[-1].reason == "approval expiry reached"
    with pytest.raises(DecisionNotAllowedError):
        env.dev("carol").revoke(p.proposal_id, content_hash=p.content_hash, approval_id=d.decision_id,
                                nonce=new_nonce(), idempotency_key="key-latervk1", reason="late")


def test_reject_and_terminal_states(repo, policies):
    env = Env(repo, policies, DEV_CONFIG)
    p = build(policies)
    env.dev("alice").create_proposal(p, idempotency_key="key-reject01")
    r = reject(env.dev("alice"), p, reason="wrong owner")
    assert r.head.state == ProposalState.REJECTED and r.decision.decision == DecisionType.REJECT
    assert r.decision.approval_expires_at is None
    for attempt in (lambda: approve(env.dev("bob"), p), lambda: reject(env.dev("bob"), p)):
        with pytest.raises(DecisionNotAllowedError):
            attempt()


def test_revoke_is_distinct_from_reject(repo, policies):
    env = Env(repo, policies, DEV_CONFIG)
    p = build(policies)
    env.dev("alice").create_proposal(p, idempotency_key="key-revoke01")
    carol = env.dev("carol")
    with pytest.raises(DecisionNotAllowedError, match="only APPROVED"):
        carol.revoke(p.proposal_id, content_hash=p.content_hash, approval_id="decision-00000000-0000-4000-8000-000000000000",
                     nonce=new_nonce(), idempotency_key="key-revoke02", reason="x")
    d = approve(env.dev("bob"), p).decision
    with pytest.raises(DecisionNotAllowedError, match="approval id"):
        carol.revoke(p.proposal_id, content_hash=p.content_hash, approval_id="decision-00000000-0000-4000-8000-000000000000",
                     nonce=new_nonce(), idempotency_key="key-revoke03", reason="x")
    r = carol.revoke(p.proposal_id, content_hash=p.content_hash, approval_id=d.decision_id, nonce=new_nonce(),
                     idempotency_key="key-revoke04", reason="owner changed")
    assert r.head.state == ProposalState.REVOKED != ProposalState.REJECTED
    assert r.decision.decision == DecisionType.REVOKE and r.decision.revokes_decision_id == d.decision_id
    assert env.proposals.history(p.proposal_id)[-1].event_type == EventType.REVOKED
    assert not carol.verify_for_execution(p.proposal_id).allowed
    with pytest.raises(DecisionNotAllowedError):
        approve(env.dev("bob"), p)


def test_policy_change_makes_proposal_stale_instead_of_approvable(repo, tmp_path):
    pm = PolicyManager(LocalPolicyStore(tmp_path / "pol"))
    pm.ensure_default()
    env = Env(repo, pm, DEV_CONFIG)
    p = build(pm)
    env.dev("alice").create_proposal(p, idempotency_key="key-stale001")
    data = DEFAULT_POLICY_DOCUMENT.model_dump()
    data["defaults"].append({"key": "Team"})
    pm.save(parse_policy(data), expected_version=1, created_by="admin")
    with pytest.raises(DecisionNotAllowedError, match="STALE"):
        approve(env.dev("bob"), p)
    assert env.proposals.head(p.proposal_id).state == ProposalState.STALE


# ================================================================ LIVE mode

def test_live_approval_with_separation_of_duties(repo, policies):
    env = Env(repo, policies, LIVE_CONFIG)
    p = build(policies)
    env.live(assumed("Requester")).create_proposal(p, idempotency_key="key-live0001")
    stored = repo.get_stored(p.proposal_id)
    assert stored.created_by == Actor(actor_type=ActorType.AWS_PRINCIPAL, id=ROLE_REQ)
    d = approve(env.live(assumed("Approver", "ops-session")), p).decision
    assert d.mode == OperatingMode.LIVE and d.principal.id == ROLE_APP
    assert d.principal_arn == assumed("Approver", "ops-session") and d.principal.authenticated
    assert env.live(assumed("Approver")).verify_for_execution(p.proposal_id).allowed


def test_live_requester_cannot_approve_own_proposal_even_with_another_session(repo, policies):
    env = Env(repo, policies, LIVE_CONFIG)
    p = build(policies)
    env.live(USER_ALICE).create_proposal(p, idempotency_key="key-livesod1")
    with pytest.raises(DecisionNotAllowedError, match="separation of duties"):
        approve(env.live(USER_ALICE), p)
    cfg = json.loads(json.dumps(LIVE_CONFIG))
    cfg["accounts"][ACCT]["create"].append(ROLE_APP)
    env2 = Env(repo, policies, cfg)
    q = build(policies)
    env2.live(assumed("Approver", "session-a")).create_proposal(q, idempotency_key="key-livesod2")
    with pytest.raises(DecisionNotAllowedError, match="separation of duties"):
        approve(env2.live(assumed("Approver", "session-b")), q)  # different session, same role


def test_development_identity_is_rejected_for_live_operations(repo, policies):
    env = Env(repo, policies, {"accounts": {ACCT: {k: v + ["dev:bob"] for k, v in LIVE_CONFIG["accounts"][ACCT].items()}}})
    p = build(policies)
    env.live(assumed("Requester")).create_proposal(p, idempotency_key="key-livedev1")

    class DevPretendingLive:
        mode = OperatingMode.LIVE

        def current(self):
            return DevelopmentIdentityProvider("bob").current()

    with pytest.raises(IdentityError, match="development identities are refused"):
        approve(env.svc(DevPretendingLive()), p)
    assert env.proposals.head(p.proposal_id).state == ProposalState.PENDING


def test_live_approval_requires_an_authenticated_requester(repo, policies):
    env = Env(repo, policies, {"accounts": {ACCT: {**LIVE_CONFIG["accounts"][ACCT], "create": ["dev:alice"]}}})
    p = build(policies)
    env.dev("alice").create_proposal(p, idempotency_key="key-devreq01")  # created in FIXTURE mode
    with pytest.raises(DecisionNotAllowedError, match="authenticated principal"):
        approve(env.live(assumed("Approver")), p)


def test_wrong_aws_account_is_denied(repo, policies):
    env = Env(repo, policies, LIVE_CONFIG)
    p = build(policies)
    with pytest.raises(AuthorizationError, match="not the proposal account"):
        env.live(f"arn:aws:iam::{OTHER}:user/alice", account=OTHER).create_proposal(p, idempotency_key="key-wrongac1")
    env.live(assumed("Requester")).create_proposal(p, idempotency_key="key-wrongac2")
    with pytest.raises(AuthorizationError, match="not the proposal account"):
        approve(env.live(f"arn:aws:sts::{OTHER}:assumed-role/Approver/s", account=OTHER), p)


def test_fixture_approval_never_authorises_live_execution(repo, policies):
    cfg = {"accounts": {ACCT: {k: v + DEV_CONFIG["accounts"][ACCT][k] for k, v in LIVE_CONFIG["accounts"][ACCT].items()}}}
    env = Env(repo, policies, cfg)
    p = build(policies)
    env.dev("alice").create_proposal(p, idempotency_key="key-fixlive1")
    approve(env.dev("bob"), p)
    check = env.live(assumed("Approver")).verify_for_execution(p.proposal_id)
    assert not check.allowed
    assert "FIXTURE approvals never authorise LIVE operations" in check.reasons


def test_execution_recheck_uses_the_current_allow_list(repo, policies):
    env = Env(repo, policies, LIVE_CONFIG)
    p = build(policies, ttl=timedelta(hours=1))
    env.live(assumed("Requester")).create_proposal(p, idempotency_key="key-recheck1")
    approve(env.live(assumed("Approver")), p)
    executor = env.live(assumed("Approver"))
    assert executor.verify_for_execution(p.proposal_id).allowed
    cfg = json.loads(json.dumps(LIVE_CONFIG))
    cfg["accounts"][ACCT]["approve"].remove(ROLE_APP)
    edit_authz(env.authz, cfg, OperatingMode.LIVE, note="remove approver role")
    check = executor.verify_for_execution(p.proposal_id)
    assert not check.allowed and "no longer authorised" in check.reasons[0]
    other = env.live(f"arn:aws:iam::{OTHER}:user/alice", account=OTHER).verify_for_execution(p.proposal_id)
    assert "caller account does not match the proposal account" in other.reasons


def test_allow_list_change_applies_to_new_decisions_and_is_recorded(repo, policies):
    env = Env(repo, policies, DEV_CONFIG)
    p, q = build(policies), build(policies, "Tag all Lambda functions with Project=CloudGuard")
    env.dev("alice").create_proposal(p, idempotency_key="key-azchg001")
    env.dev("alice").create_proposal(q, idempotency_key="key-azchg002")
    assert approve(env.dev("bob"), p).decision.authorization.ref.version == 1
    cfg = json.loads(json.dumps(DEV_CONFIG))
    cfg["accounts"][ACCT]["approve"] = ["dev:carol"]
    edit_authz(env.authz, cfg, OperatingMode.FIXTURE)
    with pytest.raises(AuthorizationError):
        approve(env.dev("bob"), q)
    assert approve(env.dev("carol"), q).decision.authorization.ref.version == 2


# ================================================================ replay, idempotency, concurrency

def test_nonce_is_single_use_across_proposals(repo, policies):
    env = Env(repo, policies, DEV_CONFIG)
    p, q = build(policies), build(policies, "Tag all Lambda functions with Project=CloudGuard")
    env.dev("alice").create_proposal(p, idempotency_key="key-nonce001")
    env.dev("alice").create_proposal(q, idempotency_key="key-nonce002")
    nonce = new_nonce()
    approve(env.dev("bob"), p, nonce=nonce)
    with pytest.raises(NonceReplayError):
        approve(env.dev("bob"), q, nonce=nonce)
    assert env.proposals.head(q.proposal_id).state == ProposalState.PENDING
    assert repo.list_decisions(q.proposal_id) == []


@pytest.mark.parametrize("nonce", ["short", "has space 0123456789012345", "x" * 129])
def test_invalid_nonces_are_rejected(repo, policies, nonce):
    env = Env(repo, policies, DEV_CONFIG)
    p = build(policies)
    env.dev("alice").create_proposal(p, idempotency_key="key-badnonce")
    with pytest.raises(ValidationError):
        approve(env.dev("bob"), p, nonce=nonce)


def test_decision_idempotency(repo, policies):
    env = Env(repo, policies, DEV_CONFIG)
    p, q = build(policies), build(policies, "Tag all Lambda functions with Project=CloudGuard")
    env.dev("alice").create_proposal(p, idempotency_key="key-didem001")
    env.dev("alice").create_proposal(q, idempotency_key="key-didem002")
    first = approve(env.dev("bob"), p, idempotency_key="key-approve1", nonce=new_nonce())
    again = approve(env.dev("bob"), p, idempotency_key="key-approve1", nonce=new_nonce())
    assert again.created is False and again.decision == first.decision
    assert len(repo.list_decisions(p.proposal_id)) == 1
    with pytest.raises(IdempotencyConflictError):  # same key, different request
        approve(env.dev("bob"), p, idempotency_key="key-approve1", reason="different")
    with pytest.raises(IdempotencyConflictError):  # same key, different proposal
        approve(env.dev("bob"), q, idempotency_key="key-approve1")
    with pytest.raises(IdempotencyConflictError):  # same key, different principal
        approve(env.dev("carol"), p, idempotency_key="key-approve1")


def test_concurrent_decisions_from_one_head_only_one_wins(repo, policies):
    """Two deciders pass every service-level check against the same PENDING view; only storage
    arbitration remains, and it lets exactly one commit."""
    env = Env(repo, policies, DEV_CONFIG)
    p = build(policies)
    env.dev("alice").create_proposal(p, idempotency_key="key-concdec1")
    pending_view = env.proposals.get(p.proposal_id)
    approve(env.dev("bob"), p)
    env.proposals.get = lambda pid: pending_view  # the second decider still sees PENDING
    try:
        with pytest.raises(TransitionConflictError):
            reject(env.dev("carol"), p)
    finally:
        del env.proposals.get
    assert repo.get_head(p.proposal_id).state == ProposalState.APPROVED
    assert [d.decision for d in repo.list_decisions(p.proposal_id)] == [DecisionType.APPROVE]


def test_threaded_approve_and_reject_race(tmp_path, policies):
    repo = LocalProposalRepository(tmp_path / "proposals")
    env = Env(repo, policies, DEV_CONFIG)
    p = build(policies)
    env.dev("alice").create_proposal(p, idempotency_key="key-threadrc")
    outcomes, barrier = [], threading.Barrier(6)

    def run(fn):
        barrier.wait()
        try:
            fn()
            outcomes.append("ok")
        except (TransitionConflictError, DecisionNotAllowedError):
            outcomes.append("lost")

    workers = [lambda: approve(env.dev("bob"), p) for _ in range(3)] + [lambda: reject(env.dev("carol"), p) for _ in range(3)]
    threads = [threading.Thread(target=run, args=(w,)) for w in workers]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert outcomes.count("ok") == 1 and outcomes.count("lost") == 5
    assert len(repo.list_decisions(p.proposal_id)) == 1
    assert len(env.proposals.history(p.proposal_id)) == 2


# ================================================================ audit reconstruction and tampering

def test_audit_reconstructs_decisions(repo, policies):
    env = Env(repo, policies, DEV_CONFIG)
    p = build(policies)
    env.dev("alice").create_proposal(p, idempotency_key="key-audit001")
    d = approve(env.dev("bob"), p).decision
    stored, events, decisions = repo.get_stored(p.proposal_id), repo.list_events(p.proposal_id), repo.list_decisions(p.proposal_id)
    head = reconstruct_head(stored, events)
    assert head == repo.get_head(p.proposal_id)
    assert (head.approval_id, head.approval_expires_at) == (d.decision_id, d.approval_expires_at)
    verify_decisions(stored, events, decisions)


def _tamper_decision(repo, pid, decision, **changes):
    forged = decision.model_copy(update=changes)
    if isinstance(repo, DynamoProposalRepository):
        repo.client.put_item(TableName=TABLE, Item={
            "PK": {"S": f"PROPOSAL#{pid}"}, "SK": {"S": f"DECISION#{decision.decision_id}"},
            "entity": {"S": "DecisionRecord"}, "decision": {"S": decision.decision.value},
            "decision_json": {"S": forged.model_dump_json()}})
    else:
        repo.files.replace(f"proposals/{pid}/DECISION-{decision.decision_id}.json", forged.model_dump_json())


@pytest.mark.parametrize("changes", [
    {"principal": Actor(actor_type=ActorType.DEVELOPMENT, id="dev:mallory")},
    {"content_hash": "sha256:" + "0" * 64},
    {"binding_digest": "sha256:" + "0" * 64},
    {"decided_at": NOW + timedelta(minutes=3)},
])
def test_tampered_decision_records_are_detected(repo, policies, changes):
    env = Env(repo, policies, DEV_CONFIG)
    p = build(policies)
    env.dev("alice").create_proposal(p, idempotency_key="key-tamperd1")
    d = approve(env.dev("bob"), p).decision
    _tamper_decision(repo, p.proposal_id, d, **changes)
    with pytest.raises(ProposalIntegrityError):
        env.proposals.get(p.proposal_id)


def test_orphan_and_forged_decision_records_are_detected(repo, policies):
    env = Env(repo, policies, DEV_CONFIG)
    p = build(policies)
    env.dev("alice").create_proposal(p, idempotency_key="key-orphan01")
    d = approve(env.dev("bob"), p).decision
    orphan = d.model_copy(update={"decision_id": "decision-11111111-1111-4111-8111-111111111111"})
    _tamper_decision(repo, p.proposal_id, orphan)
    with pytest.raises(ProposalIntegrityError, match="without events"):
        env.proposals.get(p.proposal_id)


def test_ttl_only_on_nonce_and_idempotency_records(policies):
    with mock_aws():
        client = boto3.client("dynamodb", region_name="ap-south-1", aws_access_key_id="t", aws_secret_access_key="t")
        client.create_table(TableName=TABLE, BillingMode="PAY_PER_REQUEST",
                            AttributeDefinitions=[{"AttributeName": "PK", "AttributeType": "S"},
                                                  {"AttributeName": "SK", "AttributeType": "S"}],
                            KeySchema=[{"AttributeName": "PK", "KeyType": "HASH"}, {"AttributeName": "SK", "KeyType": "RANGE"}])
        env = Env(DynamoProposalRepository(client, TABLE), policies, DEV_CONFIG)
        p = build(policies)
        env.dev("alice").create_proposal(p, idempotency_key="key-ttl00001")
        d = approve(env.dev("bob"), p).decision
        items = client.scan(TableName=TABLE)["Items"]
    for item in items:
        pk, sk = item["PK"]["S"], item["SK"]["S"]
        if pk.startswith("PROPOSAL#"):
            assert "ttl_epoch" not in item, sk
        else:
            assert pk.startswith(("NONCE#", "IDEMP#")) and "ttl_epoch" in item
            if pk.startswith("NONCE#"):
                assert int(item["ttl_epoch"]["N"]) > int(d.approval_expires_at.timestamp())
    assert any(i["SK"]["S"].startswith("DECISION#") for i in items)


# ================================================================ no AI/text path to authorisation

def test_no_api_accepts_a_principal_or_free_text_decision():
    for name in ("approve", "reject", "revoke", "create_proposal", "verify_for_execution"):
        params = set(inspect.signature(getattr(DecisionService, name)).parameters)
        assert not params & {"principal", "actor", "approver", "identity", "decided_by", "decision_text"}, name
    for text in ("approve proposal proposal-123", "I approve this change", "approved", "reject it"):
        assert not isinstance(RuleBasedParser().parse(text), TaggingRequest)
    assert not hasattr(approval_module, "parse_decision")


# ================================================================ CLI

def test_cli_decision_flow(tmp_path, capsys, monkeypatch):
    s = ["--proposal-store", str(tmp_path / "p"), "--policy-store", str(tmp_path / "pol"), "--authz-store", str(tmp_path / "az")]
    monkeypatch.setenv(BOOTSTRAP_ENV, "dev:admin")
    assert main(["authz", "validate", str(AUTHZ_FIXTURE)]) == 0
    assert main(["authz", "save", str(AUTHZ_FIXTURE), "--expected-version", "0", "--dev-identity", "admin",
                 "--authz-store", str(tmp_path / "az")]) == 0
    assert main(["authz", "history", "--authz-store", str(tmp_path / "az")]) == 0
    capsys.readouterr()
    assert main(["proposal", "save", "Update Owner of all Lambda functions to platform-team", "--dev-identity", "alice",
                 "--fixture", str(FIXTURE), *s]) == 0
    pid = json.loads(capsys.readouterr().out)["proposal_id"]
    assert main(["proposal", "show", pid, *s[:4]]) == 0
    h = json.loads(capsys.readouterr().out)["proposal"]["content_hash"]
    assert main(["proposal", "approve", pid, "--content-hash", h, "--dev-identity", "bob", *s]) == 3
    assert "overwrite acknowledgement" in capsys.readouterr().err
    assert main(["proposal", "approve", pid, "--content-hash", h, "--dev-identity", "alice",
                 "--acknowledge-overwrites", *s]) == 3
    assert "lacks 'approve'" in capsys.readouterr().err
    assert main(["proposal", "approve", pid, "--content-hash", h, "--dev-identity", "bob", "--acknowledge-overwrites", *s]) == 0
    approved = json.loads(capsys.readouterr().out)
    assert approved["head"]["state"] == "APPROVED"
    assert main(["proposal", "revoke", pid, "--content-hash", h, "--approval-id", approved["decision"]["decision_id"],
                 "--dev-identity", "carol", "--reason", "changed", *s]) == 0
    assert json.loads(capsys.readouterr().out)["head"]["state"] == "REVOKED"


def test_cli_live_mode_never_takes_a_dev_identity(tmp_path):
    with pytest.raises(SystemExit) as exc:
        main(["proposal", "approve", "proposal-00000000-0000-4000-8000-000000000000", "--content-hash", "x",
              "--mode", "live", "--dev-identity", "bob"])
    assert exc.value.code == 2


def test_cli_rejects_invalid_allow_list(tmp_path, capsys):
    bad = tmp_path / "bad.json"
    bad.write_text(json.dumps({"accounts": {ACCT: {"approve": ["*"]}}}), encoding="utf-8")
    assert main(["authz", "validate", str(bad)]) == 2
    assert "no wildcards" in capsys.readouterr().err
