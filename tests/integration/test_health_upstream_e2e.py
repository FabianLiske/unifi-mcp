"""A gateway outage remains visible to callers, not Kubernetes readiness."""

import asyncio

import httpx
import pytest

from tests.conftest import asgi_lifespan, mcp_client_session
from unifi_mcp.app import create_app

BASE = "http://gateway.test/proxy/network/integration/v1"


@pytest.mark.parametrize("failure", ["timeout", "503", "401", "invalid_json"])
async def test_read_tools_report_upstream_error_while_locally_ready(
    failure,
    make_settings,
    make_client,
    respx_mock,
):
    if failure == "timeout":
        response = respx_mock.get(f"{BASE}/info").mock(side_effect=httpx.ReadTimeout("timeout"))
        expected = "unavailable"
    elif failure == "invalid_json":
        response = respx_mock.get(f"{BASE}/info").mock(
            return_value=httpx.Response(200, text="not json")
        )
        expected = "unifi_error"
    else:
        response = respx_mock.get(f"{BASE}/info").mock(
            return_value=httpx.Response(int(failure), json={"message": "gateway failed"})
        )
        expected = "authentication" if failure == "401" else "unavailable"
    settings = make_settings(upstream_check_interval_seconds=60, unifi_max_retries=0)
    client = await make_client(settings)
    app = create_app(settings, client=client)
    async with (
        asgi_lifespan(app),
        httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://testserver"
        ) as http,
    ):
        async with asyncio.timeout(1):
            while (await http.get("/readyz")).json()["upstream"]["status"] == "stale":
                await asyncio.sleep(0.001)
        ready = await http.get("/readyz")
        assert ready.status_code == 200
        upstream = ready.json()["upstream"]
        assert upstream["status"] == "down"
        assert upstream["consecutive_failures"] == 1
        assert upstream["last_error"]
        assert upstream["last_check_age_seconds"] is not None
        # Probes do not initiate more gateway traffic.
        assert response.call_count == 1
        async with mcp_client_session(app, settings.mcp_auth_token.get_secret_value()) as session:
            await session.initialize()
            result = await session.call_tool("get_system_info", {})
        assert result.structured_content["error"] == expected
        assert (await http.get("/readyz")).status_code == 200
        assert (await http.get("/healthz")).status_code == 200


async def test_shutdown_cancels_inflight_diagnostics(make_settings, make_client, respx_mock):
    entered = asyncio.Event()
    cancelled = asyncio.Event()
    calls = 0

    async def slow(request):
        nonlocal calls
        calls += 1
        entered.set()
        try:
            await asyncio.sleep(8)
        finally:
            cancelled.set()
        return httpx.Response(200, json={"applicationVersion": "unused"})

    respx_mock.get(f"{BASE}/info").mock(side_effect=slow)
    settings = make_settings(upstream_check_timeout_seconds=30)
    client = await make_client(settings)
    app = create_app(settings, client=client, groups=())
    async with asyncio.timeout(1):
        async with asgi_lifespan(app):
            await entered.wait()
            assert not cancelled.is_set()
    assert cancelled.is_set()
    assert calls == 1
    assert not client._http.is_closed  # lifespan does not steal caller ownership
