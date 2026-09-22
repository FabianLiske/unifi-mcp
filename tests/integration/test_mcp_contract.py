"""MCP contract tests over the real ASGI app (in-process).

Verifies design doc §37: connect to ``/mcp``, ``tools/list``, check expected
tools, and auth (401 without a token / 403 with a wrong token). No real UniFi
gateway is required — a pre-built client is injected.
"""

from __future__ import annotations

from contextlib import AsyncExitStack
from typing import Any

import httpx
import jsonschema
from mcp.client.session import ClientSession
from mcp.client.streamable_http import streamable_http_client
from mcp_types import TextContent

_MCP_URL = "http://testserver/mcp"


def _http_client(app: Any, token: str | None) -> httpx.AsyncClient:
    headers = {"Authorization": f"Bearer {token}"} if token is not None else {}
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app), headers=headers)


def _result_text(result: Any) -> str:
    return "\n".join(c.text for c in result.content if isinstance(c, TextContent))


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
        # WP-7–10, WP-13 firewall policies + WP-13b: exactly the read-only
        # toolset.
        assert set(names) == {
            "get_system_info",
            "list_sites",
            "list_devices",
            "get_device",
            "get_device_statistics",
            "list_pending_devices",
            "list_clients",
            "get_client",
            "inspect_client_path",
            "list_networks",
            "get_network",
            "list_dns_policies",
            "get_dns_policy",
            "list_wifi",
            "get_wifi",
            "list_firewall_zones",
            "get_firewall_zone",
            "list_firewall_policies",
            "get_firewall_policy",
            "get_firewall_policy_ordering",
            "list_acl_rules",
            "get_acl_rule",
            "get_acl_rule_ordering",
            "list_traffic_matching_lists",
            "get_traffic_matching_list",
            "list_reference_resources",
        }
        assert len(names) == 26
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


# --- design §37: outputSchema (point 5) -------------------------------------


async def test_tools_advertise_output_schema(mcp_session) -> None:
    """Representative tools from tools/list carry an object outputSchema."""
    async with mcp_session() as session:
        tools = await session.list_tools()
    by_name = {tool.name: tool for tool in tools.tools}
    representative = (
        "get_system_info",
        "list_sites",
        "list_devices",
        "inspect_client_path",
        "list_firewall_zones",
        "get_firewall_zone",
        "list_firewall_policies",
        "get_firewall_policy",
        "get_firewall_policy_ordering",
    )
    for name in representative:
        schema = by_name[name].output_schema
        assert isinstance(schema, dict), f"{name} does not advertise an outputSchema"
        assert schema.get("type") == "object", f"{name} outputSchema is not an object schema"


async def test_structured_content_conforms_to_output_schema(mcp_session) -> None:
    """The structuredContent of called tools validates against their outputSchema."""
    async with mcp_session() as session:
        tools = await session.list_tools()
        schemas = {tool.name: tool.output_schema for tool in tools.tools}
        sites = await session.call_tool("list_sites", {})
        info = await session.call_tool("get_system_info", {})
        devices = await session.call_tool("list_devices", {})
    for result in (sites, info, devices):
        assert result.is_error is not True
        assert result.structured_content is not None
    jsonschema.validate(sites.structured_content, schemas["list_sites"])
    jsonschema.validate(info.structured_content, schemas["get_system_info"])
    jsonschema.validate(devices.structured_content, schemas["list_devices"])


# --- design §37: input-schema validation (point 7) ---------------------------


async def test_wrong_typed_tool_argument_returns_clean_error_result(mcp_session) -> None:
    """A type mismatch yields an isError result, not a JSON-RPC error or crash.

    Empirically observed (mcp SDK 2.x): the arguments are validated against
    the tool's input schema server-side; a mismatch produces an isError
    CallToolResult carrying the pydantic validation message, while the
    session stays usable (no exception is raised by the client).
    """
    async with mcp_session() as session:
        result = await session.call_tool("list_devices", {"limit": "not-an-int"})
        # the session survives the failed call
        follow_up = await session.call_tool("list_sites", {})
    assert result.is_error is True
    assert result.structured_content is None
    text = _result_text(result)
    assert "list_devices" in text
    assert "validation error" in text
    assert "Traceback" not in text
    assert follow_up.is_error is not True


async def test_unknown_tool_argument_is_ignored_cleanly(mcp_session) -> None:
    """Empirically observed (mcp SDK 2.x): unknown arguments are dropped.

    The generated input schema does not set ``additionalProperties: false``,
    so pydantic ignores the extra key and the tool executes normally — a
    clean success result, no error, no crash.
    """
    async with mcp_session() as session:
        result = await session.call_tool("list_devices", {"bogus": True})
    assert result.is_error is not True
    assert result.structured_content is not None
    assert result.structured_content["total_count"] == 4
