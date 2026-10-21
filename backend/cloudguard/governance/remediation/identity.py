"""Identity: who is calling, established by an IdentityProvider - never by a caller-supplied label.

- StsCallerIdentityProvider (LIVE): the caller's own AWS credentials, verified by AWS through
  sts:GetCallerIdentity. The canonical identity used for authorisation and separation of duties is:
      IAM user          arn:aws:iam::<acct>:user/<name>        (path dropped)
      assumed role      arn:aws:iam::<acct>:role/<role-name>   (session name dropped - it is
                                                                caller-chosen, so it can't separate people)
      root              arn:aws:iam::<acct>:root
  Other principal types (federated users, service principals) are rejected.
- DevelopmentIdentityProvider (FIXTURE only): a named, explicitly UNauthenticated identity for
  synthetic data and tests. It can never satisfy a LIVE operation.

The STS UserId is captured as well (M3): for an IAM user it is the user's unique id (AIDA...), for an
assumed role "<role unique id>:<session name>", for root the account id. GetCallerIdentity returns only
Account, Arn and UserId - never SourceIdentity or session tags - so neither is available here.
A canonical principal is NOT a person: separation of duties resolves principals to people through the
allow-list's person bindings (authz.py).

Trust boundary: the CLI process runs with the caller's own credentials, so it is as trustworthy as
that local process. A server deployment (Phase 7) must verify identity server-side instead.
"""

from __future__ import annotations

import re
from collections.abc import Callable
from datetime import UTC, datetime
from typing import Protocol

from pydantic import BaseModel, ConfigDict, model_validator

from cloudguard.governance.remediation.models import Actor, ActorType, OperatingMode

_ACCOUNT = r"\d{12}"
_PARTITION = r"aws|aws-cn|aws-us-gov"
_USER = re.compile(rf"^arn:(?P<p>{_PARTITION}):iam::(?P<acct>{_ACCOUNT}):user/(?:[\w+=,.@-]+/)*(?P<name>[\w+=,.@-]+)$")
_ASSUMED = re.compile(rf"^arn:(?P<p>{_PARTITION}):sts::(?P<acct>{_ACCOUNT}):assumed-role/(?P<role>[\w+=,.@-]+)/(?P<session>[\w+=,.@-]+)$")
_ROOT = re.compile(rf"^arn:(?P<p>{_PARTITION}):iam::(?P<acct>{_ACCOUNT}):root$")
_DEV_NAME = re.compile(r"^[a-z0-9][a-z0-9._-]{0,62}$")


class IdentityError(PermissionError):
    pass


class PrincipalKind:
    IAM_USER = "IAM_USER"
    ASSUMED_ROLE = "ASSUMED_ROLE"
    ROOT = "ROOT"
    DEVELOPMENT = "DEVELOPMENT"


