"""E2E tests for the WP-7 tools: MCP client -> ASGI app -> fake UniFi API.

Design §36/§37: ``MCP Tool -> Tool Handler -> UniFi Client -> Mock HTTP API ->
normalized response``, over the real ``/mcp`` Streamable HTTP surface
(in-process, no gateway required).
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import AsyncExitStack, asynccontextmanager
from typing import Any

import httpx
import pytest
from mcp.client.session import ClientSession
from mcp.client.streamable_http import streamable_http_client

from tests.conftest import asgi_lifespan
from unifi_mcp.app import create_app

_MCP_URL = "http://testserver/mcp"

EXPECTED_TOOLS = {"get_system_info", "list_sites", "list_devices"}


@asynccontextmanager
async def _mcp_session(app: Any, token: str) -> AsyncIterator[ClientSession]:
    """Authenticated MCP client session; enter/exit within one test task.

    (An async generator fixture cannot be used here: pytest-asyncio runs
    fixture teardown in a different task, which breaks the client's anyio
    task groups — same reason ``asgi_lifespan`` drives the app in a task.)
    """
    headers = {"Authorization": f"Bearer {token}"}
    http_client = httpx.AsyncClient(transport=httpx.ASGITransport(app=app), headers=headers)
    async with AsyncExitStack() as stack:
        await stack.enter_async_context(http_client)
        read, write = await stack.enter_async_context(
            streamable_http_client(_MCP_URL, http_client=http_client)
        )
        session = await stack.enter_async_context(ClientSession(read, write))
        yield session


@pytest.fixture
def mcp_session(mcp_app, fake_unifi):
    """Factory for MCP client sessions against the full app."""
    app, settings = mcp_app
    token = settings.mcp_auth_token.get_secret_value()

    def _session() -> Any:
        return _mcp_session(app, token)

    return _session


async def test_tools_list_contains_exactly_the_wp7_tools(mcp_session) -> None:
    async with mcp_session() as session:
        tools = await session.list_tools()
    names = {tool.name for tool in tools.tools}
    assert names == EXPECTED_TOOLS


async def test_get_system_info(mcp_session, fake_unifi) -> None:
    async with mcp_session() as session:
        result = await session.call_tool("get_system_info", {})
    assert result.is_error is not True
    info = result.structured_content
    assert info["application_version"] == "10.6.101"
    assert info["mcp_version"]
    assert info["active_site"] == {"id": fake_unifi.site_id, "name": "Default"}
    caps = info["capabilities"]
    assert caps["sites"]["status"] == "ok"
    assert caps["devices"]["status"] == "ok"
    assert caps["clients"]["status"] == "ok"
    assert caps["firewall"]["status"] == "not_configured"


async def test_list_sites(mcp_session) -> None:
    async with mcp_session() as session:
        result = await session.call_tool("list_sites", {})
    assert result.is_error is not True
    payload = result.structured_content
    assert payload["count"] == 2
    assert payload["total_count"] == 2
    assert payload["next_offset"] is None
    assert payload["items"] == [
        {"id": "site-default", "name": "Default"},
        {"id": "site-guest", "name": "Guest"},
    ]


async def test_list_devices_default(mcp_session, fake_unifi) -> None:
    async with mcp_session() as session:
        result = await session.call_tool("list_devices", {})
    assert result.is_error is not True
    payload = result.structured_content
    assert payload["count"] == 4
    assert payload["total_count"] == 4
    assert payload["next_offset"] is None
    first = payload["items"][0]
    assert first["id"] == "dev-gateway"
    assert first["name"] == "Cloud Gateway Ultra"
    assert first["state"] == "ONLINE"
    assert first["features"] == ["switching"]
    # the list endpoint reports interfaces as plain strings (see
    # docs/unifi-api-notes.md §5a) — summary normalization keeps them as-is
    assert first["interfaces"] == ["ports"]
    # pagination parameters: default limit 50, offset 0
    request = fake_unifi.devices_requests[-1]
    assert dict(request.url.params)["limit"] == "50"
    assert dict(request.url.params)["offset"] == "0"
    assert "filter" not in request.url.params


async def test_list_devices_applies_filters(mcp_session, fake_unifi) -> None:
    async with mcp_session() as session:
        result = await session.call_tool(
            "list_devices",
            {
                "site_id": fake_unifi.site_id,
                "device_type": "ap",
                "state": "online",
                "search": "USW",
                "limit": 5,
                "offset": 3,
            },
        )
    assert result.is_error is not True
    request = fake_unifi.devices_requests[-1]
    params = dict(request.url.params)
    assert params["filter"] == (
        "and(features.contains('accessPoint'),state.eq('ONLINE'),name.like('*USW*'))"
    )
    assert params["limit"] == "5"
    assert params["offset"] == "3"


async def test_list_devices_limit_clamped_to_max(mcp_session, fake_unifi) -> None:
    async with mcp_session() as session:
        result = await session.call_tool("list_devices", {"limit": 10000})
    assert result.is_error is not True
    request = fake_unifi.devices_requests[-1]
    assert dict(request.url.params)["limit"] == "200"


async def test_list_devices_invalid_state_returns_validation_error(mcp_session) -> None:
    async with mcp_session() as session:
        result = await session.call_tool("list_devices", {"state": "bogus"})
    payload = result.structured_content
    assert payload["error"] == "validation"
    assert payload["field"] == "state"
    assert "online" in str(payload["allowed"])
    assert payload["message"]


async def test_list_devices_invalid_device_type_returns_validation_error(mcp_session) -> None:
    async with mcp_session() as session:
        result = await session.call_tool("list_devices", {"device_type": "router"})
    payload = result.structured_content
    assert payload["error"] == "validation"
    assert payload["field"] == "device_type"


async def test_list_devices_unknown_site_returns_not_found(
    mcp_session, fake_unifi, respx_mock
) -> None:
    base = "http://gateway.test/proxy/network/integration/v1"
    respx_mock.get(f"{base}/sites/nope/devices").mock(
        return_value=httpx.Response(404, json={"code": "not-found", "message": "Site not found."})
    )
    async with mcp_session() as session:
        result = await session.call_tool("list_devices", {"site_id": "nope"})
    payload = result.structured_content
    assert payload["error"] == "not_found"
    assert payload["api_code"] == "not-found"


async def test_list_devices_gateway_unavailable_returns_unavailable(
    mcp_session, fake_unifi, respx_mock
) -> None:
    base = "http://gateway.test/proxy/network/integration/v1"
    respx_mock.get(f"{base}/sites/flaky/devices").mock(
        return_value=httpx.Response(503, json={"code": "unavailable", "message": "try again"})
    )
    async with mcp_session() as session:
        result = await session.call_tool("list_devices", {"site_id": "flaky"})
    payload = result.structured_content
    assert payload["error"] == "unavailable"


async def test_list_sites_response_too_large(
    respx_mock, fake_unifi, make_settings, make_client
) -> None:
    """A full app with a tiny MAX_TOOL_RESPONSE_BYTES: every payload fails."""
    settings = make_settings(max_tool_response_bytes=64)
    client = await make_client(settings)
    app = create_app(settings, client=client)
    token = settings.mcp_auth_token.get_secret_value()
    async with asgi_lifespan(app), _mcp_session(app, token) as session:
        result = await session.call_tool("list_sites", {})
    payload = result.structured_content
    assert payload["error"] == "response_too_large"
