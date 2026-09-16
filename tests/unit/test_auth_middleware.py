"""Unit tests for unifi_mcp.auth.middleware."""

from __future__ import annotations

from typing import Any

from unifi_mcp.auth.middleware import BearerAuthMiddleware, extract_bearer_token
from unifi_mcp.observability.metrics import AUTH_FAILURES_TOTAL

TOKEN = "secret-token"


async def _inner(scope: dict[str, Any], receive: Any, send: Any) -> None:
    await send(
        {
            "type": "http.response.start",
            "status": 200,
            "headers": [(b"content-type", b"text/plain")],
        }
    )
    await send({"type": "http.response.body", "body": b"inner-ok"})


def _scope(path: str, headers: list[tuple[bytes, bytes]] | None = None) -> dict[str, Any]:
    return {"type": "http", "path": path, "headers": headers or []}


async def _run(app: Any, scope: dict[str, Any]) -> tuple[int, bytes]:
    sent: list[dict[str, Any]] = []

    async def receive() -> dict[str, Any]:
        return {"type": "http.disconnect"}

    async def send(message: dict[str, Any]) -> None:
        sent.append(message)

    await app(scope, receive, send)
    status = 0
    body = b""
    for message in sent:
        if message["type"] == "http.response.start":
            status = message["status"]
        elif message["type"] == "http.response.body":
            body += message.get("body", b"")
    return status, body


def _app(**overrides: Any) -> BearerAuthMiddleware:
    kwargs: dict[str, Any] = {"expected_token": TOKEN}
    kwargs.update(overrides)
    return BearerAuthMiddleware(_inner, **kwargs)


def test_extract_bearer_token() -> None:
    assert extract_bearer_token(_scope("/mcp")) is None
    assert extract_bearer_token(_scope("/mcp", [(b"authorization", b"Bearer abc")])) == "abc"
    assert extract_bearer_token(_scope("/mcp", [(b"authorization", b"Basic abc")])) is None
    assert extract_bearer_token(_scope("/mcp", [(b"authorization", b"Bearer")])) is None
    assert extract_bearer_token(_scope("/mcp", [(b"authorization", b"Bearer   ")])) is None
    # case-insensitive scheme, mixed-case header name
    assert extract_bearer_token(_scope("/mcp", [(b"AUTHORIZATION", b"bearer  xyz ")])) == "xyz"


async def test_missing_token_is_401() -> None:
    before = AUTH_FAILURES_TOTAL._value.get()
    status, _ = await _run(_app(), _scope("/mcp"))
    assert status == 401
    assert AUTH_FAILURES_TOTAL._value.get() == before + 1


async def test_invalid_token_is_403() -> None:
    before = AUTH_FAILURES_TOTAL._value.get()
    status, _ = await _run(_app(), _scope("/mcp", [(b"authorization", b"Bearer wrong")]))
    assert status == 403
    assert AUTH_FAILURES_TOTAL._value.get() == before + 1


async def test_valid_token_passes_through() -> None:
    status, body = await _run(
        _app(), _scope("/mcp", [(b"authorization", f"Bearer {TOKEN}".encode())])
    )
    assert status == 200
    assert body == b"inner-ok"


async def test_health_paths_are_public() -> None:
    for path in ("/healthz", "/readyz"):
        status, body = await _run(_app(), _scope(path))
        assert status == 200
        assert body == b"inner-ok"


async def test_non_http_scope_passes_through() -> None:
    # lifespan scope must be forwarded untouched (no auth, no response).
    forwarded: list[dict[str, Any]] = []

    async def passthrough(scope: dict[str, Any], receive: Any, send: Any) -> None:
        forwarded.append(scope)

    app = BearerAuthMiddleware(passthrough, expected_token=TOKEN)
    scope = {"type": "lifespan", "path": ""}
    await app(scope, lambda: _noop_receive(), lambda m: _noop_send(m))
    assert forwarded == [scope]


async def _noop_receive() -> dict[str, Any]:
    return {"type": "lifespan.shutdown"}


async def _noop_send(message: dict[str, Any]) -> None:
    return None
