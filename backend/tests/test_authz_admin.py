"""Batch 1 (H1): identity-bound administration of the authorization allow-list.

Every test runs against the in-memory, on-disk and DynamoDB (moto) stores where persistence
matters. LIVE identities come from StsCallerIdentityProvider with a fake STS client; all accounts
and ARNs are synthetic.
"""

import json

import boto3
import pytest
from moto import mock_aws

from authz_helpers import live_identity, with_bindings
from cloudguard.governance.__main__ import main
from cloudguard.governance.remediation.authz import (
    BOOTSTRAP_ENV,
    AuthzConflictError,
    AuthzDeniedError,
    AuthzIntegrityError,
    AuthzManager,
    DynamoAuthzStore,
    LocalAuthzStore,
    parse_bootstrap_admins,
)
from cloudguard.governance.remediation.authz import parse_authz as _parse_authz
from cloudguard.governance.remediation.identity import DevelopmentIdentityProvider, IdentityError
from cloudguard.governance.remediation.models import ActorType, OperatingMode, Permission

ACCT = "123456789012"
OTHER = "210987654321"
TABLE = "cloudguard-authz-test"
ADMIN_ROLE = f"arn:aws:iam::{ACCT}:role/AuthzAdmin"
OPS_ROLE = f"arn:aws:iam::{ACCT}:role/AuthzOps"
APPROVER_ROLE = f"arn:aws:iam::{ACCT}:role/Approver"
OTHER_ADMIN = f"arn:aws:iam::{OTHER}:role/AuthzAdmin"


