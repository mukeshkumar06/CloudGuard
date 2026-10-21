"""M3: separation of duties between PEOPLE (person bindings), not between AWS principals.

LIVE identities come from StsCallerIdentityProvider with fake STS responses that include a realistic
UserId. Role bindings rest on administrator assertions about IAM trust policies; the application
can't verify them, and these tests don't pretend to.
"""

import json
from datetime import timedelta

import pytest

from authz_helpers import fake_unique_id, live_identity
from cloudguard.governance.remediation.approval import AuthorizationError, DecisionNotAllowedError, new_nonce
from cloudguard.governance.remediation.authz import (
    AuthzDeniedError,
    AuthzManager,
    AuthzValidationError,
    LocalAuthzStore,
    PersonResolutionError,
    parse_authz,
    resolve_person,
)
from cloudguard.governance.remediation.executor import ExecutionNotAllowedError
from cloudguard.governance.remediation.execution_models import ExecutionIntegrityError
from cloudguard.governance.remediation.identity import (
    DevelopmentIdentityProvider,
    IdentityError,
    StsCallerIdentityProvider,
    principal_from_caller_arn,
)
from cloudguard.governance.remediation.models import OperatingMode, ProposalIntegrityError, ProposalState
from cloudguard.governance.remediation.repository import LocalProposalRepository
from cloudguard.governance.remediation.writers import SimulatedCrash
from test_approval import ACCT, NOW, Env, approve, assumed, build, policies, repo, reject  # noqa: F401
from test_approval import _isolated_cwd  # noqa: F401
from test_execution import CONFIG as EXEC_CONFIG, LAMBDA, World, _crash_at

USER_ALICE = f"arn:aws:iam::{ACCT}:user/alice"
ADMIN_ROLE = f"arn:aws:iam::{ACCT}:role/AdminRole"
SHARED = f"arn:aws:iam::{ACCT}:role/SharedGovernance"
APPROVER = f"arn:aws:iam::{ACCT}:role/Approver"


def bind_user(arn, person, **kw):
    return {"kind": "iam_user", "principal": arn, "person": person, **kw}


def bind_role(arn, person, **kw):
    return {"kind": "single_person_role", "principal": arn, "person": person, **kw}


def bind_sessions(arn, sessions, source="iam-username", **kw):
    return {"kind": "session_attributed_role", "principal": arn, "sessions": sessions, "session_source": source, **kw}


def live_cfg(bindings, **perms):
    return {"accounts": {ACCT: {**perms, "bindings": bindings}}}


# ================================================================ V1: one person, two principals

def test_iam_user_and_assumed_role_of_the_same_person_cannot_self_approve(repo, policies):
    env = Env(repo, policies, live_cfg([bind_user(USER_ALICE, "alice"), bind_role(ADMIN_ROLE, "alice")],
                                       create=[USER_ALICE], approve=[ADMIN_ROLE], reject=[ADMIN_ROLE]))
    p = build(policies)
    env.live(USER_ALICE).create_proposal(p, idempotency_key="key-m3-v1-01")
    with pytest.raises(DecisionNotAllowedError, match="person 'alice' requested this proposal"):
        approve(env.live(assumed("AdminRole", "anything")), p)
    assert env.proposals.head(p.proposal_id).state == ProposalState.PENDING


# ================================================================ V2: session-attributed roles

def test_two_people_sharing_a_session_attributed_role_can_approve_each_other(repo, policies):
    env = Env(repo, policies, live_cfg([bind_sessions(SHARED, {"bob": "bob", "carol": "carol"})],
                                       create=[SHARED], approve=[SHARED], reject=[SHARED]))
    p, q = build(policies), build(policies, "Tag all Lambda functions with Project=CloudGuard")
    env.live(assumed("SharedGovernance", "bob")).create_proposal(p, idempotency_key="key-m3-v2-01")
    d = approve(env.live(assumed("SharedGovernance", "carol")), p).decision
    assert (d.identity.person_id, d.identity.binding_kind) == ("carol", "session_attributed_role")
    env.live(assumed("SharedGovernance", "carol")).create_proposal(q, idempotency_key="key-m3-v2-02")
    with pytest.raises(DecisionNotAllowedError, match="person 'carol'"):
        approve(env.live(assumed("SharedGovernance", "carol")), q)  # carol can't approve her own proposal


