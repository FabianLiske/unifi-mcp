"""Slow-response tests for the F05 timeout / retry-budget ordering (design §7/§44).

These model a *degraded* gateway — one whose responses are slow — using a real
elapsed delay in each respx side_effect (``await asyncio.sleep(...)``), rather
than an instantaneous error. The acceptance criterion "Tests mit langsamen
Antworten" is covered here:

* a merely *slow* read (within the per-attempt read timeout) completes;
* a read whose response exceeds the read timeout is treated as a transport
  timeout and retried (bounded) for ``GET``;
* a write (``PUT``) whose response exceeds the timeout is *never* retried —
  it is surfaced as :class:`UniFiTransportTimeoutError` for the write layer to
  resolve (design §44);
* a tool call whose per-tool-call budget cannot cover even one slow attempt is
  cut off *before* it starts, with the structured :class:`UniFiToolBudgetError`
  — it never runs past the external 60 s MCP/LiteLLM deadline.

The slow response is modeled as: sleep (the gateway is slow) → raise the
matching ``httpx`` timeout (httpx gave up waiting). The sleep makes the delay
real, so the test actually waits for it.
"""

from __future__ import annotations

import asyncio

import httpx
import pytest

from unifi_mcp import budget
from unifi_mcp.unifi.errors import (
    UniFiToolBudgetError,
    UniFiTransportTimeoutError,
    UniFiUnavailableError,
)

BASE = "http://gateway.test/proxy/network/integration/v1"
OBJ = "/sites/site-default/wifi/broadcasts/bc-1"


# ---------------------------------------------------------------------------
# A merely slow read (within the per-attempt read timeout) completes.
# ---------------------------------------------------------------------------


async def test_slow_read_within_timeout_completes(respx_mock, make_client) -> None:
    """A response that is slow but still within the read timeout is *not* a
    timeout: it completes normally (no spurious retry)."""

    async def slow_then_ok(request: httpx.Request) -> httpx.Response:
        await asyncio.sleep(0.25)  # slow gateway, but the default read timeout is 5 s
        return httpx.Response(200, json={"id": "bc-1"})

    route = respx_mock.get(f"{BASE}{OBJ}").mock(side_effect=slow_then_ok)
    client = await make_client()
    result = await client.get(OBJ)
    assert result == {"id": "bc-1"}
    assert route.call_count == 1  # a slow-but-in-time response must not be retried


# ---------------------------------------------------------------------------
# A read whose response exceeds the read timeout -> transport timeout, retried.
# ---------------------------------------------------------------------------


async def test_slow_read_over_timeout_is_retried(respx_mock, make_settings, make_client) -> None:
    """A response slower than the read timeout is a transport timeout; for a
    ``GET`` it is retried (bounded) and the fast second attempt wins."""
    settings = make_settings(unifi_timeout_seconds=0.15)  # read timeout 150 ms

    async def slow_timeout(request: httpx.Request) -> httpx.Response:
        await asyncio.sleep(0.3)  # gateway slower than the 150 ms read timeout
        raise httpx.ReadTimeout("read timed out", request=request)

    route = respx_mock.get(f"{BASE}{OBJ}").mock(
        side_effect=[slow_timeout, httpx.Response(200, json={"id": "bc-1"})]
    )
    client = await make_client(settings)
    result = await client.get(OBJ)
    assert result == {"id": "bc-1"}
    assert route.call_count == 2  # slow attempt retried once, then success


async def test_slow_read_exhausts_retries(respx_mock, make_settings, make_client) -> None:
    """When *every* attempt is too slow, the bounded retries are exhausted and
    the (last) transport timeout is surfaced — it never loops forever."""
    settings = make_settings(unifi_timeout_seconds=0.1, unifi_max_retries=1)  # 2 attempts total

    async def slow_timeout(request: httpx.Request) -> httpx.Response:
        await asyncio.sleep(0.15)
        raise httpx.ReadTimeout("read timed out", request=request)

    route = respx_mock.get(f"{BASE}{OBJ}").mock(side_effect=slow_timeout)
    client = await make_client(settings)
    # A GET whose every attempt is too slow is "unreachable" (not an ambiguous
    # write) — the bounded retries are exhausted and the last transport error
    # surfaces as a structured UniFiUnavailableError.
    with pytest.raises(UniFiUnavailableError):
        await client.get(OBJ)
    assert route.call_count == 2  # exactly 1 + max_retries attempts


# ---------------------------------------------------------------------------
# A slow write is never retried (ambiguous outcome).
# ---------------------------------------------------------------------------


async def test_slow_write_over_timeout_not_retried(respx_mock, make_settings, make_client) -> None:
    """A ``PUT`` whose response exceeds the timeout is *never* blindly retried
    — the request may have executed server-side. It is surfaced as
    :class:`UniFiTransportTimeoutError` for the write layer to resolve."""
    settings = make_settings(unifi_timeout_seconds=0.1)

    async def slow_timeout(request: httpx.Request) -> httpx.Response:
        await asyncio.sleep(0.2)
        raise httpx.ReadTimeout("read timed out", request=request)

    route = respx_mock.put(f"{BASE}{OBJ}").mock(side_effect=slow_timeout)
    client = await make_client(settings)
    with pytest.raises(UniFiTransportTimeoutError):
        await client.put(OBJ, json={"name": "ok"})
    assert route.call_count == 1  # no blind write retry


# ---------------------------------------------------------------------------
# The per-tool-call budget cuts off a slow call before it overruns 60 s.
# ---------------------------------------------------------------------------


async def test_slow_call_cannot_fit_budget_is_cut_before_start(respx_mock, make_client) -> None:
    """A tool call whose remaining budget cannot cover one attempt's worst
    case (connect + read + pool) is refused *before* the slow request starts —
    the structured :class:`UniFiToolBudgetError` is raised and the external
    60 s deadline is never crossed."""

    async def very_slow(request: httpx.Request) -> httpx.Response:
        await asyncio.sleep(0.2)
        return httpx.Response(200, json={"id": "bc-1"})

    route = respx_mock.get(f"{BASE}{OBJ}").mock(side_effect=very_slow)
    client = await make_client()
    # Default attempt headroom is connect(1) + read(5) + pool(5) = 11 s; give
    # only 5 s so the guard refuses to even start the (slow) attempt.
    with budget.tool_call_budget(5.0), pytest.raises(UniFiToolBudgetError):
        await client.get(OBJ)
    assert route.call_count == 0  # the slow request never started
