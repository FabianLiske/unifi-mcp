"""MCP contract tests over the real ASGI app (in-process).

Verifies design doc §37: connect to ``/mcp``, ``tools/list``, check expected
tools, and auth (401 without a token / 403 with a wrong token). No real UniFi
gateway is required — a pre-built client is injected.
"""

from __future__ import annotations

from contextlib import AsyncExitStack
from typing import Any

import httpx
from mcp.client.session import ClientSession
from mcp.client.streamable_http import streamable_http_client

_MCP_URL = "http://testserver/mcp"


def _http_client(app: Any, token: str | None) -> httpx.AsyncClient:
    headers = {"Authorization": f"Bearer {token}"} if token is not None else {}
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app), headers=headers)


async def test_initialize_and_tools_list(mcp_app) -> None:
    app, settings = mcp_app
    token = settings.mcp_auth_token.get_secret_value()
    async with AsyncExitStack() as stack:
        http_client = await stack.enter_async_context(_http_client(app, token))
        read, write = await stack.enter_async_context(
            streamable_http_client(_MCP_URL, http_client=http_client)
        )
        session = await stack.enter_async_context(ClientSession(read, write))

        result = await session.initialize()
        assert result.server_info.name == "unifi-mcp"
        assert result.instructions is not None
        assert "UniFi" in result.instructions

        tools = await session.list_tools()
        names = [tool.name for tool in tools.tools]
        # WP-7–10: exactly the read-only MVP toolset (10 groups).
        assert set(names) == {
            "get_system_info",
            "list_sites",
            "list_devices",
            "list_clients",
            "get_client",
            "inspect_client_path",
            "list_networks",
            "get_network",
            "list_wifi",
            "get_wifi",
            "list_firewall_zones",
            "get_firewall_zone",
            "list_acl_rules",
            "get_acl_rule",
            "list_traffic_matching_lists",
            "get_traffic_matching_list",
            "list_reference_resources",
        }
        assert len(names) == 17
        # Read-only MVP: no write/action/delete tool may be listed.
        forbidden = ("write", "update", "create", "delete", "restart", "cycle", "reconnect")
        assert not any(word in name for name in names for word in forbidden)
        # Every read-only tool advertises the read-only hint (design §31).
        for tool in tools.tools:
            assert tool.annotations is not None
            assert tool.annotations.read_only_hint is True


async def test_healthz_public(mcp_app) -> None:
    app, _ = mcp_app
    async with _http_client(app, None) as http_client:
        response = await http_client.get("http://testserver/healthz")
        assert response.status_code == 200
        assert response.json() == {"status": "ok"}


async def test_401_without_token(mcp_app) -> None:
    app, _ = mcp_app
    async with _http_client(app, None) as http_client:
        response = await http_client.post(_MCP_URL, json={})
        assert response.status_code == 401


async def test_403_with_wrong_token(mcp_app) -> None:
    app, _ = mcp_app
    async with _http_client(app, "wrong-token") as http_client:
        response = await http_client.post(_MCP_URL, json={})
        assert response.status_code == 403


async def test_readyz_public_and_ready(mcp_app) -> None:
    # ``/info`` is mocked reachable, so readiness is 200; the endpoint is
    # public (no 401/403) and returns the JSON status shape.
    app, _ = mcp_app
    async with _http_client(app, None) as http_client:
        response = await http_client.get("http://testserver/readyz")
        assert response.status_code == 200
        body = response.json()
        assert body["status"] == "ready"
        assert "10.6.101" in body["detail"]
