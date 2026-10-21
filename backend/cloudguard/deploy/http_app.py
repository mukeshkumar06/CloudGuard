"""ASGI app for running the CloudGuard cost MCP server behind AWS Lambda Web Adapter (PROTOTYPE).

Lambda Web Adapter starts a normal HTTP server inside the function and forwards each Function URL request
to it, so the server needs no Lambda-specific code: this module only adds what a remote endpoint needs.
It registers no tools of its own: the six cost tools come from cloudguard.server.app.create_server.

Two separate concerns, plus AWS IAM:

1. Caller authentication (here, before anything reaches MCP). An `Authenticator` turns the Authorization
   header into a `Principal` or refuses it. The only implementation is a static bearer token: the server
   holds its SHA-256 (CLOUDGUARD_TOKEN_SHA256), never the token, and compares digests in constant time.
   Tokens must be random and at least 32 bytes (`python -m cloudguard.deploy --new-token` makes one). Only
   the digest is configured, so the server can't measure a token's strength; it refuses the digests of
   known weak tokens. OAuth 2.1 (Alexa+ account linking) is NOT implemented: CLOUDGUARD_AUTH_MODE=oauth is
   refused at startup rather than faked, and /.well-known/oauth-protected-resource answers 404.
   Missing or invalid credentials: 401 with `WWW-Authenticate: Bearer`. A valid caller without the
   "cost:read" scope: 403 (error="insufficient_scope").
2. Tool authorization (in the MCP server, before argument handling or tool code). Each cost tool needs
   "cost:read". Unknown tools and requests without a principal are refused as tool errors.
3. AWS IAM (the Lambda execution role). Decides which AWS calls the process can make; every cost tool is
   read-only. No tool argument can name an account, role, profile, region or credentials.

Authentication happens before any AWS call: startup makes none (live mode only builds the Cost Explorer
client), and only tool calls reach AWS.

Also: host and origin validation stays on (the MCP SDK's DNS-rebinding protection with an explicit
allow-list, CLOUDGUARD_ALLOWED_HOSTS); request bodies over MAX_BODY_BYTES answer 413; GET /healthz for the
adapter's readiness check (no auth, no AWS call, no account data); one JSON log line per request (method,
path, status, auth outcome, caller subject, duration; never headers, bodies or the token); unexpected errors
answer 500 with no details. There is no request timeout here: the Lambda function timeout bounds a request.

Environment:
    CLOUDGUARD_MODE           fixture (default) or live (AWS credentials of the process: the execution role)
    CLOUDGUARD_FIXTURE        fixture mode: cost fixture name or path (default: demo)
    CLOUDGUARD_AUTH_MODE      static-token (default; the only implemented mode)
    CLOUDGUARD_TOKEN_SHA256   hex SHA-256 of the bearer token (required)
    CLOUDGUARD_TOKEN_SUBJECT  name recorded for the token's caller in logs (default: static-token)
    CLOUDGUARD_TOKEN_SCOPES   space- or comma-separated scopes of the token (default: cost:read)
    CLOUDGUARD_ALLOWED_HOSTS  comma-separated Host values, each "name" or "name:port" or "name:*"
                              (default: 127.0.0.1:*,localhost:*,[::1]:*)
"""

from __future__ import annotations

import hashlib
import hmac
import json
import logging
import os
import re
import secrets
import time
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Protocol

from mcp.server.transport_security import TransportSecuritySettings

from cloudguard.server.app import PRINCIPAL_SCOPE_KEY, create_server
from cloudguard.server.context import CostContext
from cloudguard.server.cost_tools import COST_TOOL_NAMES

log = logging.getLogger("cloudguard.deploy")

