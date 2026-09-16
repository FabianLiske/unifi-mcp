"""Unit tests for the UniFi client core (design doc section 35)."""

from __future__ import annotations

import io

import httpx
import pytest

from unifi_mcp.observability import logging as app_logging
from unifi_mcp.unifi.errors import (
    UniFiAuthenticationError,
    UniFiAuthorizationError,
    UniFiConflictError,
    UniFiError,
    UniFiNotFoundError,
    UniFiRateLimitError,
    UniFiResponseTooLargeError,
    UniFiUnavailableError,
    UniFiValidationError,
)

BASE = "http://gateway.test/proxy/network/integration/v1"


def _error_envelope(code: str, message: str) -> dict[str, object]:
    return {"code": code, "message": message, "statusCode": 400, "requestPath": "/x"}


async def test_auth_header_and_base_path(respx_mock, make_client) -> None:
    route = respx_mock.get(f"{BASE}/info").mock(
        return_value=httpx.Response(200, json={"applicationVersion": "10.6.101"})
    )
    client = await make_client()
    info = await client.get_application_info()
    assert info.application_version == "10.6.101"
    request = route.calls.last.request
    assert request.headers["x-api-key"] == "test-api-key"
    assert "authorization" not in request.headers
    assert str(request.url) == f"{BASE}/info"


async def test_timeouts_explicit(respx_mock, make_client) -> None:
    client = await make_client()
    timeout = client._http.timeout
    assert timeout.connect == 1.0
    assert timeout.read == 5.0
    assert timeout.write == 5.0
    assert timeout.pool == 1.0


async def test_get_list_parses_envelope(respx_mock, make_client) -> None:
    respx_mock.get(f"{BASE}/sites/abc/devices").mock(
        return_value=httpx.Response(
            200,
            json={
                "offset": 0,
                "limit": 25,
                "count": 1,
                "totalCount": 47,
                "data": [{"id": "d1", "name": "USW"}],
            },
        )
    )
    client = await make_client()
    page = await client.get_list("/sites/abc/devices")
    assert page.total_count == 47
    assert page.count == 1
    assert page.data[0]["name"] == "USW"


async def test_get_list_rejects_non_envelope(respx_mock, make_client) -> None:
    respx_mock.get(f"{BASE}/sites/abc/devices").mock(return_value=httpx.Response(200, json=["x"]))
    client = await make_client()
    with pytest.raises(UniFiError, match="list envelope"):
        await client.get_list("/sites/abc/devices")


@pytest.mark.parametrize(
    ("status", "exc_type", "code"),
    [
        (401, UniFiAuthenticationError, "api.authentication.missing-credentials"),
        (403, UniFiAuthorizationError, "api.authorization.denied"),
        (404, UniFiNotFoundError, "api.resource.not-found"),
        (409, UniFiConflictError, "api.resource.conflict"),
        (400, UniFiValidationError, "api.request.invalid-filter"),
    ],
)
async def test_error_mapping(
    respx_mock, make_client, status: int, exc_type: type[UniFiError], code: str
) -> None:
    route = respx_mock.get(f"{BASE}/info").mock(
        return_value=httpx.Response(status, json=_error_envelope(code, "boom"))
    )
    client = await make_client()
    with pytest.raises(exc_type) as excinfo:
        await client.get_application_info()
    assert excinfo.value.api_code == code
    assert excinfo.value.status_code == status
    # non-transient statuses are never retried
    assert route.call_count == 1


async def test_429_retries_then_rate_limit(respx_mock, make_client) -> None:
    route = respx_mock.get(f"{BASE}/info").mock(
        return_value=httpx.Response(
            429, json=_error_envelope("api.rate.limit", "slow down"), headers={"Retry-After": "0"}
        )
    )
    client = await make_client()
    with pytest.raises(UniFiRateLimitError) as excinfo:
        await client.get_application_info()
    assert excinfo.value.retry_after == 0.0
    assert route.call_count == 3  # 1 + 2 retries


async def test_429_then_success(respx_mock, make_client) -> None:
    route = respx_mock.get(f"{BASE}/info").mock(
        side_effect=[
            httpx.Response(
                429,
                json=_error_envelope("api.rate.limit", "slow down"),
                headers={"Retry-After": "0"},
            ),
            httpx.Response(200, json={"applicationVersion": "10.6.101"}),
        ]
    )
    client = await make_client()
    info = await client.get_application_info()
    assert info.application_version == "10.6.101"
    assert route.call_count == 2