def test_unattributed_session_fails_closed_for_every_live_action(repo, policies):
    env = Env(repo, policies, live_cfg([bind_sessions(SHARED, {"bob": "bob"}), bind_role(APPROVER, "approver")],
                                       create=[SHARED], approve=[APPROVER], reject=[SHARED, APPROVER]))
    p = build(policies)
    mallory = env.live(assumed("SharedGovernance", "mallory"))  # session not attributed to anyone
    with pytest.raises(PersonResolutionError, match="not attributed to any person"):
        mallory.create_proposal(p, idempotency_key="key-m3-v2-03")
    env.live(assumed("SharedGovernance", "bob")).create_proposal(p, idempotency_key="key-m3-v2-04")
    with pytest.raises(PersonResolutionError):
        reject(mallory, p)
    assert env.proposals.head(p.proposal_id).state == ProposalState.PENDING
    assert isinstance(PersonResolutionError("x"), IdentityError)  # the CLI reports it as a denial (exit 3)


def test_session_attribution_requires_an_explicit_trust_assertion():
    unasserted = {"kind": "session_attributed_role", "principal": SHARED, "sessions": {"bob": "bob"}}
    with pytest.raises(AuthzValidationError, match="session_source"):
        parse_authz(live_cfg([unasserted], create=[SHARED]))
    with pytest.raises(AuthzValidationError):
        parse_authz(live_cfg([bind_sessions(SHARED, {"bob": "bob"}, source="caller-chosen")], create=[SHARED]))
    with pytest.raises(AuthzValidationError, match="name exactly one person"):  # a single-person role can't map sessions
        parse_authz(live_cfg([{**bind_role(SHARED, "bob"), "sessions": {"carol": "carol"}}], create=[SHARED]))


# ================================================================ R0: unbound principals

def test_unbound_live_entries_and_root_are_rejected_by_the_allow_list():
    with pytest.raises(AuthzValidationError, match="is not bound to a person"):
        parse_authz(live_cfg([], create=[USER_ALICE]))
    with pytest.raises(AuthzValidationError, match="is not bound to a person"):
        parse_authz(live_cfg([], approve=[f"arn:aws:iam::{ACCT}:root"]))
    with pytest.raises(AuthzValidationError, match="bound more than once"):
        parse_authz(live_cfg([bind_user(USER_ALICE, "alice"), bind_user(USER_ALICE, "bob")], create=[USER_ALICE]))
    with pytest.raises(AuthzValidationError, match="belongs to another account"):
        parse_authz(live_cfg([bind_user("arn:aws:iam::210987654321:user/x", "x")], create=[USER_ALICE]))


def test_unbound_principals_never_resolve_to_a_person():
    config = parse_authz(live_cfg([bind_user(USER_ALICE, "alice")], create=[USER_ALICE]))
    bob = principal_from_caller_arn(f"arn:aws:iam::{ACCT}:user/bob", ACCT, NOW, fake_unique_id("AIDA", "bob"))
    root = principal_from_caller_arn(f"arn:aws:iam::{ACCT}:root", ACCT, NOW, ACCT)
    for principal in (bob, root):
        with pytest.raises(PersonResolutionError, match="not bound to a person"):
            resolve_person(config, principal, ACCT, OperatingMode.LIVE)
    with pytest.raises(PersonResolutionError):  # wrong account
        resolve_person(config, bob, "210987654321", OperatingMode.LIVE)
    dev = DevelopmentIdentityProvider("alice").current()
    with pytest.raises(PersonResolutionError):  # development identities never resolve in LIVE
        resolve_person(config, dev, ACCT, OperatingMode.LIVE)
    assert resolve_person(config, dev, ACCT, OperatingMode.FIXTURE) == ("alice", "development")


# ================================================================ rule 4: admin/approve overlap

