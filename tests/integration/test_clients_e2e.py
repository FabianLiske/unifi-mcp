"""E2E tests for the WP-8 client tools: MCP client -> ASGI app -> fake UniFi API.

Design §36/§37 pattern; the client routes emulate the live-verified (WP-8)
gateway behaviour: only ``ipAddress.eq`` / ``macAddress.eq`` are honoured as
server-side filters, detail endpoints return bare objects.
"""

from __future__ import annotations

import re
from typing import Any

import httpx
import pytest

from tests.conftest import mcp_client_session
from unifi_mcp.tools.clients import register_client_tools
from unifi_mcp.tools.registry import TOOL_GROUPS, ToolGroup, always_on

CLIENT_GROUP = ToolGroup(name="clients", gate=always_on, register=register_client_tools)

BASE = "http://gateway.test/proxy/network/integration/v1"
SITE = "site-default"


def _apply_gateway_filter(
    data: list[dict[str, Any]], filter_expr: str | None
) -> list[dict[str, Any]]:
    """Emulate the client filter properties the live gateway accepts."""
    if not filter_expr:
        return data
    match = re.fullmatch(r"ipAddress\.eq\('([^']*)'\)", filter_expr)
    if match:
        return [item for item in data if item.get("ipAddress") == match.group(1)]
    match = re.fullmatch(r"macAddress\.eq\('([^']*)'\)", filter_expr)
    if match:
        return [item for item in data if str(item.get("macAddress", "")).lower() == match.group(1)]
    return data


def _register_detail_route(respx_mock: Any, base_path: str, items: list[dict[str, Any]]) -> None:
    """Detail route: 200 for a known id (bare object), else 404."""

    def _detail(request: httpx.Request) -> httpx.Response:
        item_id = request.url.path.rsplit("/", 1)[-1]
        for item in items:
            if item["id"] == item_id:
                return httpx.Response(200, json=item)
        return httpx.Response(404, json={"code": "not-found", "message": "Object not found."})

    respx_mock.route(method="GET", url__regex=re.escape(f"{base_path}/") + r"[\w.-]+").mock(
        side_effect=_detail
    )


def _register_client_routes(respx_mock: Any, load_fixture: Any) -> list[httpx.Request]:
    """Register the WP-8 routes; last-registered wins over fake_unifi stubs."""
    clients_fixture = load_fixture("clients.json")
    devices_fixture = load_fixture("devices.json")
    network_fixture = load_fixture("client_path_networks.json")
    list_requests: list[httpx.Request] = []

    respx_mock.get(f"{BASE}/sites").mock(
        return_value=httpx.Response(200, json=load_fixture("sites.json"))
    )

    def _clients(request: httpx.Request) -> httpx.Response:
        list_requests.append(request)
        data = _apply_gateway_filter(clients_fixture["data"], request.url.params.get("filter"))
        limit = int(request.url.params.get("limit", 25))
        offset = int(request.url.params.get("offset", 0))
        window = data[offset : offset + limit]
        return httpx.Response(
            200,
            json={
                "offset": offset,
                "limit": limit,
                "count": len(window),
                "totalCount": len(data),
                "data": window,
            },
        )

    respx_mock.get(f"{BASE}/sites/{SITE}/clients").mock(side_effect=_clients)
    _register_detail_route(respx_mock, f"{BASE}/sites/{SITE}/clients", clients_fixture["data"])
    _register_detail_route(respx_mock, f"{BASE}/sites/{SITE}/networks", network_fixture["data"])
    _register_detail_route(respx_mock, f"{BASE}/sites/{SITE}/devices", devices_fixture["data"])
    return list_requests


@pytest.fixture
def clients_routes(respx_mock, load_fixture) -> list[httpx.Request]:
    return _register_client_routes(respx_mock, load_fixture)


async def _call(mcp_app_factory, tool: str, arguments: dict[str, Any]) -> dict[str, Any]:
    async with mcp_app_factory(groups=TOOL_GROUPS + (CLIENT_GROUP,)) as (app, settings):
        token = settings.mcp_auth_token.get_secret_value()
        async with mcp_client_session(app, token) as session:
            result = await session.call_tool(tool, arguments)
    assert result.is_error is not True
    return result.structured_content  # type: ignore[no-any-return]


async def test_inspect_client_path_acceptance(clients_routes, mcp_app_factory) -> None:
    """Abnahme: Finde wk-5 und zeig mir, worüber er verbunden ist."""
    payload = await _call(mcp_app_factory, "inspect_client_path", {"hostname": "wk-5"})
    assert payload["client"]["id"] == "client-wk-5"
    assert payload["client"]["name"] == "wk-5"
    assert payload["client"]["type"] == "WIRELESS"
    assert payload["client"]["macAddress"] == "00:11:22:33:44:55"
    assert payload["network"]["name"] == "Clients"
    assert payload["network"]["vlanId"] == 40
    assert payload["attachment"]["device"]["id"] == "dev-ap-1"
    assert payload["attachment"]["device"]["name"] == "UAP AC Lite EG"
    assert payload["attachment"]["port"] is None
    observations = payload["observations"]
    assert "wireless" in observations
    assert "client_connected" in observations
    assert "network:Clients" in observations
    assert "uplink_device:UAP AC Lite EG" in observations


