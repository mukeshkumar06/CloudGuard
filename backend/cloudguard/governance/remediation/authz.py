"""Versioned, account-level authorisation allow-list (Checkpoint 4.2; administration secured in Batch 1 / H1).

Configuration (JSON), see docs/authorization.md:
    {
      "schema_version": "cloudguard.authz/v1",
      "accounts": {
        "<12-digit account id>": {
          "admin":   ["arn:aws:iam::<acct>:role/<name>", ...],
          "create":  ["arn:aws:iam::<acct>:role/<name>", "arn:aws:iam::<acct>:user/<name>", ...],
          "approve": [...],
          "reject":  [...],
          "execute": [...]
        }
      }
    }
Entries are exact canonical principals: IAM user or role ARNs of that same account (paths ignored),
the account root, or "dev:<name>" development identities (honoured in FIXTURE mode only).
Wildcards are not supported. Nothing is hardcoded: with no configuration, every request is denied.

Administration (who may save a new version):
- Every save is made by a principal from an IdentityProvider - never by a caller-supplied label.
  created_by and the AuthzChange audit record are derived from that verified principal.
- LIVE and FIXTURE entries are administered separately: a save in one mode may not change the
  other mode's entries ("dev:" entries are FIXTURE entries; ARN and root entries are LIVE entries).
- The first save of a mode is a one-time bootstrap by an explicitly configured bootstrap
  administrator (BOOTSTRAP_ENV). Once a mode is initialised it stays initialised (recorded in every
  later version), and the bootstrap setting is never consulted for that mode again.
- After bootstrap, a change to an account's entries requires 'admin' on that account in the active
  version. LIVE callers may only change their own account, and LIVE accounts can't be added or
  removed after bootstrap.

Person bindings (M3 - separation of duties):
- Every LIVE (ARN) entry must be bound to exactly one PERSON in its account's "bindings":
    iam_user                 an IAM user ARN -> person
    single_person_role       a role ARN -> person; the ADMINISTRATOR ASSERTS that the role's trust
                             policy admits only that person
    session_attributed_role  a role ARN whose session name identifies the person; the ADMINISTRATOR
                             ASSERTS that the trust policy enforces it (session_source "iam-username":
                             sts:RoleSessionName = ${aws:username}; "identity-center": sessions named by
                             IAM Identity Center). The application can't verify either assertion.
  Optional unique_id pins the IAM unique id (AIDA... / AROA...) so a deleted and recreated user or
  role with the same name doesn't inherit the binding.
- Root can't be bound, so root can't appear in LIVE entries. Unbound principals fail closed.
- A person may not hold both 'admin' and 'approve' on the same account.
- FIXTURE identities map implicitly: dev:<name> -> person <name>.
- Bindings and the LIVE separation setting are LIVE data: only LIVE saves may change them.

Every saved configuration is an immutable version (id, version, content hash, record hash) with
optimistic concurrency; the conditional write makes concurrent saves - including concurrent
bootstraps - fail all but one.
"""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Iterable
from datetime import UTC, datetime
from pathlib import Path
from typing import Literal, Protocol

from botocore.exceptions import ClientError
from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator, model_validator

from cloudguard.governance.remediation.identity import (
    IdentityError,
    IdentityProvider,
    Principal,
    PrincipalKind,
    require_identity_for_mode,
)
from cloudguard.governance.remediation.models import (
    PERSON_ID_PATTERN,
    IdentityEvidence,
    Actor,
    ActorType,
    AuthorizationBasis,
    AuthzRef,
    OperatingMode,
    Permission,
    _utc,
    canonical_digest,
)
from cloudguard.governance.remediation.repository import _DiskFiles, _MemoryFiles

AUTHZ_SCHEMA = "cloudguard.authz/v1"
DEFAULT_AUTHZ_ID = "default"
BOOTSTRAP_ENV = "CLOUDGUARD_AUTHZ_BOOTSTRAP_ADMIN"
_ENTRY = re.compile(
    r"^(?:dev:[a-z0-9][a-z0-9._-]{0,62}"
    r"|arn:(?:aws|aws-cn|aws-us-gov):iam::(?P<acct>\d{12}):(?:root|(?P<kind>user|role)/(?:[\w+=,.@-]+/)*(?P<name>[\w+=,.@-]+)))$"
)