async def test_5xx_retries_then_unavailable(respx_mock, make_client) -> None:
    route = respx_mock.get(f"{BASE}/info").mock(
        return_value=httpx.Response(503, json=_error_envelope("api.internal", "boom"))
    )
    client = await make_client()
    with pytest.raises(UniFiUnavailableError) as excinfo:
        await client.get_application_info()
    assert excinfo.value.status_code == 503
    assert route.call_count == 3  # 1 + 2 retries


async def test_5xx_then_success(respx_mock, make_client) -> None:
    route = respx_mock.get(f"{BASE}/info").mock(
        side_effect=[
            httpx.Response(500, json=_error_envelope("api.internal", "boom")),
            httpx.Response(200, json={"applicationVersion": "10.6.101"}),
        ]
    )
    client = await make_client()
    info = await client.get_application_info()
    assert info.application_version == "10.6.101"
    assert route.call_count == 2


async def test_connect_error_retries_then_unavailable(respx_mock, make_client) -> None:
    route = respx_mock.get(f"{BASE}/info").mock(side_effect=httpx.ConnectError("no route to host"))
    client = await make_client()
    with pytest.raises(UniFiUnavailableError):
        await client.get_application_info()
    assert route.call_count == 3


async def test_read_timeout_retries_then_unavailable(respx_mock, make_client) -> None:
    route = respx_mock.get(f"{BASE}/info").mock(side_effect=httpx.ReadTimeout("read timed out"))
    client = await make_client()
    with pytest.raises(UniFiUnavailableError):
        await client.get_application_info()
    assert route.call_count == 3


async def test_response_size_cap_declared(respx_mock, make_settings, make_client) -> None:
    route = respx_mock.get(f"{BASE}/info").mock(
        return_value=httpx.Response(200, content=b"x" * 300)
    )
    client = await make_client(make_settings(max_tool_response_bytes=100))
    with pytest.raises(UniFiResponseTooLargeError):
        await client.get_application_info()
    assert route.call_count == 1  # size cap is not retried


class _FakeStream:
    """Minimal stand-in for a streaming httpx response (size-cap logic)."""

    def __init__(self, chunks: list[bytes]) -> None:
        self.headers = httpx.Headers()
        self._chunks = chunks
        self.closed = False

    async def aiter_bytes(self):
        for chunk in self._chunks:
            yield chunk

    async def aclose(self) -> None:
        self.closed = True


async def test_response_size_cap_streamed(respx_mock, make_settings, make_client) -> None:
    client = await make_client(make_settings(max_tool_response_bytes=100))
    fake = _FakeStream([b"x" * 60, b"x" * 60])
    with pytest.raises(UniFiResponseTooLargeError):
        await client._read_body_with_cap(fake, 100)
    assert fake.closed is True


async def test_malformed_json(respx_mock, make_client) -> None:
    respx_mock.get(f"{BASE}/info").mock(return_value=httpx.Response(200, content=b"not json"))
    client = await make_client()
    with pytest.raises(UniFiError, match="invalid JSON"):
        await client.get_application_info()


async def test_no_secrets_in_logs(respx_mock, make_client) -> None:
    respx_mock.get(f"{BASE}/info").mock(
        return_value=httpx.Response(401, json=_error_envelope("api.auth.bad", "bad"))
    )
    buf = io.StringIO()
    app_logging.setup_logging("DEBUG", "json", stream=buf)
    client = await make_client()
    with pytest.raises(UniFiAuthenticationError):
        await client.get_application_info()
    out = buf.getvalue()
    assert "test-api-key" not in out
    assert "test-mcp-token" not in out


async def test_429_without_retry_after_still_retries(respx_mock, make_client) -> None:
    route = respx_mock.get(f"{BASE}/info").mock(
        return_value=httpx.Response(429, json=_error_envelope("api.rate.limit", "slow down"))
    )
    client = await make_client()
    with pytest.raises(UniFiRateLimitError) as excinfo:
        await client.get_application_info()
    assert excinfo.value.retry_after is None
    assert route.call_count == 3