@pytest.mark.parametrize("config", [
    {"accounts": {ACCT: {"admin": ["dev:alice"], "approve": ["dev:alice"]}}},
    live_cfg([bind_user(USER_ALICE, "alice"), bind_role(ADMIN_ROLE, "alice")], admin=[USER_ALICE], approve=[ADMIN_ROLE]),
    live_cfg([bind_sessions(SHARED, {"bob": "bob", "carol": "carol"}), bind_role(APPROVER, "carol")],
             admin=[SHARED], approve=[APPROVER]),
])
def test_a_person_cannot_hold_admin_and_approve(config):
    with pytest.raises(AuthzValidationError, match="hold both 'admin' and 'approve'"):
        parse_authz(config)


def test_admin_cannot_rebind_their_second_principal_to_approve_their_own_proposal(repo, policies):
    """The V3 path: alice requests as an IAM user; her AdminRole can't be given 'approve' while she
    is an administrator, and binding it to her makes approval of her own proposal a self-approval."""
    env = Env(repo, policies, live_cfg([bind_user(USER_ALICE, "alice"), bind_role(APPROVER, "approver")],
                                       create=[USER_ALICE], approve=[APPROVER], reject=[APPROVER]))
    with pytest.raises(AuthzValidationError, match="hold both"):
        parse_authz(live_cfg([bind_user(USER_ALICE, "alice"), bind_role(ADMIN_ROLE, "alice")],
                             admin=[USER_ALICE], approve=[ADMIN_ROLE]))
    p = build(policies)
    env.live(USER_ALICE).create_proposal(p, idempotency_key="key-m3-v3-01")
    from authz_helpers import edit_authz
    edit_authz(env.authz, live_cfg([bind_user(USER_ALICE, "alice"), bind_role(APPROVER, "approver"),
                                    bind_role(ADMIN_ROLE, "alice")],
                                   create=[USER_ALICE], approve=[APPROVER, ADMIN_ROLE], reject=[APPROVER]),
               OperatingMode.LIVE)
    with pytest.raises(DecisionNotAllowedError, match="person 'alice'"):
        approve(env.live(assumed("AdminRole", "x")), p)


# ================================================================ unique-id pinning (V4)

def test_mismatched_pinned_sts_unique_id_is_rejected(repo, policies):
    pinned_user, pinned_role = fake_unique_id("AIDA", "alice"), fake_unique_id("AROA", "Approver")
    env = Env(repo, policies, live_cfg([bind_user(USER_ALICE, "alice", unique_id=pinned_user),
                                        bind_role(APPROVER, "approver", unique_id=pinned_role)],
                                       create=[USER_ALICE], approve=[APPROVER], reject=[APPROVER]))
    p = build(policies)
    recreated = env.svc(live_identity(USER_ALICE, ACCT, clock=env.clock, user_id=fake_unique_id("AIDA", "alice-2")))
    with pytest.raises(PersonResolutionError, match="does not match the pinned id"):
        recreated.create_proposal(p, idempotency_key="key-m3-v4-01")
    env.live(USER_ALICE).create_proposal(p, idempotency_key="key-m3-v4-02")  # the pinned id matches
    other_role = env.svc(live_identity(assumed("Approver", "s1"), ACCT, clock=env.clock,
                                       user_id=f"{fake_unique_id('AROA', 'Approver-2')}:s1"))
    with pytest.raises(PersonResolutionError, match="does not match the pinned id"):
        approve(other_role, p)
    assert approve(env.live(assumed("Approver", "s1")), p).decision.identity.sts_user_id.startswith(pinned_role)


def test_unique_id_pins_must_have_the_right_prefix():
    with pytest.raises(AuthzValidationError, match="AIDA"):
        parse_authz(live_cfg([bind_user(USER_ALICE, "alice", unique_id=fake_unique_id("AROA", "x"))], create=[USER_ALICE]))


# ================================================================ STS UserId capture

