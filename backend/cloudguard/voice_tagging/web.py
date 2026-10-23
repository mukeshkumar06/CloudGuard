"""HTTP routes for personal voice tagging, mounted under /operator next to /mcp (deploy.http_app).

These are not MCP tools on purpose: MCP callers authenticate with bearer tokens that page JavaScript holds, and
the general Alexa token must never reach a write. Here the operator's session lives only in an HttpOnly,
SameSite=Strict cookie scoped to /operator, which page JavaScript can't read.

    POST /operator/login              form field `credential`; sets the session cookie, redirects to /
    POST /operator/logout             ends the session and voids its pending confirmations
    GET  /operator/session            {enabled, authenticated, operator, expires_at, mode, kill_switch}
    POST /operator/voice-tagging/prepare  TagSpec + channel "voice" -> plan and confirmation challenge
    POST /operator/voice-tagging/confirm  {challenge_id, transcript, overwrite_transcript?, channel "voice"}
    POST /operator/voice-tagging/cancel   {challenge_id}
    POST /operator/voice-tagging/kill     engages the kill switch (also: create the kill file)

Every JSON POST needs `X-CloudGuard-Voice: 1`; the Host and any Origin must be on the allowed-host list; with SameSite=Strict
that rules out cross-site requests. Responses are never cached. No response, log line or audit entry contains
the credential, the session id or AWS credentials.
"""

from __future__ import annotations

import json
import logging
import re
from urllib.parse import parse_qs, urlsplit

import anyio
from pydantic import ValidationError

from cloudguard.voice_tagging.service import TagSpec, VoiceTaggingError, VoiceTaggingService
from cloudguard.voice_tagging.sessions import OperatorSessions

log = logging.getLogger("cloudguard.voice_tagging")
COOKIE = "cg_operator"
PREFIX = "/operator"
MAX_BODY = 16 * 1024


def _host_allowed(host: str, allowed: tuple[str, ...]) -> bool:
    name, _, port = host.rpartition(":") if not host.endswith("]") else (host, "", "")
    if not name:
        name, port = host, ""
    for entry in allowed:
        e_name, _, e_port = entry.rpartition(":") if not entry.endswith("]") else (entry, "", "")
        if not e_name:
            e_name, e_port = entry, ""
        if e_name.lower() == name.lower() and (e_port in ("*", port) or (not e_port and not port)):
            return True
    return False