LOCAL_HOSTS = ("127.0.0.1:*", "localhost:*", "[::1]:*")
HEALTH_PATH = "/healthz"
OAUTH_METADATA_PATH = "/.well-known/oauth-protected-resource"
COST_SCOPE = "cost:read"
KNOWN_SCOPES = frozenset({COST_SCOPE})
DEFAULT_SCOPES = frozenset({COST_SCOPE})
TOOL_SCOPES = {t: COST_SCOPE for t in COST_TOOL_NAMES}
# A JSON-RPC request to these tools is a few hundred bytes; 64 KiB leaves ample room. The MCP SDK's own
# limit (4 MiB) still applies behind this one.
MAX_BODY_BYTES = 64 * 1024
MIN_TOKEN_BYTES = 32
MIN_TOKEN_CHARS = 43  # 32 random bytes in URL-safe base64 (secrets.token_urlsafe(32))
# Digests of tokens no deployment should accept. The server sees only the digest, so this is a backstop for
# obvious mistakes, not an entropy check: generate tokens with `python -m cloudguard.deploy --new-token`.
WEAK_TOKENS = ("", "token", "secret", "password", "changeme", "test", "test-token", "cloudguard", "admin",
               "bearer", "0" * 32, "1234567890", "12345678901234567890123456789012")
WEAK_DIGESTS = frozenset(hashlib.sha256(t.encode()).hexdigest() for t in WEAK_TOKENS)
_HOST = re.compile(r"^(\[[0-9a-fA-F:]+\]|[A-Za-z0-9.-]+)(:(\d{1,5}|\*))?$")
_SHA256_HEX = re.compile(r"^[0-9a-f]{64}$")
_SUBJECT = re.compile(r"^[A-Za-z0-9._@:-]{1,64}$")


class ConfigError(ValueError):
    """Invalid deployment configuration; the app refuses to start."""


def new_token() -> tuple[str, str]:
    """A new random bearer token (32 bytes, URL-safe base64) and the SHA-256 hex digest to configure."""
    token = secrets.token_urlsafe(MIN_TOKEN_BYTES)
    return token, token_digest(token)


def token_digest(token: str) -> str:
    """The digest to configure for an operator-supplied token; refuses tokens shorter than 32 random bytes."""
    if len(token) < MIN_TOKEN_CHARS or len(set(token)) < 16:
        raise ConfigError(f"bearer tokens must be random and at least {MIN_TOKEN_BYTES} bytes "
                          f"({MIN_TOKEN_CHARS} URL-safe characters); use --new-token")
    return hashlib.sha256(token.encode()).hexdigest()


@dataclass(frozen=True)
class DeployConfig:
    mode: str
    fixture: str | None
    token_sha256: bytes
    allowed_hosts: tuple[str, ...]
    token_subject: str = "static-token"
    token_scopes: frozenset[str] = DEFAULT_SCOPES

    @classmethod
    def from_env(cls, env: Mapping[str, str] | None = None) -> DeployConfig:
        env = os.environ if env is None else env
        mode = env.get("CLOUDGUARD_MODE", "fixture")
        if mode not in ("fixture", "live"):
            raise ConfigError("CLOUDGUARD_MODE must be fixture or live")
        auth_mode = env.get("CLOUDGUARD_AUTH_MODE", "static-token")
        if auth_mode == "oauth":
            raise ConfigError("CLOUDGUARD_AUTH_MODE=oauth is not implemented: OAuth 2.1 needs the account-linking "
                              "authorization server (issuer, token validation keys, audience); see "
                              "deploy/lambda/README.md")
        if auth_mode != "static-token":
            raise ConfigError("CLOUDGUARD_AUTH_MODE must be static-token")
        digest = env.get("CLOUDGUARD_TOKEN_SHA256", "").strip().lower()
        if not _SHA256_HEX.match(digest):
            raise ConfigError("CLOUDGUARD_TOKEN_SHA256 must be the 64-character hex SHA-256 of the bearer token")
        if digest in WEAK_DIGESTS:
            raise ConfigError("CLOUDGUARD_TOKEN_SHA256 is the digest of a known weak token; generate a random "
                              "one with `python -m cloudguard.deploy --new-token`")
        subject = env.get("CLOUDGUARD_TOKEN_SUBJECT", "static-token").strip()
        if not _SUBJECT.fullmatch(subject):
            raise ConfigError("CLOUDGUARD_TOKEN_SUBJECT must be 1-64 characters of letters, digits and ._@:-")
        raw_scopes = env.get("CLOUDGUARD_TOKEN_SCOPES")
        scopes = (frozenset(s for s in re.split(r"[\s,]+", raw_scopes) if s) if raw_scopes is not None
                  else DEFAULT_SCOPES)
        if unknown := scopes - KNOWN_SCOPES:
            raise ConfigError(f"unknown scope(s) in CLOUDGUARD_TOKEN_SCOPES: {sorted(unknown)}; "
                              f"expected {sorted(KNOWN_SCOPES)}")
        raw_hosts = env.get("CLOUDGUARD_ALLOWED_HOSTS", "")
        hosts = tuple(h.strip() for h in raw_hosts.split(",") if h.strip()) or LOCAL_HOSTS
        for host in hosts:
            if host in ("*", "*:*") or not _HOST.match(host):
                raise ConfigError(f"invalid allowed host {host!r}: use name, name:port or name:*")
        return cls(mode=mode, fixture=env.get("CLOUDGUARD_FIXTURE") or None, token_sha256=bytes.fromhex(digest),
                   allowed_hosts=hosts, token_subject=subject, token_scopes=scopes)


