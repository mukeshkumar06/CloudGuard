"""BearerAuthApp forwards only authenticated HTTP and lifespan scopes. A WebSocket is closed with 1008 and an unknown
scope type is refused, even when the inner app would serve them."""

import hashlib

import anyio
import pytest
from starlette.applications import Starlette
from starlette.responses import PlainTextResponse
from starlette.routing import Route, WebSocketRoute
from starlette.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

from cloudguard.deploy.http_app import BearerAuthApp, StaticTokenAuthenticator

TOKEN = "scope-test-token-not-a-secret"
ALL_SCOPES = frozenset({"cost:read", "governance:read"})


def inner_app(events):
    async def ws(websocket):
        events.append("websocket reached")
        await websocket.accept()
        await websocket.send_text("inner app data")
        await websocket.close()

    async def ok(request):
        events.append("http reached")
        return PlainTextResponse("ok")

    async def lifespan(app):
        events.append("startup")
        yield
        events.append("shutdown")

    return Starlette(routes=[WebSocketRoute("/ws", ws), Route("/mcp", ok, methods=["POST"])], lifespan=lifespan)


def wrapped(events):
    auth = StaticTokenAuthenticator(hashlib.sha256(TOKEN.encode()).digest(), "static-token", ALL_SCOPES)
    return BearerAuthApp(inner_app(events), auth, "fixture")


@pytest.mark.parametrize("headers", [{}, {"authorization": f"Bearer {TOKEN}"}, {"authorization": "Bearer wrong"}])
def test_websocket_is_closed_with_1008_and_never_reaches_the_inner_app(headers):
    events = []
    with TestClient(wrapped(events)) as c:
        with pytest.raises(WebSocketDisconnect) as exc:
            with c.websocket_connect("/ws", headers=headers) as ws:
                ws.receive_text()
    assert exc.value.code == 1008
    assert "websocket reached" not in events


def test_unsupported_scope_type_is_not_forwarded():
    events = []
    app = wrapped(events)
    sent = []

    async def receive():
        return {"type": "x.request"}

    async def send(message):
        sent.append(message)

    async def main():
        with pytest.raises(RuntimeError, match="unsupported ASGI scope type"):
            await app({"type": "x-custom", "path": "/mcp", "headers": []}, receive, send)
    anyio.run(main)
    assert events == [] and sent == []


def test_lifespan_startup_and_shutdown_still_reach_the_inner_app():
    events = []
    with TestClient(wrapped(events)):
        assert events == ["startup"]
    assert events == ["startup", "shutdown"]


def test_http_still_requires_the_token():
    events = []
    with TestClient(wrapped(events)) as c:
        assert c.post("/mcp").status_code == 401
        assert c.post("/mcp", headers={"authorization": "Bearer wrong"}).status_code == 401
        assert events == ["startup"]
        r = c.post("/mcp", headers={"authorization": f"Bearer {TOKEN}"})
    assert r.status_code == 200 and r.text == "ok" and "http reached" in events