class VoiceTaggingApp:
    def __init__(self, service: VoiceTaggingService, sessions: OperatorSessions, allowed_hosts: tuple[str, ...]):
        self.service, self.sessions, self.allowed_hosts = service, sessions, allowed_hosts

    async def __call__(self, scope, receive, send):
        headers = {k.decode("latin-1").lower(): v.decode("latin-1") for k, v in scope.get("headers", [])}
        path, method = scope["path"], scope["method"]
        status = 0

        async def respond(code: int, body: dict | None = None, extra: list[tuple[str, str]] = ()):
            nonlocal status
            status = code
            payload = json.dumps(body or {}).encode()
            hdrs = [(b"content-type", b"application/json"), (b"cache-control", b"no-store"),
                    (b"content-length", str(len(payload)).encode())]
            hdrs += [(k.encode(), v.encode()) for k, v in extra]
            await send({"type": "http.response.start", "status": code, "headers": hdrs})
            await send({"type": "http.response.body", "body": payload})

        try:
            host = headers.get("host", "")
            if not _host_allowed(host, self.allowed_hosts):
                return await respond(421, {"error": "host_not_allowed"})
            origin = headers.get("origin")
            fetch_site = headers.get("sec-fetch-site")
            if fetch_site == "cross-site":
                return await respond(403, {"error": "origin_not_allowed"})
            # The simulator's no-referrer policy makes browsers send "Origin: null" on a form post; accept that only
            # when the browser itself says the request is same-origin.
            if origin == "null" and fetch_site == "same-origin":
                origin = None
            # Behind the same-origin dev proxy the Host is the backend's own address, so the Origin is checked
            # against the same allow-list as the Host (as the MCP transport does), not against the Host itself.
            if origin is not None and not (urlsplit(origin).scheme in ("http", "https")
                                           and _host_allowed(urlsplit(origin).netloc, self.allowed_hosts)):
                log.info(json.dumps({"event": "operator_origin_refused", "origin": origin[:100]}))
                return await respond(403, {"error": "origin_not_allowed"})
            body = b""
            while True:
                message = await receive()
                body += message.get("body", b"")
                if len(body) > MAX_BODY:
                    return await respond(413, {"error": "payload_too_large"})
                if not message.get("more_body", False):
                    break
            session_id = _cookie(headers.get("cookie", ""))
            secure = scope.get("scheme") == "https"

            if path == f"{PREFIX}/login" and method == "POST":
                form = parse_qs(body.decode("utf-8", "replace"), max_num_fields=4)
                credential = (form.get("credential") or [None])[0]
                found = await anyio.to_thread.run_sync(self.sessions.login, credential)
                if found is None:
                    return await respond(303, {"error": "login_failed"}, [("location", "/?operator=failed")])
                new_id, session = found
                cookie = (f"{COOKIE}={new_id}; HttpOnly; SameSite=Strict; Path={PREFIX}; "
                          f"Max-Age={int((session.expires_at - session.created_at).total_seconds())}"
                          + ("; Secure" if secure else ""))
                return await respond(303, {"ok": True}, [("location", "/?operator=ok"), ("set-cookie", cookie)])

            if path == f"{PREFIX}/logout" and method == "POST":
                if headers.get("x-cloudguard-voice") != "1":
                    return await respond(403, {"error": "missing_voice_header"})
                self.sessions.logout(session_id)
                return await respond(200, {"ok": True}, [("set-cookie", f"{COOKIE}=; HttpOnly; SameSite=Strict; "
                                                                         f"Path={PREFIX}; Max-Age=0")])

            session = self.sessions.get(session_id)
            if path == f"{PREFIX}/session" and method == "GET":
                kill = self.service.killed()
                return await respond(200, {
                    "enabled": True, "authenticated": session is not None, "mode": self.service.backend.label,
                    "operator": session.subject if session else None,
                    "expires_at": session.expires_at.isoformat() if session else None,
                    "kill_switch": kill is not None and kill != "voice tagging is disabled"})

            if not path.startswith(f"{PREFIX}/voice-tagging/") or method != "POST":
                return await respond(404, {"error": "not_found"})
            if headers.get("x-cloudguard-voice") != "1" or "application/json" not in headers.get("content-type", ""):
                return await respond(403, {"error": "missing_voice_header"})
            if session is None:
                return await respond(401, {"error": "operator_session_required",
                                           "speech": "Sign in as the operator first. Nothing was written."})
            try:
                data = json.loads(body or b"{}")
            except ValueError:
                return await respond(400, {"error": "invalid_json"})
            if not isinstance(data, dict):
                return await respond(400, {"error": "invalid_json"})
            action = path.removeprefix(f"{PREFIX}/voice-tagging/")
            if action == "kill":
                await anyio.to_thread.run_sync(self.service.engage_kill_switch)
                return await respond(200, {"ok": True, "speech": "Voice tagging is now disabled."})
            if action == "cancel":  # always allowed: cancelling can only make things safer
                out = await anyio.to_thread.run_sync(self.service.cancel, session.id_hash,
                                                     str(data.get("challenge_id", "")))
                return await respond(200, out)
            if data.pop("channel", None) != "voice":  # typed chat never calls these routes
                return await respond(403, {"error": "voice_channel_required"})
            if action == "prepare":
                try:
                    spec = TagSpec.model_validate(data)
                except ValidationError:
                    return await respond(400, {"error": "invalid_request",
                                               "speech": "That request isn't valid. Nothing was written."})
                out = await anyio.to_thread.run_sync(self.service.prepare, session.id_hash, spec)
                return await respond(200, out)
            if action == "confirm":
                out = await anyio.to_thread.run_sync(
                    lambda: self.service.confirm(session.id_hash, session.subject, str(data.get("challenge_id", "")),
                                                 data.get("transcript"), data.get("overwrite_transcript")))
                return await respond(200, out)
            return await respond(404, {"error": "not_found"})
        except VoiceTaggingError as exc:
            return await respond(exc.status, {"error": exc.code, "speech": exc.message})
        except Exception:
            log.exception("voice tagging request failed")
            if status == 0:
                return await respond(500, {"error": "internal_error",
                                           "speech": "Something went wrong. Check the server log."})
        finally:
            log.info(json.dumps({"event": "operator_request", "method": method, "path": path, "status": status}))


def _cookie(header: str) -> str | None:
    for part in header.split(";"):
        name, _, value = part.strip().partition("=")
        if name == COOKIE and re.fullmatch(r"[A-Za-z0-9_-]{20,100}", value):
            return value
    return None
