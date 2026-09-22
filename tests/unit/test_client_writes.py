"""Unit tests for the UniFi client write methods (PUT/POST, design §7/§14)."""

from __future__ import annotations

import io
import json

import httpx
import pytest

from unifi_mcp.observability import logging as app_logging
from unifi_mcp.unifi.errors import (
    UniFiConflictError,
    UniFiNotFoundError,
    UniFiRateLimitError,
    UniFiUnavailableError,
    UniFiValidationError,
)

BASE = "http://gateway.test/proxy/network/integration/v1"
OBJ = "/sites/site-default/wifi/broadcasts/bc-1"


def _error_envelope(code: str, message: str) -> dict[str, object]:
    return {"code": code, "message": message, "statusCode": 400, "requestPath": OBJ}


async def test_put_sends_json_body_and_method(respx_mock, make_client) -> None:
    body = {"name": "New SSID", "enabled": True}
    route = respx_mock.put(f"{BASE}{OBJ}").mock(
        return_value=httpx.Response(200, json=body | {"id": "bc-1"})
    )
    client = await make_client()
    result = await client.put(OBJ, json=body)
    assert result["id"] == "bc-1"
    request = route.calls.last.request
    assert request.method == "PUT"
    assert json.loads(request.content) == body
    assert request.headers["x-api-key"] == "test-api-key"


async def test_put_returns_parsed_payload(respx_mock, make_client) -> None:
    respx_mock.put(f"{BASE}{OBJ}").mock(
        return_value=httpx.Response(200, json={"id": "bc-1", "name": "ok"})
    )
    client = await make_client()
    result = await client.put(OBJ, json={"name": "ok"})
    assert result == {"id": "bc-1", "name": "ok"}


async def test_put_retries_on_transient_5xx(respx_mock, make_client) -> None:
    route = respx_mock.put(f"{BASE}{OBJ}").mock(
        side_effect=[
            httpx.Response(500, json=_error_envelope("api.internal", "boom")),
            httpx.Response(200, json={"id": "bc-1"}),
        ]
    )
    client = await make_client()
    result = await client.put(OBJ, json={"name": "ok"})
    assert result == {"id": "bc-1"}
    assert route.call_count == 2  # idempotent: safe to retry


async def test_put_retries_exhaust_then_unavailable(respx_mock, make_client) -> None:
    route = respx_mock.put(f"{BASE}{OBJ}").mock(
        return_value=httpx.Response(503, json=_error_envelope("api.internal", "down"))
    )
    client = await make_client()
    with pytest.raises(UniFiUnavailableError):
        await client.put(OBJ, json={"name": "ok"})
    assert route.call_count == 3  # 1 + 2 retries


async def test_put_no_retry_on_4xx(respx_mock, make_client) -> None:
    route = respx_mock.put(f"{BASE}{OBJ}").mock(
        return_value=httpx.Response(400, json=_error_envelope("api.request.invalid", "bad"))
    )
    client = await make_client()
    with pytest.raises(UniFiValidationError):
        await client.put(OBJ, json={"name": "ok"})
    assert route.call_count == 1  # non-transient: never retried


@pytest.mark.parametrize(
    ("status", "exc_type"),
    [
        (404, UniFiNotFoundError),
        (409, UniFiConflictError),
    ],
)
async def test_put_error_mapping(respx_mock, make_client, status: int, exc_type: type) -> None:
    code = "api.resource.not-found" if status == 404 else "api.resource.conflict"
    respx_mock.put(f"{BASE}{OBJ}").mock(
        return_value=httpx.Response(status, json=_error_envelope(code, "x"))
    )
    client = await make_client()
    with pytest.raises(exc_type):
        await client.put(OBJ, json={"name": "ok"})


async def test_post_sends_json_body_and_method(respx_mock, make_client) -> None:
    body = {"name": "brand new"}
    route = respx_mock.post(f"{BASE}/sites/site-default/wifi/broadcasts").mock(
        return_value=httpx.Response(201, json=body | {"id": "bc-new"})
    )
    client = await make_client()
    result = await client.post("/sites/site-default/wifi/broadcasts", json=body)
    assert result["id"] == "bc-new"
    request = route.calls.last.request
    assert request.method == "POST"
    assert json.loads(request.content) == body


async def test_post_is_never_retried(respx_mock, make_client) -> None:
    # POST is not idempotent: a transient failure must NOT be retried, or a
    # create could be doubled.
    route = respx_mock.post(f"{BASE}/sites/site-default/wifi/broadcasts").mock(
        return_value=httpx.Response(500, json=_error_envelope("api.internal", "boom"))
    )
    client = await make_client()
    with pytest.raises(UniFiUnavailableError):
        await client.post("/sites/site-default/wifi/broadcasts", json={"name": "x"})
    assert route.call_count == 1  # no retry


async def test_post_is_never_retried_on_429(respx_mock, make_client) -> None:
    route = respx_mock.post(f"{BASE}/sites/site-default/wifi/broadcasts").mock(
        return_value=httpx.Response(
            429, json=_error_envelope("api.rate.limit", "slow"), headers={"Retry-After": "0"}
        )
    )
    client = await make_client()
    with pytest.raises(UniFiRateLimitError) as excinfo:
        await client.post("/sites/site-default/wifi/broadcasts", json={"name": "x"})
    assert route.call_count == 1  # no retry, even for the transient 429
    # 429 maps to the typed rate-limit error.
    assert excinfo.value.status_code == 429


async def test_write_body_not_logged(respx_mock, make_client) -> None:
    buf = io.StringIO()
    app_logging.setup_logging("DEBUG", "json", stream=buf)
    respx_mock.put(f"{BASE}{OBJ}").mock(
        return_value=httpx.Response(500, json=_error_envelope("api.internal", "boom"))
    )
    client = await make_client()
    with pytest.raises(UniFiUnavailableError):
        await client.put(OBJ, json={"name": "super-secret-ssid"})
    out = buf.getvalue()
    # the request body (which may carry a WiFi PSK) must never reach the log.
    assert "super-secret-ssid" not in out
    assert "test-api-key" not in out
