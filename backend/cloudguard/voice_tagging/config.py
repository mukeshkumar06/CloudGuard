"""Voice tagging configuration, read once at startup. Disabled unless explicitly enabled; refuses to start when
enabled but incomplete.

Environment:
    CLOUDGUARD_VOICE_TAGGING_ENABLED        "true" to enable (default: disabled; anything else stays disabled)
    CLOUDGUARD_VOICE_OPERATOR_TOKEN_SHA256  hex SHA-256 of the operator credential (required when enabled). Must
                                            differ from CLOUDGUARD_TOKEN_SHA256 and CLOUDGUARD_ALEXA_TOKEN_SHA256.
    CLOUDGUARD_VOICE_OPERATOR_SUBJECT       name recorded for the operator in logs and audit (default: operator)
    CLOUDGUARD_VOICE_TAGGING_ACCOUNTS       comma-separated 12-digit AWS account IDs voice tagging may write to
                                            (required). The STS caller's account must be one of them.
    CLOUDGUARD_VOICE_TAGGING_ROLE_ARN       live mode: the tagging role the server assumes for writes (required in
                                            live mode; must belong to an allow-listed account)
    CLOUDGUARD_VOICE_TAGGING_KILL_FILE      while this file exists every voice tagging request is refused and a
                                            running execution stops before its next write
                                            (default: .cloudguard/voice-tagging.disabled)
    CLOUDGUARD_VOICE_TAGGING_STORE          directory for proposals, allow-list and audit (default: .cloudguard)
    CLOUDGUARD_VOICE_SESSION_TTL_SECONDS    operator session lifetime, 60-14400 (default: 1800)
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import timedelta
from pathlib import Path

_SHA256_HEX = re.compile(r"^[0-9a-f]{64}$")
_SUBJECT = re.compile(r"^[A-Za-z0-9._@:-]{1,64}$")
_ACCOUNT = re.compile(r"^\d{12}$")
_ROLE_ARN = re.compile(r"^arn:aws[a-z-]*:iam::(\d{12}):role/[\w+=,.@/-]{1,512}$")
DEFAULT_KILL_FILE = Path(".cloudguard") / "voice-tagging.disabled"
DEFAULT_STORE = Path(".cloudguard")


class VoiceConfigError(ValueError):
    """Voice tagging is enabled but misconfigured; the server refuses to start."""


@dataclass(frozen=True)
class VoiceTaggingConfig:
    enabled: bool
    operator_token_sha256: bytes | None = None
    operator_subject: str = "operator"
    accounts: frozenset[str] = frozenset()
    role_arn: str | None = None
    kill_file: Path = DEFAULT_KILL_FILE
    store: Path = DEFAULT_STORE
    session_ttl: timedelta = timedelta(minutes=30)

    @classmethod
    def disabled(cls) -> VoiceTaggingConfig:
        return cls(enabled=False)

    @classmethod
    def from_env(cls, env: Mapping[str, str], *, mode: str, other_digests: tuple[bytes | None, ...],
                 weak_digests: frozenset[str]) -> VoiceTaggingConfig:
        if env.get("CLOUDGUARD_VOICE_TAGGING_ENABLED", "").strip().lower() != "true":
            return cls.disabled()
        digest = env.get("CLOUDGUARD_VOICE_OPERATOR_TOKEN_SHA256", "").strip().lower()
        if not _SHA256_HEX.match(digest):
            raise VoiceConfigError("voice tagging needs CLOUDGUARD_VOICE_OPERATOR_TOKEN_SHA256: the 64-character hex "
                                   "SHA-256 of a dedicated operator credential")
        if digest in weak_digests:
            raise VoiceConfigError("CLOUDGUARD_VOICE_OPERATOR_TOKEN_SHA256 is the digest of a known weak token")
        if any(d is not None and bytes.fromhex(digest) == d for d in other_digests):
            raise VoiceConfigError("the voice operator credential must differ from the MCP and Alexa tokens")
        subject = env.get("CLOUDGUARD_VOICE_OPERATOR_SUBJECT", "operator").strip()
        if not _SUBJECT.match(subject):
            raise VoiceConfigError("CLOUDGUARD_VOICE_OPERATOR_SUBJECT must be 1-64 characters of letters, digits "
                                   "and ._@:-")
        accounts = frozenset(a.strip() for a in env.get("CLOUDGUARD_VOICE_TAGGING_ACCOUNTS", "").split(",")
                             if a.strip())
        if not accounts or not all(_ACCOUNT.match(a) for a in accounts):
            raise VoiceConfigError("voice tagging needs CLOUDGUARD_VOICE_TAGGING_ACCOUNTS: the 12-digit ID of each "
                                   "personal development account it may write to")
        role = env.get("CLOUDGUARD_VOICE_TAGGING_ROLE_ARN", "").strip() or None
        if mode == "live":
            if role is None:
                raise VoiceConfigError("live voice tagging needs CLOUDGUARD_VOICE_TAGGING_ROLE_ARN (the tagging role)")
            m = _ROLE_ARN.match(role)
            if not m or m[1] not in accounts:
                raise VoiceConfigError("CLOUDGUARD_VOICE_TAGGING_ROLE_ARN must be an IAM role ARN in an allow-listed "
                                       "account")
        try:
            ttl = int(env.get("CLOUDGUARD_VOICE_SESSION_TTL_SECONDS", "1800"))
        except ValueError:
            raise VoiceConfigError("CLOUDGUARD_VOICE_SESSION_TTL_SECONDS must be a number of seconds") from None
        if not 60 <= ttl <= 4 * 3600:
            raise VoiceConfigError("CLOUDGUARD_VOICE_SESSION_TTL_SECONDS must be between 60 and 14400")
        return cls(enabled=True, operator_token_sha256=bytes.fromhex(digest), operator_subject=subject,
                   accounts=accounts, role_arn=role,
                   kill_file=Path(env.get("CLOUDGUARD_VOICE_TAGGING_KILL_FILE") or DEFAULT_KILL_FILE),
                   store=Path(env.get("CLOUDGUARD_VOICE_TAGGING_STORE") or DEFAULT_STORE),
                   session_ttl=timedelta(seconds=ttl))