def test_sts_user_id_is_captured_and_must_be_consistent():
    class Sts:
        def __init__(self, resp):
            self.resp = resp

        def get_caller_identity(self):
            return self.resp
    arn = assumed("Approver", "s1")
    ok = StsCallerIdentityProvider(Sts({"Arn": arn, "Account": ACCT, "UserId": "AROAEXAMPLEID12345:s1"})).current()
    assert (ok.user_id, ok.unique_id, ok.session_name) == ("AROAEXAMPLEID12345:s1", "AROAEXAMPLEID12345", "s1")
    for user_id in (None, "", "AROAEXAMPLEID12345:other", "AROAEXAMPLEID12345"):
        with pytest.raises(IdentityError):
            StsCallerIdentityProvider(Sts({"Arn": arn, "Account": ACCT, "UserId": user_id})).current()


# ================================================================ executor separation (fixture executor)

def _exec_config(**changes):
    cfg = json.loads(json.dumps(EXEC_CONFIG))
    cfg["accounts"][ACCT].update(changes)
    return cfg


def test_executor_must_differ_from_approver_on_start_and_resume(repo):
    w = World(repo, config=_exec_config(execute=["dev:alice", "dev:erin", "dev:bob"]))
    with pytest.raises(ExecutionNotAllowedError, match="person 'bob' approved this proposal"):
        w.execute(name="bob", key="key-exec-bob-01")
    assert w.state() == ProposalState.APPROVED and not w.account.write_log
    with pytest.raises(SimulatedCrash):
        w.execute(hooks=_crash_at("before_write", LAMBDA + "shared-name"))
    w.clock.now += timedelta(minutes=3)
    with pytest.raises(ExecutionNotAllowedError, match="person 'bob' approved"):
        w.execute(name="bob", key="key-exec-bob-02")  # nor resume it
    assert w.execute(name="erin", key="key-exec-erin-1").summary.outcome == ProposalState.SUCCEEDED


def test_executor_requester_separation_is_configurable(repo):
    default = World(repo)
    assert default.execute().summary.outcome == ProposalState.SUCCEEDED  # alice requested and executes: allowed
    w = World(LocalProposalRepository(), config=_exec_config(
        separation={"fixture_executor_must_differ_from_requester": True}))
    with pytest.raises(ExecutionNotAllowedError, match="person 'alice' requested this proposal"):
        w.execute(name="alice", key="key-exec-sep-01")
    assert w.execute(name="erin", key="key-exec-sep-02").summary.outcome == ProposalState.SUCCEEDED


def test_fixture_self_approval_is_unchanged_by_default_and_can_be_enforced(repo, policies):
    cfg = {"accounts": {ACCT: {"create": ["dev:alice"], "approve": ["dev:alice"], "reject": ["dev:alice"]}}}
    env = Env(repo, policies, cfg)
    p = build(policies)
    env.dev("alice").create_proposal(p, idempotency_key="key-m3-fx-01")
    assert approve(env.dev("alice"), p).decision.identity.person_id == "alice"  # default: allowed
    strict = Env(LocalProposalRepository(), policies,
                 {"accounts": {ACCT: {**cfg["accounts"][ACCT],
                                      "separation": {"fixture_approver_must_differ_from_requester": True}}}})
    q = build(policies)
    strict.dev("alice").create_proposal(q, idempotency_key="key-m3-fx-02")
    with pytest.raises(DecisionNotAllowedError, match="person 'alice' requested"):
        approve(strict.dev("alice"), q)


def test_bindings_and_live_separation_are_live_data(tmp_path):
    """Batch 1 mode isolation covers M3 data: a FIXTURE save can't change bindings or the LIVE setting."""
    m = AuthzManager(LocalAuthzStore(), bootstrap_admin=f"dev:root,{ADMIN_ROLE}")
    base = {"admin": ["dev:root", ADMIN_ROLE], "approve": ["dev:bob", APPROVER]}
    binds = [bind_role(ADMIN_ROLE, "admin"), bind_role(APPROVER, "approver")]
    m.save(parse_authz({"accounts": {ACCT: {"admin": ["dev:root"], "approve": ["dev:bob"]}}}), expected_version=None,
           identity=DevelopmentIdentityProvider("root"))
    v2 = m.save(parse_authz(live_cfg(binds, **base)), expected_version=1,
                identity=live_identity(assumed("AdminRole", "s"), ACCT))
    assert v2.change.person_id == "admin"
    for tampered in (live_cfg([bind_role(ADMIN_ROLE, "admin"), bind_role(APPROVER, "mallory")], **base),
                     {"accounts": {ACCT: {**base, "bindings": binds,
                                          "separation": {"live_executor_must_differ_from_requester": True}}}}):
        with pytest.raises(AuthzDeniedError, match="FIXTURE save cannot change LIVE"):
            m.save(parse_authz(tampered), expected_version=2, identity=DevelopmentIdentityProvider("root"))


