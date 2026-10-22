"""Read-only account identity MCP tool: "Which AWS account am I using?"

get_aws_account_identity answers with the AWS account this server's credentials belong to and what kind of
principal they are, from AWS STS GetCallerIdentity. GetCallerIdentity needs no IAM permission and changes
nothing. No LLM is involved: `speech` is a deterministic template.

When AWS is called: never at startup. Live mode creates the boto3 session and the STS client inside the tool
call, which runs only after the HTTP front end has authenticated and authorized the caller. Fixture mode
returns a clearly labelled synthetic identity and never imports boto3.

What is returned: the account ID, the identity type (iam_user, assumed_role, root, federated_user or other)
and a display label built from the type and the IAM name (which AWS restricts to [A-Za-z0-9+=,.@_-]).
Never returned: credentials of any kind (access key IDs, secret keys, session tokens), the STS UserId (which
carries the access key or role ID), the assumed-role session name, and AWS error message text (which can name
the principal). The principal ARN (without an assumed role's session name) is in the operator result only; the
Alexa projection drops it.

Errors are answers, not crashes: missing credentials, access denied and network failures give outcome
"unavailable" with an error kind and the AWS error code, and a speech that says the account could not be
confirmed. The AWS message is logged locally, redacted.
"""

from __future__ import annotations

import logging
import re
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Literal

import anyio
from mcp.server.fastmcp import FastMCP
from mcp.types import ToolAnnotations
from pydantic import BaseModel, ConfigDict

from cloudguard.cost.service import Outcome
from cloudguard.server.governance_tools import redact

log = logging.getLogger(__name__)

SCHEMA_VERSION = "1"
IDENTITY_TOOL_NAMES = ("get_aws_account_identity",)

IdentityType = Literal["iam_user", "assumed_role", "root", "federated_user", "other"]
ErrorKind = Literal["no_credentials", "access_denied", "network", "aws_error", "unexpected_response"]

# Spoken and displayed per type. Never includes a name, so speech is free of untrusted text.
TYPE_PHRASE: dict[str, str] = {
    "iam_user": "an IAM user", "assumed_role": "an IAM role", "root": "the account root user",
    "federated_user": "a federated user", "other": "an AWS identity of another type",
}
TYPE_LABEL: dict[str, str] = {
    "iam_user": "IAM user", "assumed_role": "IAM role", "root": "Root user", "federated_user": "Federated user",
    "other": "Other identity",
}

# The synthetic identity fixture mode reports (the same synthetic account as the governance fixture).
FIXTURE_ACCOUNT = "123456789012"
FIXTURE_ARN = f"arn:aws:sts::{FIXTURE_ACCOUNT}:assumed-role/CloudGuardDemoReadOnly/cloudguard-demo-session"

_ACCOUNT = re.compile(r"^\d{12}$")
_IAM_NAME = re.compile(r"^[A-Za-z0-9+=,.@_-]{1,128}$")
_ARN = re.compile(r"^arn:aws[a-z-]*:(iam|sts)::(\d{12}):(.{1,2048})$")
_CODE = re.compile(r"^[A-Za-z0-9._:-]{1,64}$")


def classify(arn: str) -> tuple[IdentityType, str | None]:
    """(identity type, IAM name or None) from a GetCallerIdentity ARN. The session name is never kept."""
    m = _ARN.match(arn)
    if not m:
        return "other", None
    service, resource = m.group(1), m.group(3)
    if service == "iam" and resource == "root":
        return "root", None
    kind, _, rest = resource.partition("/")
    name = rest.rsplit("/", 1)[-1] if kind == "user" else rest.split("/", 1)[0]  # user path / role[/session]
    name = name if _IAM_NAME.match(name or "") else None
    if service == "iam" and kind == "user":
        return "iam_user", name
    if service == "sts" and kind == "assumed-role":
        return "assumed_role", name
    if service == "sts" and kind == "federated-user":
        return "federated_user", name
    return "other", None