async def test_get_client_ambiguous_hostname(clients_routes, mcp_app_factory) -> None:
    async with mcp_app_factory(groups=TOOL_GROUPS + (CLIENT_GROUP,)) as (app, settings):
        token = settings.mcp_auth_token.get_secret_value()
        async with mcp_client_session(app, token) as session:
            result = await session.call_tool("get_client", {"hostname": "printer"})
    payload = result.structured_content
    assert payload["error"] == "ambiguous_match"
    assert len(payload["matches"]) == 2
    assert {m["id"] for m in payload["matches"]} == {"client-printer-1", "client-printer-2"}
    for match in payload["matches"]:
        assert set(match) <= {"id", "name", "ipAddress", "macAddress"}
    assert payload["message"]


async def test_get_client_by_mac(clients_routes, mcp_app_factory) -> None:
    payload = await _call(mcp_app_factory, "get_client", {"mac": "aa:bb:cc:00:00:01"})
    assert payload["id"] == "client-printer-1"
    assert payload["name"] == "printer"
    assert payload["type"] == "WIRED"
    assert payload["uplinkDeviceId"] == "dev-switch-1"


async def test_list_clients_exact_ip_search_uses_gateway_filter(
    clients_routes, mcp_app_factory
) -> None:
    payload = await _call(mcp_app_factory, "list_clients", {"search": "172.26.40.50"})
    assert payload["count"] == 1
    assert payload["items"][0]["id"] == "client-wk-5"
    request = clients_routes[-1]
    params = dict(request.url.params)
    assert params["filter"] == "ipAddress.eq('172.26.40.50')"
    assert params["limit"] == "50"
    assert params["offset"] == "0"


async def test_list_clients_pagination_and_limit_clamp(clients_routes, mcp_app_factory) -> None:
    payload = await _call(mcp_app_factory, "list_clients", {"limit": 2})
    assert payload["count"] == 2
    assert payload["total_count"] == 5
    assert payload["next_offset"] == 2
    assert payload["items"][0]["id"] == "client-wk-5"
    params = dict(clients_routes[-1].url.params)
    assert params["limit"] == "2"
    assert params["offset"] == "0"
    assert "filter" not in params

    payload = await _call(mcp_app_factory, "list_clients", {"limit": 10000})
    params = dict(clients_routes[-1].url.params)
    assert params["limit"] == "200"
    assert payload["count"] == 5
    assert payload["total_count"] == 5
    assert payload["next_offset"] is None


async def test_list_clients_network_filter(clients_routes, mcp_app_factory) -> None:
    payload = await _call(mcp_app_factory, "list_clients", {"network_id": "net-clients"})
    # 172.26.40.0/24 keeps wk-5, both printers and the iPad; PiVPN (172.26.70.x) drops
    assert {item["id"] for item in payload["items"]} == {
        "client-wk-5",
        "client-printer-1",
        "client-printer-2",
        "client-ipad",
    }
    assert payload["total_count"] == 4
    assert payload["next_offset"] is None


async def test_list_clients_unknown_network_returns_not_found(
    clients_routes, mcp_app_factory
) -> None:
    async with mcp_app_factory(groups=TOOL_GROUPS + (CLIENT_GROUP,)) as (app, settings):
        token = settings.mcp_auth_token.get_secret_value()
        async with mcp_client_session(app, token) as session:
            result = await session.call_tool("list_clients", {"network_id": "net-missing"})
    payload = result.structured_content
    assert payload["error"] == "not_found"
    assert payload["resource"] == "network"
    assert payload["query"] == "net-missing"


async def test_list_clients_unknown_site_returns_not_found(
    clients_routes, mcp_app_factory, respx_mock
) -> None:
    respx_mock.get(f"{BASE}/sites/nope/clients").mock(
        return_value=httpx.Response(404, json={"code": "not-found", "message": "Site not found."})
    )
    async with mcp_app_factory(groups=TOOL_GROUPS + (CLIENT_GROUP,)) as (app, settings):
        token = settings.mcp_auth_token.get_secret_value()
        async with mcp_client_session(app, token) as session:
            result = await session.call_tool("list_clients", {"site_id": "nope"})
    payload = result.structured_content
    assert payload["error"] == "not_found"
    assert payload["api_code"] == "not-found"


async def test_get_client_two_identifiers_returns_validation(
    clients_routes, mcp_app_factory
) -> None:
    async with mcp_app_factory(groups=TOOL_GROUPS + (CLIENT_GROUP,)) as (app, settings):
        token = settings.mcp_auth_token.get_secret_value()
        async with mcp_client_session(app, token) as session:
            result = await session.call_tool(
                "get_client", {"mac": "aa:bb:cc:00:00:01", "ip": "172.26.40.10"}
            )
    payload = result.structured_content
    assert payload["error"] == "validation"
    assert payload["field"] == "identifier"
    assert set(payload["allowed"]) == {"client_id", "mac", "ip", "hostname"}
