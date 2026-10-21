"""Test helpers: seed and edit allow-lists through the real, identity-bound AuthzManager.save().

Tests don't bypass H1: every version is written by a verified identity. Each account that has
entries of a mode gets a dedicated seed administrator for that mode (appended to its 'admin' list
if missing), which bootstraps the mode and later makes test edits.

M3: every LIVE (ARN) entry without an explicit person binding gets one automatically - an IAM user
binds to a person named after the user, a role to a single_person_role person named after the role.
Tests that need specific people pass explicit "bindings".
"""

import copy
import hashlib
import re

from cloudguard.governance.remediation.authz import AuthzManager, LocalAuthzStore, parse_authz
from cloudguard.governance.remediation.identity import DevelopmentIdentityProvider, StsCallerIdentityProvider
from cloudguard.governance.remediation.models import OperatingMode

SEED_DEV_NAME = "authz-admin"
SEED_DEV_ADMIN = f"dev:{SEED_DEV_NAME}"
SEED_ROLE = "AuthzSeedAdmin"


def seed_role_arn(account: str) -> str:
    return f"arn:aws:iam::{account}:role/{SEED_ROLE}"


def fake_unique_id(prefix: str, name: str) -> str:
    return prefix + hashlib.sha256(name.encode()).hexdigest()[:17].upper()


def fake_user_id(arn: str, account: str) -> str:
    """A realistic GetCallerIdentity UserId for `arn` (deterministic per user/role name)."""
    if m := re.search(r":assumed-role/([^/]+)/(.+)$", arn):
        return f"{fake_unique_id('AROA', m[1])}:{m[2]}"
    if m := re.search(r":user/(?:.*/)?([^/]+)$", arn):
        return fake_unique_id("AIDA", m[1])
    return account  # root


class FakeSts:
    def __init__(self, arn: str, account: str, user_id: str | None = None):
        self.arn, self.account = arn, account
        self.user_id = user_id or fake_user_id(arn, account)

    def get_caller_identity(self):
        return {"Arn": self.arn, "Account": self.account, "UserId": self.user_id}


def live_identity(arn: str, account: str, clock=None, user_id: str | None = None) -> StsCallerIdentityProvider:
    kw = {"clock": clock} if clock else {}
    return StsCallerIdentityProvider(FakeSts(arn, account, user_id), **kw)


def person_for(entry: str) -> str:
    name = entry.rsplit("/", 1)[-1]
    return re.sub(r"[^a-z0-9._-]", "-", name.lower())


def with_bindings(config: dict) -> dict:
    """Add a person binding for every LIVE entry that lacks one (M3)."""
    cfg = copy.deepcopy(config)
    for grants in cfg["accounts"].values():
        bindings = grants.setdefault("bindings", [])
        bound = {re.sub(r":(user|role)/(?:.*/)?([^/]+)$", r":\1/\2", b["principal"]) for b in bindings}
        for key, lst in list(grants.items()):
            if key in ("bindings", "separation"):
                continue
            for e in lst:
                canon = re.sub(r":(user|role)/(?:.*/)?([^/]+)$", r":\1/\2", e)
                if e.startswith("dev:") or canon in bound or e.endswith(":root"):
                    continue
                kind = "iam_user" if ":user/" in e else "single_person_role"
                bindings.append({"kind": kind, "principal": e, "person": person_for(e)})
                bound.add(canon)
    return cfg


def seed_admin_identity(mode: OperatingMode, account: str | None = None):
    if mode == OperatingMode.FIXTURE:
        return DevelopmentIdentityProvider(SEED_DEV_NAME)
    return live_identity(f"arn:aws:sts::{account}:assumed-role/{SEED_ROLE}/seed", account)


def with_seed_admins(config: dict) -> dict:
    cfg = copy.deepcopy(config)
    for account, grants in cfg["accounts"].items():
        entries = [e for k, lst in grants.items() if k not in ("bindings", "separation") for e in lst]
        admin = grants.setdefault("admin", [])
        if any(e.startswith("dev:") for e in entries) and SEED_DEV_ADMIN not in admin:
            admin.append(SEED_DEV_ADMIN)
        if any(not e.startswith("dev:") for e in entries) and seed_role_arn(account) not in admin:
            admin.append(seed_role_arn(account))
    return with_bindings(cfg)


def _modes(config) -> list[OperatingMode]:
    return [m for m in (OperatingMode.FIXTURE, OperatingMode.LIVE) if config.mode_view(m)]


def _live_account(config) -> str:
    accounts = list(config.mode_view(OperatingMode.LIVE))
    assert len(accounts) == 1, "seeded LIVE configs are single-account"
    return accounts[0]


def _only(config, mode):
    """The config restricted to one mode's entries (what a first, single-mode bootstrap saves)."""
    data = config.model_dump(mode="json")
    for grants in data["accounts"].values():
        for p, lst in list(grants.items()):
            if p in ("bindings", "separation"):
                continue
            grants[p] = [e for e in lst if e.startswith("dev:") == (mode == OperatingMode.FIXTURE)]
        if mode == OperatingMode.FIXTURE:
            grants["bindings"] = []
            grants["separation"] = {**grants["separation"], "live_executor_must_differ_from_requester": False}
    data["accounts"] = {a: g for a, g in data["accounts"].items()
                        if any(v for k, v in g.items() if k not in ("separation",))}
    return parse_authz(data)


def seeded_authz(config: dict, store=None) -> AuthzManager:
    """An AuthzManager whose active version is `config` (plus seed admins), bootstrapped mode by mode."""
    parsed = parse_authz(with_seed_admins(config))
    modes = _modes(parsed)
    bootstrap = [SEED_DEV_ADMIN] if OperatingMode.FIXTURE in modes else []
    if OperatingMode.LIVE in modes:
        bootstrap.append(seed_role_arn(_live_account(parsed)))
    manager = AuthzManager(store if store is not None else LocalAuthzStore(), bootstrap_admin=bootstrap)
    version = None
    for i, mode in enumerate(modes):
        target = parsed if i == len(modes) - 1 else _only(parsed, mode)
        account = _live_account(parsed) if mode == OperatingMode.LIVE else None
        version = manager.save(target, expected_version=version, identity=seed_admin_identity(mode, account)).version
    return manager


def edit_authz(manager: AuthzManager, config: dict, mode: OperatingMode, note: str = ""):
    """Save an edited config as the seed administrator of `mode`."""
    parsed = parse_authz(with_seed_admins(config))
    account = _live_account(parsed) if mode == OperatingMode.LIVE else None
    current = manager.store.active_version(manager.config_id)
    return manager.save(parsed, expected_version=current, identity=seed_admin_identity(mode, account), note=note)