def display_label(identity_type: str, name: str | None) -> str:
    return f"{TYPE_LABEL[identity_type]} {name}" if name else TYPE_LABEL[identity_type]


def identity_speech(mode: str, account_id: str, identity_type: str) -> str:
    if mode == "fixture":
        return (f"This is fixture mode with synthetic data: the demo account ends in {account_id[-4:]} and uses "
                f"{TYPE_PHRASE[identity_type]}. No real AWS account is connected.")
    return f"You are currently connected to AWS account ending in {account_id[-4:]} using {TYPE_PHRASE[identity_type]}."


# ---------- context ----------

@dataclass(frozen=True)
class IdentityContext:
    """Where the identity comes from, fixed at startup. `sts_factory` is called inside each live tool call."""

    mode: Literal["fixture", "live"]
    data_source: str
    sts_factory: Callable[[], object] | None = None
    clock: Callable[[], datetime] = field(default=lambda: datetime.now(UTC))

    @classmethod
    def fixture(cls) -> IdentityContext:
        return cls(mode="fixture", data_source="fixture synthetic identity (no AWS call)")

    @classmethod
    def live(cls, profile: str | None = None, session_factory: Callable[[], object] | None = None) -> IdentityContext:
        def factory():
            from botocore.config import Config  # live mode only

            if session_factory is not None:
                session = session_factory()
            else:
                import boto3
                session = boto3.Session(profile_name=profile)
            region = getattr(session, "region_name", None) or "us-east-1"
            return session.client("sts", region_name=region, config=Config(
                retries={"mode": "standard", "max_attempts": 2}, connect_timeout=5, read_timeout=10,
                user_agent_extra="cloudguard-ai"))
        return cls(mode="live", sts_factory=factory,
                   data_source="live AWS STS GetCallerIdentity (the server's own credentials, read-only)")


# ---------- output ----------