class _Frozen(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")


class AuthzValidationError(ValueError):
    def __init__(self, errors: list[str]):
        self.errors = errors
        super().__init__("invalid authorization config: " + "; ".join(errors))


class AuthzConflictError(RuntimeError):
    pass


class AuthzNotFoundError(LookupError):
    pass


class AuthzIntegrityError(RuntimeError):
    pass


class AuthzDeniedError(PermissionError):
    """The verified caller may not make this allow-list change. Nothing was written."""


class PersonResolutionError(IdentityError):
    """The principal does not resolve to exactly one person (M3): fail closed."""


_SESSION_NAME = re.compile(r"^[\w+=,.@-]{2,64}$")
_UNIQUE_ID = re.compile(r"^(AIDA|AROA)[A-Z0-9]{12,124}$")


def _canonical_entry(entry: str) -> str:
    """Drop IAM paths so entries compare like canonical principals."""
    m = _ENTRY.match(entry)
    if m and m["kind"]:
        return f"{entry.split(':iam::')[0]}:iam::{m['acct']}:{m['kind']}/{m['name']}"
    return entry


def entry_mode(entry: str) -> OperatingMode:
    return OperatingMode.FIXTURE if entry.startswith("dev:") else OperatingMode.LIVE


class PersonBinding(_Frozen):
    """Binds one IAM principal to a person (M3). Role bindings rest on administrator assertions
    about IAM trust policies that the application does not verify."""

    kind: Literal["iam_user", "single_person_role", "session_attributed_role"]
    principal: str  # exact IAM user or role ARN (paths ignored)
    person: str | None = None  # iam_user, single_person_role
    sessions: dict[str, str] = Field(default_factory=dict)  # session_attributed_role: session name -> person
    session_source: Literal["iam-username", "identity-center"] | None = None  # session_attributed_role only
    unique_id: str | None = None  # optional pin: AIDA... (user) / AROA... (role)

    @model_validator(mode="after")
    def _shape(self) -> PersonBinding:
        m = _ENTRY.match(self.principal)
        expected = "user" if self.kind == "iam_user" else "role"
        if not m or m["kind"] != expected:
            raise ValueError(f"{self.kind} bindings name an exact IAM {expected} ARN, not {self.principal!r}")
        if self.kind == "session_attributed_role":
            if self.person is not None or not self.sessions or self.session_source is None:
                raise ValueError("session_attributed_role bindings map sessions to people and state session_source")
            for session, person in self.sessions.items():
                if not _SESSION_NAME.match(session) or not PERSON_ID_PATTERN.match(person):
                    raise ValueError(f"invalid session mapping {session!r} -> {person!r}")
        else:
            if self.person is None or self.sessions or self.session_source is not None:
                raise ValueError(f"{self.kind} bindings name exactly one person")
            if not PERSON_ID_PATTERN.match(self.person):
                raise ValueError(f"invalid person id {self.person!r}")
        if self.unique_id is not None:
            prefix = "AIDA" if self.kind == "iam_user" else "AROA"
            if not _UNIQUE_ID.match(self.unique_id) or not self.unique_id.startswith(prefix):
                raise ValueError(f"unique_id for {self.kind} must be an IAM unique id starting with {prefix}")
        return self

    @property
    def canonical_principal(self) -> str:
        return _canonical_entry(self.principal)

    def people(self) -> set[str]:
        return set(self.sessions.values()) if self.kind == "session_attributed_role" else {self.person}


class SeparationSettings(_Frozen):
    """Configurable separation of duties (M3). Approver != requester is mandatory in LIVE and
    executor != approver is mandatory in every mode; these settings only add rules."""

    live_executor_must_differ_from_requester: bool = False
    fixture_approver_must_differ_from_requester: bool = False  # default False keeps fixture self-approval
    fixture_executor_must_differ_from_requester: bool = False

    def executor_must_differ_from_requester(self, mode: OperatingMode) -> bool:
        return (self.live_executor_must_differ_from_requester if mode == OperatingMode.LIVE
                else self.fixture_executor_must_differ_from_requester)


class AccountGrants(_Frozen):
    admin: list[str] = Field(default_factory=list)  # Batch 1 (H1): may change this account's entries
    create: list[str] = Field(default_factory=list)
    approve: list[str] = Field(default_factory=list)
    reject: list[str] = Field(default_factory=list)
    execute: list[str] = Field(default_factory=list)  # Checkpoint 4.3; optional, defaults to nobody
    bindings: list[PersonBinding] = Field(default_factory=list)  # M3: LIVE principal -> person
    separation: SeparationSettings = Field(default_factory=SeparationSettings)  # M3

    def entries(self, permission: Permission) -> list[str]:
        return {Permission.CREATE: self.create, Permission.APPROVE: self.approve, Permission.REJECT: self.reject,
                Permission.EXECUTE: self.execute, Permission.ADMIN: self.admin}[permission]

    def binding_for(self, canonical_principal: str) -> PersonBinding | None:
        return next((b for b in self.bindings if b.canonical_principal == canonical_principal), None)

    def people_for_entry(self, entry: str) -> set[str]:
        """Every person an allow-list entry can act as (empty if unbound)."""
        if entry.startswith("dev:"):
            return {entry[4:]}
        binding = self.binding_for(_canonical_entry(entry))
        return binding.people() if binding else set()


class AuthorizationConfig(_Frozen):
    schema_version: str = AUTHZ_SCHEMA
    accounts: dict[str, AccountGrants]

    @field_validator("schema_version")
    @classmethod
    def _schema(cls, v: str) -> str:
        if v != AUTHZ_SCHEMA:
            raise ValueError(f"unsupported authz schema {v!r}")
        return v

    @model_validator(mode="after")
    def _validate(self) -> AuthorizationConfig:
        for account, grants in self.accounts.items():
            if not re.fullmatch(r"\d{12}", account):
                raise ValueError(f"account id {account!r} must be 12 digits")
            for permission in Permission:
                entries = grants.entries(permission)
                canon = [_canonical_entry(e) for e in entries]
                if len(set(canon)) != len(canon):
                    raise ValueError(f"{account}.{permission.value}: duplicate entries")
                for e in entries:
                    m = _ENTRY.match(e)
                    if not m:
                        raise ValueError(f"{account}.{permission.value}: unsupported entry {e!r} (no wildcards; "
                                         "use exact IAM user/role ARNs, root, or dev:<name>)")
                    if m["acct"] and m["acct"] != account:
                        raise ValueError(f"{account}.{permission.value}: {e!r} belongs to another account")
            seen: set[str] = set()
            for b in grants.bindings:  # M3: person bindings
                if _ENTRY.match(b.principal)["acct"] != account:
                    raise ValueError(f"{account}.bindings: {b.principal!r} belongs to another account")
                if b.canonical_principal in seen:
                    raise ValueError(f"{account}.bindings: {b.principal!r} is bound more than once")
                seen.add(b.canonical_principal)
            for permission in Permission:
                for e in grants.entries(permission):
                    if entry_mode(e) == OperatingMode.LIVE and not grants.people_for_entry(e):
                        raise ValueError(f"{account}.{permission.value}: {e!r} is not bound to a person "
                                         "(LIVE entries need a binding; root can't be bound)")
            admins = set().union(*(grants.people_for_entry(e) for e in grants.admin))
            approvers = set().union(*(grants.people_for_entry(e) for e in grants.approve))
            if admins & approvers:
                raise ValueError(f"{account}: person(s) {sorted(admins & approvers)} hold both 'admin' and 'approve'")
        return self

    def content_hash(self) -> str:
        canonical = json.dumps(self.model_dump(mode="json"), sort_keys=True, separators=(",", ":"))
        return "sha256:" + hashlib.sha256(canonical.encode()).hexdigest()

    def mode_view(self, mode: OperatingMode) -> dict[str, dict]:
        """Everything one mode owns, per account: its entries in stored order, plus (LIVE) the person
        bindings and the LIVE separation setting, or (FIXTURE) the FIXTURE separation settings.
        Accounts with nothing of that mode are omitted."""
        out = {}
        for account, grants in self.accounts.items():
            view: dict = {p.value: [e for e in grants.entries(p) if entry_mode(e) == mode] for p in Permission}
            sep = grants.separation
            if mode == OperatingMode.LIVE:
                view["bindings"] = [b.model_dump(mode="json") for b in grants.bindings]
                view["separation"] = {"live_executor_must_differ_from_requester":
                                      sep.live_executor_must_differ_from_requester}
            else:
                view["separation"] = {
                    "fixture_approver_must_differ_from_requester": sep.fixture_approver_must_differ_from_requester,
                    "fixture_executor_must_differ_from_requester": sep.fixture_executor_must_differ_from_requester}
            if any(view[p.value] for p in Permission) or view.get("bindings") or any(view["separation"].values()):
                out[account] = view
        return out


def resolve_person(config: AuthorizationConfig, principal: Principal, account_id: str,
                   mode: OperatingMode) -> tuple[str, str]:
    """(person id, binding kind) for `principal` acting on `account_id` (M3). Fails closed with
    PersonResolutionError. Role bindings rest on administrator assertions the app can't verify."""
    if mode == OperatingMode.FIXTURE:
        if principal.kind != PrincipalKind.DEVELOPMENT:
            raise PersonResolutionError("FIXTURE mode resolves development identities only")
        return principal.canonical_id.removeprefix("dev:"), "development"
    if principal.kind == PrincipalKind.DEVELOPMENT or principal.account_id != account_id:
        raise PersonResolutionError(f"{principal.canonical_id} can't act as a person on account {account_id}")
    grants = config.accounts.get(account_id)
    binding = grants.binding_for(principal.canonical_id) if grants else None
    if binding is None:
        raise PersonResolutionError(f"{principal.canonical_id} is not bound to a person on account {account_id}; "
                                    "LIVE governance actions require a person binding")
    expected = {PrincipalKind.IAM_USER: {"iam_user"},
                PrincipalKind.ASSUMED_ROLE: {"single_person_role", "session_attributed_role"}}.get(principal.kind, set())
    if binding.kind not in expected:
        raise PersonResolutionError(f"a {binding.kind} binding can't identify a {principal.kind} principal")
    if binding.unique_id is not None and principal.unique_id != binding.unique_id:
        raise PersonResolutionError(f"STS unique id of {principal.canonical_id} does not match the pinned id; "
                                    "the user or role may have been deleted and recreated")
    if binding.kind == "session_attributed_role":
        person = binding.sessions.get(principal.session_name or "")
        if person is None:
            raise PersonResolutionError(f"session {principal.session_name!r} of {principal.canonical_id} is not "
                                        "attributed to any person")
        return person, binding.kind
    return binding.person, binding.kind


def parse_authz(data: dict | str) -> AuthorizationConfig:
    try:
        if isinstance(data, str):
            return AuthorizationConfig.model_validate_json(data)
        return AuthorizationConfig.model_validate(data)
    except ValidationError as exc:
        raise AuthzValidationError([f"{'.'.join(str(p) for p in e['loc']) or 'config'}: {e['msg']}"
                                    for e in exc.errors()]) from None


def parse_bootstrap_admins(value: str | Iterable[str] | None) -> dict[OperatingMode, str]:
    """Bootstrap administrators: at most one canonical principal per mode (comma-separated in the
    environment variable). An ARN bootstraps LIVE; a dev:<name> bootstraps FIXTURE."""
    if value is None:
        return {}
    items = [v.strip() for v in (value.split(",") if isinstance(value, str) else value) if v and v.strip()]
    out: dict[OperatingMode, str] = {}
    for item in items:
        if not _ENTRY.match(item):
            raise ValueError(f"invalid bootstrap administrator {item!r}: use an exact IAM user/role ARN, root, "
                             "or dev:<name>")
        mode = entry_mode(item)
        if mode in out:
            raise ValueError(f"more than one {mode.value} bootstrap administrator configured")
        out[mode] = _canonical_entry(item)
    return out


# ---------------------------------------------------------------- versions and their audit record


class AuthzChange(_Frozen):
    """Who made an allow-list change and on what authority. Every identity field is copied from
    the verified IdentityProvider principal at save time; it is covered by the version's
    record_hash."""

    mode: OperatingMode
    principal: Actor
    principal_arn: str | None  # exact STS caller ARN (LIVE only)
    account_id: str | None  # STS-verified account (LIVE only)
    authn_method: str
    authenticated_at: datetime
    basis: Literal["BOOTSTRAP", "ADMIN"]
    bootstrap_admin: str | None = None  # BOOTSTRAP: the configured administrator that matched
    person_id: str  # M3: the person who made the change (resolved through the person bindings)
    admin_entries: dict[str, str] = Field(default_factory=dict)  # ADMIN: account -> matched admin entry
    authorized_by_version: int | None  # active version the change was authorised against
    authorized_by_record_hash: str | None
    changed_accounts: list[str]

    @field_validator("authenticated_at")
    @classmethod
    def _utc_only(cls, v: datetime) -> datetime:
        return _utc(v)

    @model_validator(mode="after")
    def _shape(self) -> AuthzChange:
        if self.mode == OperatingMode.LIVE:
            if not (self.principal.actor_type == ActorType.AWS_PRINCIPAL and self.principal_arn and self.account_id
                    and self.authn_method == "sts:GetCallerIdentity"):
                raise ValueError("LIVE changes are made by STS-verified AWS principals")
        elif not (self.principal.actor_type == ActorType.DEVELOPMENT and self.principal_arn is None
                  and self.account_id is None and self.authn_method == "development"):
            raise ValueError("FIXTURE changes are made by development identities")
        if (self.basis == "BOOTSTRAP") != (self.bootstrap_admin is not None):
            raise ValueError("exactly BOOTSTRAP changes name the bootstrap administrator")
        if self.basis == "BOOTSTRAP" and (self.admin_entries or self.bootstrap_admin != self.principal.id):
            raise ValueError("a bootstrap change is made by the bootstrap administrator itself")
        if self.basis == "ADMIN" and set(self.admin_entries) != set(self.changed_accounts):
            raise ValueError("an ADMIN change records the matched admin entry for every changed account")
        if not PERSON_ID_PATTERN.match(self.person_id):
            raise ValueError("person ids are 1-63 chars of [a-z0-9._-]")
        if (self.authorized_by_version is None) != (self.authorized_by_record_hash is None):
            raise ValueError("authorizing version and its hash go together")
        if not self.changed_accounts or self.changed_accounts != sorted(set(self.changed_accounts)):
            raise ValueError("changed accounts must be a non-empty sorted set")
        return self


class AuthzVersion(_Frozen):
    config_id: str
    version: int = Field(ge=1)
    config: AuthorizationConfig
    content_hash: str  # configuration only (what decisions reference)
    created_at: datetime
    created_by: str  # canonical id of the verified principal (== change.principal.id); never a label
    note: str = Field(default="", max_length=1000)
    change: AuthzChange
    initialized_modes: list[OperatingMode]  # sticky: a mode once initialised never bootstraps again
    previous_record_hash: str | None  # links each version to the one it replaced
    record_hash: str = ""  # covers every field above

    @field_validator("created_at")
    @classmethod
    def _utc_only(cls, v: datetime) -> datetime:
        return _utc(v)

    @model_validator(mode="after")
    def _verify(self) -> AuthzVersion:
        if self.content_hash != self.config.content_hash():
            raise ValueError("authz content_hash does not match the configuration")
        if self.created_by != self.change.principal.id:
            raise ValueError("created_by must be the verified principal that made the change")
        if self.change.mode not in self.initialized_modes or \
                self.initialized_modes != sorted(set(self.initialized_modes)):
            raise ValueError("initialized modes must be a sorted set that includes the change's mode")
        if (self.version == 1) != (self.previous_record_hash is None):
            raise ValueError("only version 1 has no previous version")
        if self.record_hash and self.record_hash != self.compute_hash():
            raise ValueError("authz record_hash does not match the version")
        if not self.record_hash:
            raise ValueError("authz versions must be sealed")
        return self

    def compute_hash(self) -> str:
        return canonical_digest(self.model_dump(mode="json", exclude={"record_hash"}))

    @classmethod
    def sealed(cls, **fields) -> AuthzVersion:
        draft = cls.model_construct(**{**fields, "record_hash": ""})
        data = draft.model_dump(mode="json")
        data["record_hash"] = canonical_digest({k: v for k, v in data.items() if k != "record_hash"})
        return cls.model_validate(data)

    @property
    def ref(self) -> AuthzRef:
        return AuthzRef(config_id=self.config_id, version=self.version, content_hash=self.content_hash)


# ---------------------------------------------------------------- stores


class AuthzStore(Protocol):
    def active_version(self, config_id: str) -> int | None: ...
    def get_version(self, config_id: str, version: int) -> AuthzVersion | None: ...
    def list_versions(self, config_id: str) -> list[AuthzVersion]: ...
    def commit(self, new: AuthzVersion, expected_active: int | None) -> None: ...


class LocalAuthzStore:
    """In memory, or files under `directory`: <config_id>/VERSION-<n>.json + ACTIVE.json.

    commit() runs under an exclusive lock (a lock file on disk, so separate processes serialise
    too) and re-reads the active pointer from storage, so a stale expectation always conflicts.
    ACTIVE.json is the commit point: a version file above the active version is an uncommitted
    leftover of a crashed save and is never served."""

    def __init__(self, directory: str | Path | None = None):
        self.files = _DiskFiles(Path(directory)) if directory else _MemoryFiles()

    @staticmethod
    def _base(config_id: str) -> str:
        if not re.fullmatch(r"[A-Za-z0-9_-]{1,64}", config_id):
            raise ValueError("invalid authz config id")
        return f"{config_id}/"

    def _version_name(self, config_id: str, version: int) -> str:
        return self._base(config_id) + f"VERSION-{version:010d}.json"

    def active_version(self, config_id):
        text = self.files.read(self._base(config_id) + "ACTIVE.json")
        if text is None:
            return None
        try:
            return int(json.loads(text)["version"])
        except (KeyError, ValueError, TypeError) as exc:
            raise AuthzIntegrityError(f"stored authz pointer for {config_id} is corrupt: {exc}") from None

    def get_version(self, config_id, version):
        active = self.active_version(config_id)
        if active is None or not 1 <= version <= active:
            return None
        text = self.files.read(self._version_name(config_id, version))
        if text is None:
            raise AuthzIntegrityError(f"authz version {version} of {config_id} is missing")
        try:
            found = AuthzVersion.model_validate_json(text)
        except ValidationError as exc:
            raise AuthzIntegrityError(f"stored authz version {version} failed verification: {exc}") from None
        if (found.config_id, found.version) != (config_id, version):
            raise AuthzIntegrityError(f"stored authz version {version} of {config_id} contains another version")
        return found

    def list_versions(self, config_id):
        active = self.active_version(config_id) or 0
        return [self.get_version(config_id, v) for v in range(1, active + 1)]

    def commit(self, new, expected_active) -> None:
        with self.files.lock(f"authz-{new.config_id}"):
            current = self.active_version(new.config_id)
            if current != expected_active or new.version != (expected_active or 0) + 1:
                raise AuthzConflictError(f"authz config '{new.config_id}' active version is {current}, "
                                         f"expected {expected_active}")
            self.files.replace(self._version_name(new.config_id, new.version), new.model_dump_json())
            self.files.replace(self._base(new.config_id) + "ACTIVE.json", json.dumps({"version": new.version}))


class DynamoAuthzStore:
    """PK = AUTHZ#<config_id>; SK = VERSION#<n> (immutable) | ACTIVE (pointer). No TTL."""

    def __init__(self, client, table_name: str):
        self.client = client
        self.table = table_name

    def _get(self, config_id, sk):
        return self.client.get_item(TableName=self.table, ConsistentRead=True,
                                    Key={"PK": {"S": f"AUTHZ#{config_id}"}, "SK": {"S": sk}}).get("Item")

    @staticmethod
    def _version(item) -> AuthzVersion:
        try:
            return AuthzVersion.model_validate_json(item["version_json"]["S"])
        except (KeyError, ValidationError) as exc:
            raise AuthzIntegrityError(f"stored authz version failed verification: {exc}") from None

    def active_version(self, config_id):
        item = self._get(config_id, "ACTIVE")
        return int(item["version"]["N"]) if item else None

    def get_version(self, config_id, version):
        item = self._get(config_id, f"VERSION#{version:010d}")
        return self._version(item) if item else None

    def list_versions(self, config_id):
        out = []
        for page in self.client.get_paginator("query").paginate(
                TableName=self.table, ConsistentRead=True, KeyConditionExpression="PK = :pk AND begins_with(SK, :v)",
                ExpressionAttributeValues={":pk": {"S": f"AUTHZ#{config_id}"}, ":v": {"S": "VERSION#"}}):
            out += [self._version(i) for i in page.get("Items", [])]
        return out

    def commit(self, new, expected_active) -> None:
        if new.version != (expected_active or 0) + 1:
            raise AuthzConflictError(f"authz version {new.version} does not follow {expected_active}")
        pk = {"S": f"AUTHZ#{new.config_id}"}
        active_cond = ({"ConditionExpression": "attribute_not_exists(PK)"} if expected_active is None else
                       {"ConditionExpression": "#v = :e", "ExpressionAttributeNames": {"#v": "version"},
                        "ExpressionAttributeValues": {":e": {"N": str(expected_active)}}})
        try:
            self.client.transact_write_items(TransactItems=[
                {"Put": {"TableName": self.table, "ConditionExpression": "attribute_not_exists(PK)", "Item": {
                    "PK": pk, "SK": {"S": f"VERSION#{new.version:010d}"}, "entity": {"S": "AuthzVersion"},
                    "version_json": {"S": new.model_dump_json()}}}},
                {"Put": {"TableName": self.table, **active_cond, "Item": {
                    "PK": pk, "SK": {"S": "ACTIVE"}, "entity": {"S": "AuthzActive"},
                    "version": {"N": str(new.version)}, "content_hash": {"S": new.content_hash},
                    "record_hash": {"S": new.record_hash}}}},
            ])
        except ClientError as exc:
            if exc.response.get("Error", {}).get("Code") == "TransactionCanceledException":
                raise AuthzConflictError(f"authz config '{new.config_id}' changed concurrently") from None
            raise


# ---------------------------------------------------------------- manager and evaluation


class AuthzDecision(_Frozen):
    allowed: bool
    basis: AuthorizationBasis | None
    reason: str


class AuthzManager:
    def __init__(self, store: AuthzStore, config_id: str = DEFAULT_AUTHZ_ID, *,
                 bootstrap_admin: str | Iterable[str] | None = None):
        self.store = store
        self.config_id = config_id
        self.bootstrap_admins = parse_bootstrap_admins(bootstrap_admin)

    def get_active(self) -> AuthzVersion:
        version = self.store.active_version(self.config_id)
        if version is None:
            raise AuthzNotFoundError("no authorization configuration has been saved; every request is denied")
        found = self.store.get_version(self.config_id, version)
        if found is None:
            raise AuthzIntegrityError(f"active authz version {version} is missing")
        return found

    def get_version(self, version: int) -> AuthzVersion:
        found = self.store.get_version(self.config_id, version)
        if found is None:
            raise AuthzNotFoundError(f"authz version {version} not found")
        return found

    def history(self) -> list[AuthzVersion]:
        """All versions, verified as one unbroken chain (contiguous numbers, linked record hashes)."""
        versions = sorted(self.store.list_versions(self.config_id), key=lambda v: v.version)
        previous = None
        for expected, v in enumerate(versions, start=1):
            if v.version != expected or v.previous_record_hash != (previous.record_hash if previous else None):
                raise AuthzIntegrityError(f"authz history of {self.config_id} is broken at version {expected}")
            previous = v
        return versions

    # ---- administration (H1)

    def save(self, config: AuthorizationConfig, *, expected_version: int | None, identity: IdentityProvider,
             note: str = "") -> AuthzVersion:
        """Save a new version as the verified caller of `identity`. Every check runs before the
        single conditional commit, so a refused or conflicting save never changes the active version."""
        config = parse_authz(config.model_dump(mode="json"))  # re-validate
        principal = identity.current()
        mode = identity.mode
        require_identity_for_mode(principal, mode)

        current = self.store.active_version(self.config_id)
        if expected_version != current:
            raise AuthzConflictError(f"authz config '{self.config_id}' active version is {current}, "
                                     f"expected {expected_version}")
        active = self.get_version(current) if current is not None else None
        if active is not None and active.content_hash == config.content_hash():
            raise ValueError("authorization config is unchanged")
        change = self._authorize_change(principal, mode, active, config)

        initialized = sorted({*(active.initialized_modes if active else []), mode})
        new = AuthzVersion.sealed(
            config_id=self.config_id, version=(current or 0) + 1, config=config, content_hash=config.content_hash(),
            created_at=datetime.now(UTC), created_by=principal.canonical_id, note=note, change=change,
            initialized_modes=initialized, previous_record_hash=active.record_hash if active else None)
        self.store.commit(new, current)  # conditional on `current` still being active
        return new

    def _authorize_change(self, principal: Principal, mode: OperatingMode, active: AuthzVersion | None,
                          new: AuthorizationConfig) -> AuthzChange:
        old = active.config if active else AuthorizationConfig(accounts={})
        other = OperatingMode.LIVE if mode == OperatingMode.FIXTURE else OperatingMode.FIXTURE
        if old.mode_view(other) != new.mode_view(other):
            raise AuthzDeniedError(f"a {mode.value} save cannot change {other.value} entries "
                                   f"({'dev:' if other == OperatingMode.FIXTURE else 'ARN/root'} entries)")
        old_view, new_view = old.mode_view(mode), new.mode_view(mode)
        if old_view == new_view:
            raise ValueError(f"no change to {mode.value} entries")
        changed = sorted(a for a in {*old_view, *new_view} if old_view.get(a) != new_view.get(a))
        initialized = active is not None and mode in active.initialized_modes

        for account, lists in new_view.items():
            if not lists[Permission.ADMIN.value]:
                raise AuthzDeniedError(f"account {account} would have no {mode.value} administrator")
        if initialized and not new_view:
            raise AuthzDeniedError(f"a save cannot remove every {mode.value} administrator")

        def person_from(config: AuthorizationConfig) -> str:
            account = principal.account_id if mode == OperatingMode.LIVE else None
            return resolve_person(config, principal, account, mode)[0]

        common = dict(mode=mode, principal=principal.as_actor(), principal_arn=principal.caller_arn,
                      account_id=principal.account_id, authn_method=principal.authn_method,
                      authenticated_at=principal.authenticated_at,
                      authorized_by_version=active.version if active else None,
                      authorized_by_record_hash=active.record_hash if active else None, changed_accounts=changed)

        if not initialized:
            # One-time bootstrap of this mode by the explicitly configured administrator.
            if old_view:
                raise AuthzIntegrityError(f"{mode.value} entries exist but the mode was never initialised")
            bootstrap = self.bootstrap_admins.get(mode)
            if bootstrap is None:
                raise AuthzDeniedError(f"{mode.value} allow-list is not initialised and no {mode.value} bootstrap "
                                       f"administrator is configured ({BOOTSTRAP_ENV})")
            if principal.canonical_id != bootstrap:
                raise AuthzDeniedError(f"{principal.canonical_id} is not the configured {mode.value} bootstrap administrator")
            if mode == OperatingMode.LIVE and set(new_view) != {principal.account_id}:
                raise AuthzDeniedError("a LIVE bootstrap may define only the caller's own account "
                                       f"({principal.account_id})")
            for account, lists in new_view.items():
                if bootstrap not in [_canonical_entry(e) for e in lists[Permission.ADMIN.value]]:
                    raise AuthzDeniedError(f"the bootstrap configuration must list {bootstrap} as admin of {account}")
            # The bootstrap configuration must bind the bootstrap administrator to a person (M3).
            return AuthzChange(basis="BOOTSTRAP", bootstrap_admin=bootstrap, person_id=person_from(new), **common)

        # Initialised: the bootstrap setting is never consulted again; only active admins may change.
        if mode == OperatingMode.LIVE:
            if set(new_view) != set(old_view):
                raise AuthzDeniedError("LIVE accounts cannot be added or removed after bootstrap")
            foreign = [a for a in changed if a != principal.account_id]
            if foreign:
                raise AuthzDeniedError(f"caller account {principal.account_id} cannot change grants of account(s) "
                                       f"{', '.join(foreign)}")
        matched: dict[str, str] = {}
        for account in changed:
            if account in old_view:
                decision = self._evaluate(active, principal, account, Permission.ADMIN, mode)
                if not decision.allowed:
                    raise AuthzDeniedError(decision.reason)
                matched[account] = decision.basis.matched_entry
            else:  # FIXTURE only: a new account is added by an existing administrator
                for existing in old_view:
                    decision = self._evaluate(active, principal, existing, Permission.ADMIN, mode)
                    if decision.allowed:
                        matched[account] = decision.basis.matched_entry
                        break
                else:
                    raise AuthzDeniedError(f"{principal.canonical_id} is not an administrator and cannot add "
                                           f"account {account}")
        return AuthzChange(basis="ADMIN", admin_entries=matched, person_id=person_from(active.config), **common)

    # ---- person resolution (M3)

    def resolve_identity(self, principal: Principal, account_id: str, mode: OperatingMode) -> IdentityEvidence:
        """Resolve `principal` to a person under the ACTIVE allow-list, with audit evidence."""
        try:
            active = self.get_active()
        except AuthzNotFoundError as exc:
            raise PersonResolutionError(str(exc)) from None
        person, kind = resolve_person(active.config, principal, account_id, mode)
        return IdentityEvidence(person_id=person, binding_kind=kind, principal_arn=principal.caller_arn,
                                sts_user_id=principal.user_id, authz_version=active.version,
                                authz_hash=active.content_hash)

    def separation(self, account_id: str) -> SeparationSettings:
        grants = self.get_active().config.accounts.get(account_id)
        return grants.separation if grants else SeparationSettings()

    # ---- evaluation

    def evaluate(self, principal: Principal, account_id: str, permission: Permission,
                 mode: OperatingMode) -> AuthzDecision:
        """Is `principal` allowed `permission` on `account_id` under the ACTIVE configuration?"""
        try:
            active = self.get_active()
        except AuthzNotFoundError as exc:
            return AuthzDecision(allowed=False, basis=None, reason=str(exc))
        return self._evaluate(active, principal, account_id, permission, mode)

    def _evaluate(self, active: AuthzVersion, principal: Principal, account_id: str, permission: Permission,
                  mode: OperatingMode) -> AuthzDecision:
        grants = active.config.accounts.get(account_id)
        if grants is None:
            return AuthzDecision(allowed=False, basis=None, reason=f"account {account_id} is not in the allow-list")
        if mode == OperatingMode.LIVE:
            if not principal.authenticated:
                return AuthzDecision(allowed=False, basis=None, reason="LIVE mode requires an authenticated AWS principal")
            if principal.account_id != account_id:
                return AuthzDecision(allowed=False, basis=None,
                                     reason=f"caller account {principal.account_id} is not the target account {account_id}")
        elif principal.authenticated:
            return AuthzDecision(allowed=False, basis=None, reason="FIXTURE mode uses development identities only")
        entry = self._match(active, principal.canonical_id, account_id, permission, mode)
        if entry is None:
            return AuthzDecision(allowed=False, basis=None,
                                 reason=f"{principal.canonical_id} lacks '{permission.value}' on account {account_id}")
        basis = AuthorizationBasis(ref=active.ref, account_id=account_id, permission=permission, matched_entry=entry)
        return AuthzDecision(allowed=True, basis=basis, reason="allowed")

    @staticmethod
    def _match(active: AuthzVersion, canonical_id: str, account_id: str, permission: Permission,
               mode: OperatingMode) -> str | None:
        grants = active.config.accounts.get(account_id)
        if grants is None:
            return None
        for entry in grants.entries(permission):
            if entry.startswith("dev:") != (mode == OperatingMode.FIXTURE):
                continue  # dev entries only in FIXTURE; ARN entries only in LIVE
            if _canonical_entry(entry) == canonical_id:
                return entry
        return None

    def grants(self, canonical_id: str, account_id: str, permission: Permission, mode: OperatingMode) -> AuthorizationBasis | None:
        """Re-check a previously recorded principal against the ACTIVE configuration (e.g. at execution)."""
        try:
            active = self.get_active()
        except AuthzNotFoundError:
            return None
        entry = self._match(active, canonical_id, account_id, permission, mode)
        if entry is None:
            return None
        return AuthorizationBasis(ref=active.ref, account_id=account_id, permission=permission, matched_entry=entry)