class Principal(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    kind: str
    canonical_id: str  # used for authorisation and separation of duties
    caller_arn: str | None  # exact ARN returned by STS (AWS principals only)
    account_id: str | None  # verified by STS (AWS principals only)
    session_name: str | None = None
    user_id: str | None = None  # STS GetCallerIdentity UserId (AWS principals only; required for them)
    authenticated: bool
    authn_method: str  # "sts:GetCallerIdentity" | "development"
    authenticated_at: datetime

    @model_validator(mode="after")
    def _consistent(self) -> Principal:
        if self.kind == PrincipalKind.DEVELOPMENT:
            if self.authenticated or self.account_id or self.caller_arn or self.authn_method != "development":
                raise ValueError("development principals are never authenticated and have no AWS account")
            if not self.canonical_id.startswith("dev:"):
                raise ValueError("development canonical ids start with 'dev:'")
        else:
            if not (self.authenticated and self.account_id and self.caller_arn and self.authn_method == "sts:GetCallerIdentity"):
                raise ValueError("AWS principals must be STS-authenticated with an account and caller ARN")
            if self.canonical_id.startswith("dev:"):
                raise ValueError("AWS principals cannot use development ids")
            if not self.user_id:
                raise ValueError("AWS principals carry the STS UserId")
            if self.kind == PrincipalKind.ASSUMED_ROLE:
                role_id, sep, session = self.user_id.partition(":")
                if not (role_id and sep and session == self.session_name):
                    raise ValueError("an assumed-role UserId must be '<role unique id>:<session name>'")
            elif self.kind == PrincipalKind.IAM_USER and ":" in self.user_id:
                raise ValueError("an IAM user's UserId is its unique id")
            elif self.kind == PrincipalKind.ROOT and self.user_id != self.account_id:
                raise ValueError("the root user's UserId is the account id")
        if self.kind == PrincipalKind.DEVELOPMENT and self.user_id is not None:
            raise ValueError("development principals have no STS UserId")
        return self

    @property
    def unique_id(self) -> str | None:
        """The IAM unique id of the user or role (AIDA... / AROA...), without any session name."""
        if self.kind == PrincipalKind.IAM_USER:
            return self.user_id
        if self.kind == PrincipalKind.ASSUMED_ROLE and self.user_id:
            return self.user_id.partition(":")[0]
        return None

    @property
    def mode(self) -> OperatingMode:
        return OperatingMode.LIVE if self.authenticated else OperatingMode.FIXTURE

    def as_actor(self) -> Actor:
        actor_type = ActorType.AWS_PRINCIPAL if self.authenticated else ActorType.DEVELOPMENT
        return Actor(actor_type=actor_type, id=self.canonical_id)


def principal_from_caller_arn(arn: str, account_id: str, now: datetime, user_id: str) -> Principal:
    """Build a canonical principal from a GetCallerIdentity response (Arn, Account, UserId).
    Rejects unsupported types and inconsistent UserIds."""
    common = dict(caller_arn=arn, account_id=account_id, authenticated=True, authn_method="sts:GetCallerIdentity",
                  authenticated_at=now, user_id=user_id)
    if m := _USER.match(arn):
        kind, canonical, session = PrincipalKind.IAM_USER, f"arn:{m['p']}:iam::{m['acct']}:user/{m['name']}", None
    elif m := _ASSUMED.match(arn):
        kind, canonical, session = (PrincipalKind.ASSUMED_ROLE, f"arn:{m['p']}:iam::{m['acct']}:role/{m['role']}",
                                    m["session"])
    elif m := _ROOT.match(arn):
        kind, canonical, session = PrincipalKind.ROOT, arn, None
    else:
        raise IdentityError(f"unsupported caller identity type: {arn}")
    if m["acct"] != account_id:
        raise IdentityError("caller ARN account does not match the STS account")
    try:
        return Principal(kind=kind, canonical_id=canonical, session_name=session, **common)
    except ValueError as exc:
        raise IdentityError(f"inconsistent caller identity: {exc}") from None


class IdentityProvider(Protocol):
    mode: OperatingMode

    def current(self) -> Principal: ...


class StsCallerIdentityProvider:
    """LIVE identity: whoever owns the credentials in this session, as verified by AWS."""

    mode = OperatingMode.LIVE

    def __init__(self, sts_client, clock: Callable[[], datetime] = lambda: datetime.now(UTC)):
        self.sts = sts_client
        self.clock = clock

    def current(self) -> Principal:
        try:
            resp = self.sts.get_caller_identity()
        except Exception as exc:  # any failure means "not authenticated"
            raise IdentityError(f"could not verify caller identity with STS: {type(exc).__name__}") from None
        if not resp.get("UserId"):
            raise IdentityError("STS did not return a UserId; the caller identity can't be verified")
        return principal_from_caller_arn(resp["Arn"], resp["Account"], self.clock(), resp["UserId"])


class DevelopmentIdentityProvider:
    """FIXTURE-only identity. Never authenticated; refused by every LIVE operation."""

    mode = OperatingMode.FIXTURE

    def __init__(self, name: str, clock: Callable[[], datetime] = lambda: datetime.now(UTC)):
        if not _DEV_NAME.match(name):
            raise IdentityError("development identity names are 1-63 chars of [a-z0-9._-]")
        self.name = name
        self.clock = clock

    def current(self) -> Principal:
        return Principal(kind=PrincipalKind.DEVELOPMENT, canonical_id=f"dev:{self.name}", caller_arn=None,
                         account_id=None, authenticated=False, authn_method="development",
                         authenticated_at=self.clock())


def require_identity_for_mode(principal: Principal, mode: OperatingMode) -> None:
    if mode == OperatingMode.LIVE and not principal.authenticated:
        raise IdentityError("LIVE operations require an STS-authenticated AWS principal; development identities are refused")
    if mode == OperatingMode.FIXTURE and principal.authenticated:
        raise IdentityError("FIXTURE operations use development identities only")