class IdentityError(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    kind: ErrorKind
    code: str  # AWS error code or exception name, e.g. AccessDenied, NoCredentialsError


class AccountIdentityResult(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    schema_version: str = SCHEMA_VERSION
    tool: Literal["get_aws_account_identity"] = "get_aws_account_identity"
    outcome: Outcome
    mode: Literal["fixture", "live"]
    synthetic: bool  # true in fixture mode: the identity is invented demo data
    data_source: str
    generated_at: datetime
    retrieved_at: datetime | None  # when STS (or the fixture) answered
    aws_requests: int
    account_id: str | None
    account_id_last4: str | None
    identity_type: IdentityType | None
    display_label: str | None  # e.g. "IAM role CloudGuardInspector"; no session name, never a credential
    principal_arn: str | None  # operator only (dropped for Alexa); an assumed role's session name is removed
    error: IdentityError | None
    speech: str


def _error_of(exc: Exception) -> IdentityError:
    from botocore.exceptions import ClientError, EndpointConnectionError, NoCredentialsError, NoRegionError
    from botocore.exceptions import PartialCredentialsError, ConnectTimeoutError, ReadTimeoutError

    if isinstance(exc, (NoCredentialsError, PartialCredentialsError)):
        return IdentityError(kind="no_credentials", code=type(exc).__name__)
    if isinstance(exc, ClientError):
        code = str(exc.response.get("Error", {}).get("Code", ""))
        code = code if _CODE.match(code) else "Unknown"
        denied = code in {"AccessDenied", "AccessDeniedException", "InvalidClientTokenId", "ExpiredToken",
                          "SignatureDoesNotMatch", "UnrecognizedClientException"}
        return IdentityError(kind="access_denied" if denied else "aws_error", code=code)
    if isinstance(exc, (EndpointConnectionError, ConnectTimeoutError, ReadTimeoutError, NoRegionError)):
        return IdentityError(kind="network", code=type(exc).__name__)
    return IdentityError(kind="aws_error", code=type(exc).__name__ if _CODE.match(type(exc).__name__) else "Unknown")


ERROR_SPEECH = {
    "no_credentials": "the server has no AWS credentials configured",
    "access_denied": "AWS refused the request; the credentials may be invalid or expired",
    "network": "AWS STS could not be reached",
    "aws_error": "AWS returned an error",
    "unexpected_response": "AWS returned an unexpected answer",
}


def _annotations(ctx: IdentityContext) -> ToolAnnotations:
    return ToolAnnotations(title="AWS account identity", readOnlyHint=True, destructiveHint=False,
                           idempotentHint=True, openWorldHint=ctx.mode == "live")


def register_identity_tools(server: FastMCP, ctx: IdentityContext) -> None:
    @server.tool(annotations=_annotations(ctx), structured_output=True)
    async def get_aws_account_identity() -> AccountIdentityResult:
        """Which AWS account this CloudGuard server is using: "Which AWS account am I using?"

        Returns the account ID and the kind of identity (IAM user, assumed IAM role, root user, federated user
        or other) of the server's own credentials, from AWS STS GetCallerIdentity (read-only, no permission
        needed). Never returns credentials. In fixture mode the identity is synthetic (synthetic: true) and no
        AWS call is made. outcome "unavailable" means the account could not be confirmed (see error).
        """
        base = dict(mode=ctx.mode, synthetic=ctx.mode == "fixture", data_source=ctx.data_source)
        if ctx.mode == "fixture":
            now = ctx.clock()
            kind, name = classify(FIXTURE_ARN)
            return AccountIdentityResult(
                **base, outcome=Outcome.ANSWERED, generated_at=now, retrieved_at=now, aws_requests=0,
                account_id=FIXTURE_ACCOUNT, account_id_last4=FIXTURE_ACCOUNT[-4:], identity_type=kind,
                display_label=display_label(kind, name), principal_arn=FIXTURE_ARN.rsplit("/", 1)[0], error=None,
                speech=identity_speech("fixture", FIXTURE_ACCOUNT, kind))

        def call() -> dict:
            return ctx.sts_factory().get_caller_identity()

        error: IdentityError | None = None
        response: dict = {}
        try:
            response = await anyio.to_thread.run_sync(call)
        except Exception as exc:  # noqa: BLE001 - every failure becomes a safe, typed answer
            error = _error_of(exc)
            log.warning("sts:GetCallerIdentity failed: %s %s: %s", error.kind, error.code, redact(str(exc)))
        now = ctx.clock()
        account = response.get("Account") if isinstance(response, dict) else None
        arn = response.get("Arn") if isinstance(response, dict) else None
        if error is None and not (isinstance(account, str) and _ACCOUNT.match(account) and isinstance(arn, str)):
            error = IdentityError(kind="unexpected_response", code="Unknown")
        if error is not None:
            return AccountIdentityResult(
                **base, outcome=Outcome.UNAVAILABLE, generated_at=now, retrieved_at=None, aws_requests=1,
                account_id=None, account_id_last4=None, identity_type=None, display_label=None, principal_arn=None,
                error=error, speech=f"I couldn't confirm which AWS account CloudGuard is using: {ERROR_SPEECH[error.kind]}.")
        kind, name = classify(arn)
        arn_account = _ARN.match(arn)
        if arn_account and arn_account.group(2) != account:
            kind, name = "other", None
        if kind == "assumed_role":
            arn = arn.rsplit("/", 1)[0]  # the session name is caller-chosen text, never needed to name the account
        return AccountIdentityResult(
            **base, outcome=Outcome.ANSWERED, generated_at=now, retrieved_at=now, aws_requests=1,
            account_id=account, account_id_last4=account[-4:], identity_type=kind,
            display_label=display_label(kind, name), principal_arn=arn if len(arn) <= 2048 else None, error=None,
            speech=identity_speech("live", account, kind))
