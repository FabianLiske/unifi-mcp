"""Unit tests for the F05 timeout / retry-budget ordering (design §7/§44).

Covers the write-safety retry rules, the bounded backoff, the per-attempt and
per-tool-call budget guards, and the derived pool limits:

* GET  — retried on transient status *and* transport errors;
* PUT  — retried on transient *status* only; a *transport* failure is never
  retried (the write may have executed) and is surfaced as
  :class:`UniFiTransportTimeoutError`;
* POST — never retried at all;
* pool saturation — bounded, and a structured :class:`UniFiPoolTimeoutError`
  for non-reads;
* a single tool call that would overrun its wall-clock budget reports a
  structured :class:`UniFiToolBudgetError` instead of hanging past the
  external 60 s MCP/LiteLLM deadline.
"""

from __future__ import annotations

import httpx
import pytest

from unifi_mcp import budget
from unifi_mcp.unifi.errors import (
    UniFiPoolTimeoutError,
    UniFiToolBudgetError,
    UniFiTransportTimeoutError,
    UniFiUnavailableError,
)

BASE = "http://gateway.test/proxy/network/integration/v1"
OBJ = "/sites/site-default/wifi/broadcasts/bc-1"


def _error_envelope(code: str, message: str) -> dict[str, object]:
    return {"code": code, "message": message, "statusCode": 500, "requestPath": OBJ}


# ---------------------------------------------------------------------------
# PUT: transport failure -> never retried, ambiguous outcome surfaced
# ---------------------------------------------------------------------------


async def test_put_transport_error_not_retried_is_ambiguous(respx_mock, make_client) -> None:
    """A PUT that times out in the transport is *not* blindly retried: the
    request may have executed server-side, so it is surfaced as
    :class:`UniFiTransportTimeoutError` for the write layer to resolve."""
    route = respx_mock.put(f"{BASE}{OBJ}").mock(side_effect=httpx.ReadTimeout("read timed out"))
    client = await make_client()
    with pytest.raises(UniFiTransportTimeoutError):
        await client.put(OBJ, json={"name": "ok"})
    assert route.call_count == 1  # no blind write retry


async def test_put_connect_error_not_retried_is_ambiguous(respx_mock, make_client) -> None:
    route = respx_mock.put(f"{BASE}{OBJ}").mock(
        side_effect=httpx.ConnectError("connection dropped")
    )
    client = await make_client()
    with pytest.raises(UniFiTransportTimeoutError):
        await client.put(OBJ, json={"name": "ok"})
    assert route.call_count == 1


# ---------------------------------------------------------------------------
# PUT: transient *status* (gateway answered) -> safe to retry
# ---------------------------------------------------------------------------


async def test_put_5xx_status_is_retried(respx_mock, make_client) -> None:
    """A transient status is retried for PUT: the gateway answered, so the
    write provably did not execute (idempotent full-replace PUT)."""
    route = respx_mock.put(f"{BASE}{OBJ}").mock(
        side_effect=[
            httpx.Response(503, json=_error_envelope("api.internal", "down")),
            httpx.Response(200, json={"id": "bc-1"}),
        ]
    )
    client = await make_client()
    result = await client.put(OBJ, json={"name": "ok"})
    assert result == {"id": "bc-1"}
    assert route.call_count == 2


# ---------------------------------------------------------------------------
# GET: transport errors are retried (the request almost certainly never
# reached the gateway)
# ---------------------------------------------------------------------------


async def test_get_transport_error_is_retried(respx_mock, make_client) -> None:
    route = respx_mock.get(f"{BASE}{OBJ}").mock(
        side_effect=[
            httpx.ConnectError("no route to host"),
            httpx.Response(200, json={"id": "bc-1"}),
        ]
    )
    client = await make_client()
    result = await client.get(OBJ)
    assert result == {"id": "bc-1"}
    assert route.call_count == 2


# ---------------------------------------------------------------------------
# POST: never retried at all
# ---------------------------------------------------------------------------


async def test_post_transport_error_not_retried_is_ambiguous(respx_mock, make_client) -> None:
    route = respx_mock.post(f"{BASE}{OBJ}").mock(side_effect=httpx.ReadTimeout("read timed out"))
    client = await make_client()
    with pytest.raises(UniFiTransportTimeoutError):
        await client.post(OBJ, json={"name": "created"})
    assert route.call_count == 1  # non-idempotent: a doubled create is worse


async def test_post_5xx_status_not_retried(respx_mock, make_client) -> None:
    """Even a transient *status* is not retried for POST (non-idempotent)."""
    route = respx_mock.post(f"{BASE}{OBJ}").mock(
        return_value=httpx.Response(503, json=_error_envelope("api.internal", "down"))
    )
    client = await make_client()
    with pytest.raises(UniFiUnavailableError):
        await client.post(OBJ, json={"name": "created"})
    assert route.call_count == 1


# ---------------------------------------------------------------------------
# Pool saturation: bounded + structured
# ---------------------------------------------------------------------------


async def test_pool_timeout_on_get_retried_then_ok(respx_mock, make_client) -> None:
    route = respx_mock.get(f"{BASE}{OBJ}").mock(
        side_effect=[httpx.PoolTimeout("pool saturated"), httpx.Response(200, json={"id": "x"})]
    )
    client = await make_client()
    result = await client.get(OBJ)
    assert result == {"id": "x"}
    assert route.call_count == 2


async def test_pool_timeout_on_put_is_structured_not_retried(respx_mock, make_client) -> None:
    """A pool timeout on a *write* is surfaced as a bounded, structured
    :class:`UniFiPoolTimeoutError` (the pool is full, the gateway may be
    fine) — never silently retried past the budget."""
    route = respx_mock.put(f"{BASE}{OBJ}").mock(side_effect=httpx.PoolTimeout("pool saturated"))
    client = await make_client()
    with pytest.raises(UniFiPoolTimeoutError):
        await client.put(OBJ, json={"name": "ok"})
    assert route.call_count == 1


async def test_pool_limits_derived_from_concurrency(respx_mock, make_settings, make_client) -> None:
    """max_connections is derived from the bounded concurrency (never smaller
    than the in-flight requests the client allows)."""
    settings = make_settings(unifi_max_concurrent_requests=8)
    client = await make_client(settings)
    pool = client._http._transport._pool
    assert pool._max_connections == 16  # 2 * 8
    assert pool._max_keepalive_connections == 8


# ---------------------------------------------------------------------------
# Per-tool-call budget: overrun -> structured error, never a hang
# ---------------------------------------------------------------------------


async def test_budget_exhausted_before_request_raises(respx_mock, make_client) -> None:
    """A tool call whose remaining budget cannot cover one attempt (connect +
    read + pool) reports a structured :class:`UniFiToolBudgetError` instead of
    running the request past the external 60 s deadline."""
    client = await make_client()
    # 11 s headroom is needed (connect 1 + read 5 + pool 5); give only 5 s.
    with budget.tool_call_budget(5.0):
        respx_mock.get(f"{BASE}{OBJ}").mock(return_value=httpx.Response(200, json={"id": "x"}))
        with pytest.raises(UniFiToolBudgetError):
            await client.get(OBJ)


async def test_budget_allows_request_when_headroom_fits(respx_mock, make_client) -> None:
    client = await make_client()
    with budget.tool_call_budget(40.0):  # comfortably covers one attempt
        respx_mock.get(f"{BASE}{OBJ}").mock(return_value=httpx.Response(200, json={"id": "x"}))
        assert await client.get(OBJ) == {"id": "x"}