# ---------- 1. authentication ----------

@dataclass(frozen=True)
class Principal:
    """An authenticated caller. `scopes` decide which tools it may call."""

    subject: str
    scopes: frozenset[str]
    method: str  # how it was authenticated, e.g. "static-token"


class Authenticator(Protocol):
    def authenticate(self, authorization: str | None) -> Principal | str:
        """The caller, or the failure reason ("missing" or "invalid")."""
        ...


class StaticTokenAuthenticator:
    """An operator-issued bearer token, checked against its SHA-256 digest in constant time."""

    def __init__(self, token_sha256: bytes, subject: str, scopes: frozenset[str]):
        self._digest = token_sha256
        self._principal = Principal(subject, scopes, "static-token")

    def authenticate(self, authorization: str | None) -> Principal | str:
        if authorization is None:
            return "missing"
        scheme, _, token = authorization.partition(" ")
        if scheme.lower() != "bearer" or not token.strip():
            return "invalid"
        supplied = hashlib.sha256(token.strip().encode()).digest()
        return self._principal if hmac.compare_digest(supplied, self._digest) else "invalid"


# ---------- 2. tool authorization ----------

def authorize_tool(principal: object, tool: str) -> bool:
    """Allow a tool call only for an authenticated Principal holding the tool's scope. Fails closed."""
    if not isinstance(principal, Principal):
        return False
    required = TOOL_SCOPES.get(tool)
    return required is not None and required in principal.scopes


# ---------- HTTP ----------

def transport_security(hosts: tuple[str, ...]) -> TransportSecuritySettings:
    """DNS-rebinding protection on, with the configured hosts. Browser origins are https except localhost."""
    origins = []
    for host in hosts:
        local = host.split(":")[0] in ("127.0.0.1", "localhost") or host.startswith("[::1]")
        origins.append(f"{'http' if local else 'https'}://{host}")
    return TransportSecuritySettings(enable_dns_rebinding_protection=True, allowed_hosts=list(hosts),
                                     allowed_origins=origins)


async def _send_json(send, status: int, body: dict, extra_headers: list[tuple[bytes, bytes]] = ()) -> None:
    payload = json.dumps(body).encode()
    headers = [(b"content-type", b"application/json"), (b"content-length", str(len(payload)).encode()),
               *extra_headers]
    await send({"type": "http.response.start", "status": status, "headers": headers})
    await send({"type": "http.response.body", "body": payload})


def _declared_length(scope) -> int | None:
    value = next((v for k, v in scope.get("headers", []) if k == b"content-length"), None)
    try:
        return int(value) if value is not None else None
    except ValueError:
        return None