@pytest.fixture(autouse=True)
def _isolated_cwd(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv(BOOTSTRAP_ENV, raising=False)


@pytest.fixture(params=["memory", "disk", "dynamodb"])
def store(request, tmp_path):
    if request.param == "memory":
        yield LocalAuthzStore()
    elif request.param == "disk":
        yield LocalAuthzStore(tmp_path / "authz")
    else:
        with mock_aws():
            client = boto3.client("dynamodb", region_name="ap-south-1", aws_access_key_id="t", aws_secret_access_key="t")
            client.create_table(TableName=TABLE, BillingMode="PAY_PER_REQUEST",
                                AttributeDefinitions=[{"AttributeName": "PK", "AttributeType": "S"},
                                                      {"AttributeName": "SK", "AttributeType": "S"}],
                                KeySchema=[{"AttributeName": "PK", "KeyType": "HASH"},
                                           {"AttributeName": "SK", "KeyType": "RANGE"}])
            yield DynamoAuthzStore(client, TABLE)


def dev(name):
    return DevelopmentIdentityProvider(name)


def live(role_or_user, account=ACCT, session="s1"):
    if ":role/" in role_or_user:
        name = role_or_user.rsplit("/", 1)[-1]
        return live_identity(f"arn:aws:sts::{account}:assumed-role/{name}/{session}", account)
    return live_identity(role_or_user, account)


def parse_authz(data):
    """M3: LIVE entries need person bindings; add the default ones (role -> person named after it)."""
    return _parse_authz(with_bindings(data) if isinstance(data, dict) else data)


def cfg(account=ACCT, **perms):
    return parse_authz({"accounts": {account: perms}})


def snapshot(m: AuthzManager):
    version = m.store.active_version(m.config_id)
    return version, (m.get_active().record_hash if version else None), [v.version for v in m.store.list_versions(m.config_id)]


def fixture_world(store, bootstrap="dev:root"):
    """FIXTURE allow-list bootstrapped by dev:root, with a non-admin requester (mallory) and approver (bob)."""
    m = AuthzManager(store, bootstrap_admin=bootstrap)
    m.save(cfg(admin=["dev:root"], create=["dev:mallory"], approve=["dev:bob"]), expected_version=None,
           identity=dev("root"))
    return m


def live_world(store):
    m = AuthzManager(store, bootstrap_admin=ADMIN_ROLE)
    m.save(cfg(admin=[ADMIN_ROLE], approve=[APPROVER_ROLE]), expected_version=None, identity=live(ADMIN_ROLE))
    return m


# ================================================================ bootstrap

def test_configured_bootstrap_administrator_creates_the_first_policy(store):
    m = AuthzManager(store, bootstrap_admin=f"{ADMIN_ROLE},dev:root")
    v1 = m.save(cfg(admin=["dev:root"], approve=["dev:bob"]), expected_version=None, identity=dev("root"))
    assert (v1.version, v1.change.basis, v1.change.bootstrap_admin, v1.created_by) == (1, "BOOTSTRAP", "dev:root", "dev:root")
    assert v1.initialized_modes == [OperatingMode.FIXTURE] and v1.change.principal.actor_type == ActorType.DEVELOPMENT
    both = parse_authz({"accounts": {ACCT: {"admin": ["dev:root", ADMIN_ROLE], "approve": ["dev:bob", APPROVER_ROLE]}}})
    v2 = m.save(both, expected_version=1, identity=live(ADMIN_ROLE, session="laptop"))
    change = v2.change
    assert (change.basis, change.mode, change.account_id, v2.created_by) == ("BOOTSTRAP", OperatingMode.LIVE, ACCT, ADMIN_ROLE)
    assert change.principal_arn == f"arn:aws:sts::{ACCT}:assumed-role/AuthzAdmin/laptop"  # exactly what STS returned
    assert change.authn_method == "sts:GetCallerIdentity" and change.principal.actor_type == ActorType.AWS_PRINCIPAL
    assert v2.initialized_modes == [OperatingMode.FIXTURE, OperatingMode.LIVE]
    assert v2.previous_record_hash == v1.record_hash and [v.version for v in m.history()] == [1, 2]


def test_bootstrap_requires_an_explicitly_configured_matching_administrator(store):
    target = cfg(admin=["dev:root"], approve=["dev:bob"])
    with pytest.raises(AuthzDeniedError, match="no FIXTURE bootstrap administrator is configured"):
        AuthzManager(store).save(target, expected_version=None, identity=dev("root"))
    with pytest.raises(AuthzDeniedError, match="no FIXTURE bootstrap"):  # a LIVE bootstrap admin doesn't cover FIXTURE
        AuthzManager(store, bootstrap_admin=ADMIN_ROLE).save(target, expected_version=None, identity=dev("root"))
    m = AuthzManager(store, bootstrap_admin="dev:root")
    with pytest.raises(AuthzDeniedError, match="not the configured FIXTURE bootstrap administrator"):
        m.save(cfg(admin=["dev:mallory"], approve=["dev:bob"]), expected_version=None, identity=dev("mallory"))
    with pytest.raises(AuthzDeniedError, match="must list dev:root as admin"):
        m.save(cfg(admin=["dev:other"], approve=["dev:bob"]), expected_version=None, identity=dev("root"))
    with pytest.raises(AuthzDeniedError, match="no FIXTURE administrator"):
        m.save(cfg(approve=["dev:bob"]), expected_version=None, identity=dev("root"))
    assert store.active_version("default") is None  # nothing was written by any refused attempt


def test_live_bootstrap_is_bound_to_the_callers_own_account(store):
    m = AuthzManager(store, bootstrap_admin=OTHER_ADMIN)
    with pytest.raises(AuthzDeniedError, match="caller's own account"):  # caller is OTHER's admin, config is ACCT's
        m.save(cfg(admin=[ADMIN_ROLE]), expected_version=None, identity=live(OTHER_ADMIN, account=OTHER))
    two = parse_authz({"accounts": {OTHER: {"admin": [OTHER_ADMIN]}, ACCT: {"admin": [ADMIN_ROLE]}}})
    with pytest.raises(AuthzDeniedError, match="caller's own account"):
        m.save(two, expected_version=None, identity=live(OTHER_ADMIN, account=OTHER))
    assert store.active_version("default") is None


def test_bootstrap_cannot_be_reused_after_initialisation(store, monkeypatch):
    fixture_world(store)  # FIXTURE initialised by dev:root
    escalate = cfg(admin=["dev:root", "dev:eve"], create=["dev:mallory"], approve=["dev:bob"])
    for configured in ("dev:eve", "dev:eve,arn:aws:iam::123456789012:root"):
        m = AuthzManager(store, bootstrap_admin=configured)  # the setting changed after initialisation
        with pytest.raises(AuthzDeniedError, match="dev:eve lacks 'admin'"):
            m.save(escalate, expected_version=1, identity=dev("eve"))
    # Even the original bootstrap administrator, once removed, can't come back through bootstrap.
    m = AuthzManager(store, bootstrap_admin="dev:root")
    m.save(cfg(admin=["dev:ops"], create=["dev:mallory"], approve=["dev:bob"]), expected_version=1, identity=dev("root"))
    with pytest.raises(AuthzDeniedError, match="dev:root lacks 'admin'"):
        m.save(cfg(admin=["dev:root"], approve=["dev:bob"]), expected_version=2, identity=dev("root"))
    with pytest.raises(AuthzDeniedError, match="remove every FIXTURE administrator"):  # can't empty the mode
        m.save(parse_authz({"accounts": {}}), expected_version=2, identity=dev("ops"))
    assert m.get_active().version == 2 and m.get_active().initialized_modes == [OperatingMode.FIXTURE]


def test_concurrent_bootstraps_cannot_both_succeed(store):
    """Both callers pass every check against 'no configuration'; the conditional commit lets one win."""
    first = AuthzManager(store, bootstrap_admin="dev:root")
    second = AuthzManager(store, bootstrap_admin="dev:other")
    real_commit = store.commit

    def racing_commit(new, expected):
        store.commit = real_commit  # let the competitor commit first, then continue with ours
        second.save(cfg(admin=["dev:other"], approve=["dev:bob"]), expected_version=None, identity=dev("other"))
        real_commit(new, expected)

    store.commit = racing_commit
    try:
        with pytest.raises(AuthzConflictError):
            first.save(cfg(admin=["dev:root"], approve=["dev:bob"]), expected_version=None, identity=dev("root"))
    finally:
        store.commit = real_commit
    active = first.get_active()
    assert (active.version, active.created_by) == (1, "dev:other") and [v.version for v in first.history()] == [1]


def test_concurrent_bootstrap_across_store_instances_on_disk(tmp_path):
    """Two processes sharing a directory: the on-disk lock + re-read makes the second commit conflict."""
    a, b = LocalAuthzStore(tmp_path / "shared"), LocalAuthzStore(tmp_path / "shared")
    AuthzManager(a, bootstrap_admin="dev:root").save(cfg(admin=["dev:root"]), expected_version=None, identity=dev("root"))
    late = AuthzManager(b, bootstrap_admin="dev:other")
    with pytest.raises(AuthzConflictError):
        late.save(cfg(admin=["dev:other"]), expected_version=None, identity=dev("other"))
    assert b.active_version("default") == 1 and late.get_active().created_by == "dev:root"


# ================================================================ changes after bootstrap

def test_existing_admin_can_update_and_the_change_is_audited(store):
    m = fixture_world(store)
    v2 = m.save(cfg(admin=["dev:root", "dev:ops"], create=["dev:mallory"], approve=["dev:bob"]), expected_version=1,
                identity=dev("root"), note="add ops")
    v3 = m.save(cfg(admin=["dev:root", "dev:ops"], create=["dev:mallory"], approve=["dev:carol"]), expected_version=2,
                identity=dev("ops"))
    assert (v3.change.basis, v3.created_by, v3.change.admin_entries) == ("ADMIN", "dev:ops", {ACCT: "dev:ops"})
    assert (v3.change.authorized_by_version, v3.change.authorized_by_record_hash) == (2, v2.record_hash)
    assert v3.change.changed_accounts == [ACCT] and v2.note == "add ops"
    bob = DevelopmentIdentityProvider("bob").current()
    assert not m.evaluate(bob, ACCT, Permission.APPROVE, OperatingMode.FIXTURE).allowed


def test_non_admin_cannot_update(store):
    m = fixture_world(store)
    before = snapshot(m)
    for who in ("mallory", "bob", "nobody"):
        with pytest.raises(AuthzDeniedError, match=f"dev:{who} lacks 'admin'"):
            m.save(cfg(admin=["dev:root"], create=["dev:mallory"], approve=["dev:bob", "dev:carol"]),
                   expected_version=1, identity=dev(who))
    assert snapshot(m) == before


def test_caller_cannot_self_promote(store):
    m = fixture_world(store)
    before = snapshot(m)
    mallory = DevelopmentIdentityProvider("mallory").current()
    attempts = [
        cfg(admin=["dev:root"], create=["dev:mallory"], approve=["dev:bob", "dev:mallory"]),
        cfg(admin=["dev:root", "dev:mallory"], create=["dev:mallory"], approve=["dev:bob"]),
        parse_authz({"accounts": {ACCT: {"admin": ["dev:root"], "create": ["dev:mallory"], "approve": ["dev:bob"]},
                                  OTHER: {"admin": ["dev:mallory"], "approve": ["dev:bob"]}}}),  # new account
    ]
    for attempt in attempts:
        with pytest.raises(AuthzDeniedError):
            m.save(attempt, expected_version=1, identity=dev("mallory"))
    assert snapshot(m) == before
    assert not m.evaluate(mallory, ACCT, Permission.APPROVE, OperatingMode.FIXTURE).allowed


def test_fixture_admin_may_add_an_account_but_not_lock_one_out(store):
    m = fixture_world(store)
    two = {"accounts": {ACCT: {"admin": ["dev:root"], "create": ["dev:mallory"], "approve": ["dev:bob"]},
                        OTHER: {"admin": ["dev:root"], "approve": ["dev:bob"]}}}
    v2 = m.save(parse_authz(two), expected_version=1, identity=dev("root"))
    assert v2.change.changed_accounts == [OTHER] and v2.change.admin_entries == {OTHER: "dev:root"}
    two["accounts"][OTHER]["admin"] = []
    with pytest.raises(AuthzDeniedError, match=f"account {OTHER} would have no FIXTURE administrator"):
        m.save(parse_authz(two), expected_version=2, identity=dev("root"))


def test_stale_expectation_cannot_reuse_a_revoked_admin_right(store):
    """The admin check uses exactly the version the commit is conditioned on."""
    m = fixture_world(store)
    m.save(cfg(admin=["dev:root", "dev:ops"], create=["dev:mallory"], approve=["dev:bob"]), expected_version=1,
           identity=dev("root"))
    m.save(cfg(admin=["dev:root"], create=["dev:mallory"], approve=["dev:bob"]), expected_version=2,
           identity=dev("root"))  # ops removed in v3
    grab = cfg(admin=["dev:root", "dev:ops"], create=["dev:mallory"], approve=["dev:carol"])
    with pytest.raises(AuthzConflictError):
        m.save(grab, expected_version=2, identity=dev("ops"))  # ops was admin in v2, but v3 is active
    with pytest.raises(AuthzDeniedError, match="dev:ops lacks 'admin'"):
        m.save(grab, expected_version=3, identity=dev("ops"))
    assert m.get_active().version == 3


# ================================================================ LIVE / FIXTURE isolation and account boundary

def test_live_mode_rejects_development_identities(store):
    m = AuthzManager(store, bootstrap_admin="dev:root")

    class DevPretendingLive:
        mode = OperatingMode.LIVE

        def current(self):
            return DevelopmentIdentityProvider("root").current()

    with pytest.raises(IdentityError, match="development identities are refused"):
        m.save(cfg(admin=["dev:root"]), expected_version=None, identity=DevPretendingLive())
    assert store.active_version("default") is None


def test_fixture_saves_cannot_touch_live_entries_and_vice_versa(store):
    m = AuthzManager(store, bootstrap_admin=f"dev:root,{ADMIN_ROLE}")
    with pytest.raises(AuthzDeniedError, match="FIXTURE save cannot change LIVE"):  # dev identity granting an ARN
        m.save(cfg(admin=["dev:root", ADMIN_ROLE], approve=[APPROVER_ROLE]), expected_version=None, identity=dev("root"))
    m.save(cfg(admin=["dev:root"], approve=["dev:bob"]), expected_version=None, identity=dev("root"))
    m.save(cfg(admin=["dev:root", ADMIN_ROLE], approve=["dev:bob", APPROVER_ROLE]), expected_version=1,
           identity=live(ADMIN_ROLE))
    before = snapshot(m)
    with pytest.raises(AuthzDeniedError, match="FIXTURE save cannot change LIVE"):
        m.save(cfg(admin=["dev:root", ADMIN_ROLE], approve=["dev:bob", APPROVER_ROLE, OPS_ROLE]), expected_version=2,
               identity=dev("root"))
    with pytest.raises(AuthzDeniedError, match="LIVE save cannot change FIXTURE"):
        m.save(cfg(admin=["dev:root", ADMIN_ROLE], approve=["dev:bob", "dev:eve", APPROVER_ROLE]), expected_version=2,
               identity=live(ADMIN_ROLE))
    assert snapshot(m) == before


def test_live_account_boundary(store):
    m = live_world(store)
    before = snapshot(m)
    with pytest.raises(AuthzDeniedError, match="cannot be added or removed"):  # new account entries
        m.save(parse_authz({"accounts": {ACCT: {"admin": [ADMIN_ROLE], "approve": [APPROVER_ROLE]},
                                         OTHER: {"admin": [OTHER_ADMIN]}}}), expected_version=1, identity=live(ADMIN_ROLE))
    with pytest.raises(AuthzDeniedError, match=f"cannot change grants of account\\(s\\) {ACCT}"):
        m.save(cfg(admin=[ADMIN_ROLE], approve=[APPROVER_ROLE, OPS_ROLE]), expected_version=1,
               identity=live(OTHER_ADMIN, account=OTHER))  # a caller from another account
    with pytest.raises(AuthzDeniedError, match="lacks 'admin'"):
        m.save(cfg(admin=[ADMIN_ROLE], approve=[APPROVER_ROLE, OPS_ROLE]), expected_version=1,
               identity=live(APPROVER_ROLE))
    assert snapshot(m) == before
    v2 = m.save(cfg(admin=[ADMIN_ROLE], approve=[APPROVER_ROLE, OPS_ROLE]), expected_version=1,
                identity=live(ADMIN_ROLE, session="other-session"))
    assert v2.change.basis == "ADMIN" and v2.change.admin_entries == {ACCT: ADMIN_ROLE} and v2.change.account_id == ACCT


# ================================================================ failed saves and integrity

def test_failed_saves_leave_the_active_policy_unchanged(store):
    m = fixture_world(store)
    before = snapshot(m)
    attempts = [
        (cfg(admin=["dev:root"], approve=["dev:mallory"]), 1, dev("mallory"), AuthzDeniedError),  # not admin
        (cfg(approve=["dev:bob"]), 1, dev("root"), AuthzDeniedError),  # lockout
        (cfg(admin=["dev:root", ADMIN_ROLE]), 1, dev("root"), AuthzDeniedError),  # cross-mode
        (cfg(admin=["dev:root"], approve=["dev:carol"]), 0, dev("root"), AuthzConflictError),  # stale
        (cfg(admin=["dev:root"], approve=["dev:carol"]), None, dev("root"), AuthzConflictError),  # stale
        (cfg(admin=["dev:root"], create=["dev:mallory"], approve=["dev:bob"]), 1, dev("root"), ValueError),  # unchanged
    ]
    for config, expected, identity, error in attempts:
        with pytest.raises(error):
            m.save(config, expected_version=expected, identity=identity)
    assert snapshot(m) == before


def test_change_record_is_covered_by_the_version_hash(tmp_path):
    store = LocalAuthzStore(tmp_path / "authz")
    m = fixture_world(store)
    path = tmp_path / "authz" / "default" / "VERSION-0000000001.json"
    original = path.read_text(encoding="utf-8")
    for mutate in (
        lambda d: d["change"]["principal"].update(id="dev:mallory"),
        lambda d: d.update(created_by="dev:mallory"),
        lambda d: d["change"].update(basis="ADMIN"),
        lambda d: d.update(initialized_modes=["FIXTURE", "LIVE"]),
        lambda d: d.update(note="edited"),
    ):
        data = json.loads(original)
        mutate(data)
        path.write_text(json.dumps(data), encoding="utf-8")
        with pytest.raises(AuthzIntegrityError):
            m.get_active()
    path.write_text(original, encoding="utf-8")
    assert m.get_active().created_by == "dev:root"


def test_history_detects_a_broken_chain(store):
    m = fixture_world(store)
    m.save(cfg(admin=["dev:root"], approve=["dev:carol"]), expected_version=1, identity=dev("root"))
    assert [v.version for v in m.history()] == [1, 2]
    v1 = m.get_version(1)

    class Swapped:
        def __getattr__(self, name):
            return getattr(store, name)

        def list_versions(self, config_id):
            return [v1, v1.model_copy(update={"version": 2})]  # a substituted second version

    with pytest.raises(AuthzIntegrityError, match="broken at version 2"):
        AuthzManager(Swapped()).history()


@pytest.mark.parametrize("value", ["*", "arn:aws:iam::123456789012:role/*", "dev:Bad", "dev:a,dev:b"])
def test_invalid_bootstrap_settings_are_rejected(value):
    with pytest.raises(ValueError):
        parse_bootstrap_admins(value)


# ================================================================ CLI

def _cli_save(tmp_path, config, *extra):
    path = tmp_path / "authz.json"
    path.write_text(json.dumps(config), encoding="utf-8")
    return main(["authz", "save", str(path), "--authz-store", str(tmp_path / "az"), *extra])


def test_cli_author_flag_cannot_spoof_identity(tmp_path, capsys, monkeypatch):
    config = {"accounts": {ACCT: {"admin": ["dev:root"], "approve": ["dev:bob"]}}}
    with pytest.raises(SystemExit) as exc:  # the label flag no longer exists
        _cli_save(tmp_path, config, "--expected-version", "0", "--dev-identity", "root", "--author", "dev:ceo")
    assert exc.value.code == 2
    monkeypatch.setenv(BOOTSTRAP_ENV, "dev:root")
    assert _cli_save(tmp_path, config, "--expected-version", "0", "--dev-identity", "root",
                     "--note", "author: dev:ceo") == 0
    saved = json.loads(capsys.readouterr().out)
    assert saved["created_by"] == "dev:root" and saved["change"]["principal"] == {"actor_type": "DEVELOPMENT", "id": "dev:root"}
    assert saved["note"] == "author: dev:ceo"  # free text stays free text, never identity


def test_cli_bootstrap_and_admin_flow(tmp_path, capsys, monkeypatch):
    config = {"accounts": {ACCT: {"admin": ["dev:root"], "approve": ["dev:bob"]}}}
    assert _cli_save(tmp_path, config, "--expected-version", "0", "--dev-identity", "root") == 3
    assert "no FIXTURE bootstrap administrator is configured" in capsys.readouterr().err
    monkeypatch.setenv(BOOTSTRAP_ENV, "dev:root")
    assert _cli_save(tmp_path, config, "--expected-version", "0", "--dev-identity", "root") == 0
    capsys.readouterr()
    monkeypatch.setenv(BOOTSTRAP_ENV, "dev:eve")  # changing the setting later grants nothing
    grab = {"accounts": {ACCT: {"admin": ["dev:root", "dev:eve"], "approve": ["dev:carol"]}}}
    assert _cli_save(tmp_path, grab, "--expected-version", "1", "--dev-identity", "eve") == 3
    assert "dev:eve lacks 'admin'" in capsys.readouterr().err
    assert _cli_save(tmp_path, grab, "--expected-version", "1", "--dev-identity", "root") == 0
    capsys.readouterr()
    assert main(["authz", "history", "--authz-store", str(tmp_path / "az")]) == 0
    history = json.loads(capsys.readouterr().out)
    assert [(h["version"], h["basis"], h["created_by"], h["mode"]) for h in history] == \
        [(1, "BOOTSTRAP", "dev:root", "FIXTURE"), (2, "ADMIN", "dev:root", "FIXTURE")]


def test_cli_live_save_never_takes_a_dev_identity(tmp_path):
    with pytest.raises(SystemExit) as exc:
        _cli_save(tmp_path, {"accounts": {}}, "--expected-version", "0", "--mode", "live", "--dev-identity", "root")
    assert exc.value.code == 2