def test_live_admin_change_requires_a_bound_person():
    m = AuthzManager(LocalAuthzStore(), bootstrap_admin=SHARED)
    config = live_cfg([bind_sessions(SHARED, {"ops": "ops"}), bind_role(APPROVER, "approver")],
                      admin=[SHARED], approve=[APPROVER])
    with pytest.raises(PersonResolutionError, match="not attributed"):
        m.save(parse_authz(config), expected_version=None, identity=live_identity(assumed("SharedGovernance", "x"), ACCT))
    v1 = m.save(parse_authz(config), expected_version=None, identity=live_identity(assumed("SharedGovernance", "ops"), ACCT))
    assert (v1.change.basis, v1.change.person_id) == ("BOOTSTRAP", "ops")


# ================================================================ evidence and tampering

def test_person_evidence_is_recorded_and_tampering_is_detected(policies):
    repo = LocalProposalRepository()
    env = Env(repo, policies, live_cfg([bind_user(USER_ALICE, "alice"), bind_role(APPROVER, "approver")],
                                       create=[USER_ALICE], approve=[APPROVER], reject=[APPROVER]))
    p = build(policies)
    env.live(USER_ALICE).create_proposal(p, idempotency_key="key-m3-ev-01")
    d = approve(env.live(assumed("Approver", "laptop")), p).decision
    ev = d.identity
    assert (ev.person_id, ev.binding_kind, ev.principal_arn) == ("approver", "single_person_role", assumed("Approver", "laptop"))
    assert ev.sts_user_id.endswith(":laptop") and ev.authz_version == env.authz.get_active().version
    assert ev.authz_hash == env.authz.get_active().content_hash
    stored = repo.get_stored(p.proposal_id)
    assert stored.requester_identity.person_id == "alice" and stored.requester_identity.binding_kind == "iam_user"

    base = f"proposals/{p.proposal_id}/"
    decision_file = base + f"DECISION-{d.decision_id}.json"
    original = repo.files.data[decision_file]
    data = json.loads(original)
    data["identity"]["person_id"] = "mallory"
    repo.files.data[decision_file] = json.dumps(data)
    with pytest.raises(ProposalIntegrityError, match="person"):
        env.proposals.get(p.proposal_id)
    repo.files.data[decision_file] = original
    meta = repo.files.data[base + "META.json"]
    data = json.loads(meta)
    data["requester_identity"]["person_id"] = "approver"
    repo.files.data[base + "META.json"] = json.dumps(data)
    with pytest.raises(ProposalIntegrityError, match="requester identity"):
        env.proposals.get(p.proposal_id)
    repo.files.data[base + "META.json"] = meta
    assert env.proposals.get(p.proposal_id).head.state == ProposalState.APPROVED


def test_execution_evidence_is_recorded_and_tampering_is_detected():
    repo = LocalProposalRepository()
    w = World(repo)
    view = w.execute()
    record = view.record
    assert (record.executor_identity.person_id, record.approver_identity.person_id) == ("alice", "bob")
    assert record.executor_identity.binding_kind == "development"
    name = next(n for n in repo.files.data if n.endswith(f"EXEC-{record.execution_id}-META.json"))
    data = json.loads(repo.files.data[name])
    data["approver_identity"]["person_id"] = "carol"
    repo.files.data[name] = json.dumps(data)
    with pytest.raises(ExecutionIntegrityError):
        w.executor().load(w.proposal.proposal_id, record.execution_id)
