"""Operator sessions: the dedicated operator credential is exchanged once for a random session id, which the
browser holds only as an HttpOnly cookie (page JavaScript can't read it). The server keeps the SHA-256 of the
session id, never the id or the credential. Sessions expire, end on logout, and every confirmation challenge is
bound to one session.

The credential is checked against its configured SHA-256 in constant time, like the MCP bearer tokens
(deploy.http_app.StaticTokenAuthenticator). Repeated failures lock login for a few minutes."""

from __future__ import annotations

import hashlib
import hmac
import secrets
import threading
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

MAX_FAILURES = 10
LOCKOUT = timedelta(minutes=5)


@dataclass(frozen=True)
class OperatorSession:
    subject: str
    id_hash: str  # SHA-256 hex of the session id: also the binding for confirmation challenges
    created_at: datetime
    expires_at: datetime


def _hash(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()


class OperatorSessions:
    def __init__(self, credential_sha256: bytes, subject: str, ttl: timedelta,
                 clock: Callable[[], datetime] = lambda: datetime.now(UTC),
                 on_end: Callable[[str], None] | None = None):
        self._digest, self._subject, self._ttl, self._clock = credential_sha256, subject, ttl, clock
        self._sessions: dict[str, OperatorSession] = {}
        self._failures: list[datetime] = []
        self._lock = threading.Lock()
        self._on_end = on_end  # called with id_hash when a session ends (logout or expiry)

    def login(self, credential: str | None) -> tuple[str, OperatorSession] | None:
        now = self._clock()
        with self._lock:
            self._failures = [t for t in self._failures if now - t < LOCKOUT]
            if len(self._failures) >= MAX_FAILURES:
                return None
            supplied = hashlib.sha256((credential or "").encode()).digest()
            if not credential or not hmac.compare_digest(supplied, self._digest):
                self._failures.append(now)
                return None
            session_id = secrets.token_urlsafe(32)
            session = OperatorSession(self._subject, _hash(session_id), now, now + self._ttl)
            self._sessions[session.id_hash] = session
            return session_id, session

    def get(self, session_id: str | None) -> OperatorSession | None:
        if not session_id:
            return None
        key = _hash(session_id)
        with self._lock:
            session = self._sessions.get(key)
            if session is None:
                return None
            if self._clock() >= session.expires_at:
                del self._sessions[key]
                expired = True
            else:
                return session
        if expired and self._on_end:
            self._on_end(key)
        return None

    def logout(self, session_id: str | None) -> None:
        if not session_id:
            return
        key = _hash(session_id)
        with self._lock:
            ended = self._sessions.pop(key, None)
        if ended is not None and self._on_end:
            self._on_end(key)

    def end_all(self) -> None:
        with self._lock:
            keys = list(self._sessions)
            self._sessions.clear()
        for key in keys:
            if self._on_end:
                self._on_end(key)