class BearerAuthApp:
    """ASGI wrapper: health check, authentication, a body-size limit, then the MCP app.

    Only two scope types exist here: `lifespan` (startup/shutdown, no request data) passes straight through, and
    `http` is authenticated. A `websocket` is closed with 1008 (policy violation) before the inner app sees it, and
    any other scope type is refused: nothing but authenticated HTTP ever reaches a route."""

    def __init__(self, app, authenticator: Authenticator, mode: str, max_body_bytes: int = MAX_BODY_BYTES):
        self.app, self.authenticator, self.mode, self.max_body_bytes = app, authenticator, mode, max_body_bytes

    async def __call__(self, scope, receive, send):
        if scope["type"] == "lifespan":
            return await self.app(scope, receive, send)
        if scope["type"] == "websocket":
            log.info(json.dumps({"event": "websocket_refused", "path": scope.get("path")}))
            return await send({"type": "websocket.close", "code": 1008})
        if scope["type"] != "http":
            raise RuntimeError("unsupported ASGI scope type")  # never forwarded
        started = time.perf_counter()
        status = {"code": 0, "started": False}

        async def tracking_send(message):
            if message["type"] == "http.response.start":
                status["code"], status["started"] = message["status"], True
            await send(message)

        async def read_body() -> bytes | None:
            """The whole request body, or None once it exceeds the limit. Bodies here are small JSON-RPC
            messages, so reading them before the MCP app is cheap and keeps the limit outside the SDK."""
            chunks, size = [], 0
            while True:
                message = await receive()
                if message["type"] != "http.request":
                    return b"".join(chunks)  # disconnect: the inner app sees an empty body
                chunk = message.get("body", b"")
                size += len(chunk)
                if size > self.max_body_bytes:
                    return None
                chunks.append(chunk)
                if not message.get("more_body", False):
                    return b"".join(chunks)

        auth, subject = "exempt", None
        try:
            if scope["path"] == HEALTH_PATH and scope["method"] in ("GET", "HEAD"):
                return await _send_json(tracking_send, 200, {"status": "ok", "mode": self.mode})
            if scope["path"] == OAUTH_METADATA_PATH:  # OAuth is not implemented: say so, never fake metadata
                return await _send_json(tracking_send, 404, {"error": "not_found"})
            header = next((v for k, v in scope.get("headers", []) if k == b"authorization"), None)
            result = self.authenticator.authenticate(None if header is None else header.decode("latin-1"))
            if not isinstance(result, Principal):
                auth = result
                return await _send_json(tracking_send, 401, {"error": "unauthorized"},
                                        [(b"www-authenticate", b'Bearer realm="cloudguard"')])
            subject = result.subject
            if not result.scopes & KNOWN_SCOPES:
                auth = "forbidden"
                return await _send_json(tracking_send, 403, {"error": "forbidden"},
                                        [(b"www-authenticate", b'Bearer realm="cloudguard", error="insufficient_scope"')])
            auth = "ok"
            declared = _declared_length(scope)
            if declared is not None and declared > self.max_body_bytes:
                return await _send_json(tracking_send, 413, {"error": "payload_too_large"})
            body = await read_body()
            if body is None:
                return await _send_json(tracking_send, 413, {"error": "payload_too_large"})
            replayed = False

            async def replay_receive():
                nonlocal replayed
                if not replayed:
                    replayed = True
                    return {"type": "http.request", "body": body, "more_body": False}
                return await receive()

            scope[PRINCIPAL_SCOPE_KEY] = result  # read by the MCP server's tool authorizer
            return await self.app(scope, replay_receive, tracking_send)
        except Exception:
            log.error(json.dumps({"event": "http_error", "method": scope["method"], "path": scope["path"]}))
            if not status["started"]:
                await _send_json(tracking_send, 500, {"error": "internal_error"})
        finally:
            log.info(json.dumps({"event": "http_request", "method": scope["method"], "path": scope["path"],
                                 "status": status["code"], "auth": auth, "subject": subject,
                                 "duration_ms": round((time.perf_counter() - started) * 1000, 1)}))


def build_context(config: DeployConfig) -> CostContext:
    """Fixture data, or a live context that makes no AWS call until an authorized tool runs."""
    return CostContext.live() if config.mode == "live" else CostContext.fixture(config.fixture)


def build_app(config: DeployConfig | None = None, *, context: CostContext | None = None):
    """The six cost tools behind authentication, tool authorization, a body limit and a host allow-list.

    `context` replaces the context built from `config` (tests and embedding only).
    """
    config = config or DeployConfig.from_env()
    server = create_server(context or build_context(config), authorizer=authorize_tool)
    # Must be set before streamable_http_app() builds the session manager, which reads it once.
    server.settings.transport_security = transport_security(config.allowed_hosts)
    authenticator = StaticTokenAuthenticator(config.token_sha256, config.token_subject, config.token_scopes)
    return BearerAuthApp(server.streamable_http_app(), authenticator, config.mode)
