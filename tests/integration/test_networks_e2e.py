"""E2E tests for the WP-9 tools: MCP client -> ASGI app -> fake UniFi API.

Design §36/§37: ``MCP Tool -> Tool Handler -> UniFi Client -> Mock HTTP API ->
normalized response`` over the real ``/mcp`` Streamable HTTP surface
(in-process, no gateway required).

The app is built with the default WP-7 groups plus the three WP-9 groups
(networks, wifi, firewall), registered locally in this file (registry.py is
not modified). Own respx routes registered here win over the ``fake_unifi``
EMPTY_PAGE stubs (respx: last registered route matches first).
"""

from __future__ import annotations

import json

import httpx

from tests.conftest import mcp_client_session
from unifi_mcp.tools.firewall import register_firewall_tools
from unifi_mcp.tools.networks import register_network_tools
from unifi_mcp.tools.registry import TOOL_GROUPS, ToolGroup, always_on
from unifi_mcp.tools.wifi import register_wifi_tools

BASE = "http://gateway.test/proxy/network/integration/v1"

WP9_GROUPS: tuple[ToolGroup, ...] = TOOL_GROUPS + (
    ToolGroup(name="networks", gate=always_on, register=register_network_tools),
    ToolGroup(name="wifi", gate=always_on, register=register_wifi_tools),
    ToolGroup(name="firewall", gate=always_on, register=register_firewall_tools),
)

WP9_TOOL_NAMES = {
    "list_networks",
    "get_network",
    "list_wifi",
    "get_wifi",
    "list_firewall_zones",
    "get_firewall_zone",
}

RAW_PSKS = ("fixture-wifi-psk-12345678", "fixture-wifi-psk-87654321")


def _assert_psk_absent(payload: object) -> None:
    raw = json.dumps(payload, ensure_ascii=False)
    for psk in RAW_PSKS:
        assert psk not in raw


async def test_tools_list_contains_wp9_tools(mcp_app_factory, fake_unifi) -> None:
    async with mcp_app_factory(groups=WP9_GROUPS) as (app, settings):
        token = settings.mcp_auth_token.get_secret_value()
        async with mcp_client_session(app, token) as session:
            tools = await session.list_tools()
    names = {tool.name for tool in tools.tools}
    assert names >= WP9_TOOL_NAMES
    # WP-7 tools stay registered
    assert names >= {"get_system_info", "list_sites", "list_devices"}


async def test_list_networks(mcp_app_factory, fake_unifi, respx_mock, load_fixture) -> None:
    site = fake_unifi.site_id
    respx_mock.get(f"{BASE}/sites/{site}/networks").mock(
        return_value=httpx.Response(200, json=load_fixture("networks.json"))
    )
    async with mcp_app_factory(groups=WP9_GROUPS) as (app, settings):
        token = settings.mcp_auth_token.get_secret_value()
        async with mcp_client_session(app, token) as session:
            result = await session.call_tool(
                "list_networks", {"site_id": site, "limit": 2, "offset": 1}
            )
    assert result.is_error is not True
    payload = result.structured_content
    assert payload["count"] == 3
    assert payload["total_count"] == 3
    assert payload["next_offset"] is None
    first = payload["items"][0]
    assert first["id"] == "net-lan"
    assert first["vlanId"] == 1
    assert first["name"] == "LAN"
    # summary normalization: deep dhcp container pruned, scalars kept
    assert first["ipv4Configuration"]["dhcpConfiguration"] == "…"
    assert first["ipv4Configuration"]["hostIpAddress"] == "172.26.1.1"
    # pagination params forwarded to the API
    params = dict(respx_mock.calls.last.request.url.params)
    assert params["limit"] == "2"
    assert params["offset"] == "1"


async def test_list_networks_default_site_resolution(
    mcp_app_factory, fake_unifi, respx_mock, load_fixture
) -> None:
    """Without an explicit site_id the tool resolves the first gateway site."""
    site = fake_unifi.site_id
    respx_mock.get(f"{BASE}/sites/{site}/networks").mock(
        return_value=httpx.Response(200, json=load_fixture("networks.json"))
    )
    async with mcp_app_factory(groups=WP9_GROUPS) as (app, settings):
        token = settings.mcp_auth_token.get_secret_value()
        async with mcp_client_session(app, token) as session:
            result = await session.call_tool("list_networks", {})
    assert result.is_error is not True
    assert result.structured_content["count"] == 3


async def test_get_network(mcp_app_factory, fake_unifi, respx_mock, load_fixture) -> None:
    site = fake_unifi.site_id
    fixture = load_fixture("networks.json")
    respx_mock.get(f"{BASE}/sites/{site}/networks/net-lan").mock(
        return_value=httpx.Response(200, json=fixture["data"][0])
    )
    async with mcp_app_factory(groups=WP9_GROUPS) as (app, settings):
        token = settings.mcp_auth_token.get_secret_value()
        async with mcp_client_session(app, token) as session:
            result = await session.call_tool(
                "get_network", {"site_id": site, "network_id": "net-lan"}
            )
    assert result.is_error is not True
    payload = result.structured_content
    assert payload["id"] == "net-lan"
    assert payload["vlanId"] == 1
    # detail: full dhcp configuration kept
    assert payload["ipv4Configuration"]["dhcpConfiguration"]["mode"] == "NAT"
    assert payload["ipv4Configuration"]["dhcpConfiguration"]["ipAddressRange"] == {
        "start": "172.26.1.100",
        "stop": "172.26.1.199",
    }


async def test_get_network_unknown_returns_not_found(
    mcp_app_factory, fake_unifi, respx_mock
) -> None:
    site = fake_unifi.site_id
    respx_mock.get(f"{BASE}/sites/{site}/networks/does-not-exist").mock(
        return_value=httpx.Response(
            404, json={"code": "not-found", "message": "Network not found."}
        )
    )
    async with mcp_app_factory(groups=WP9_GROUPS) as (app, settings):
        token = settings.mcp_auth_token.get_secret_value()
        async with mcp_client_session(app, token) as session:
            result = await session.call_tool(
                "get_network", {"site_id": site, "network_id": "does-not-exist"}
            )
    payload = result.structured_content
    assert payload["error"] == "not_found"
    assert payload["resource"] == "network"
    assert payload["query"] == "does-not-exist"


async def test_wifi_psk_never_in_output(
    mcp_app_factory, fake_unifi, respx_mock, load_fixture
) -> None:
    site = fake_unifi.site_id
    fixture = load_fixture("wifi.json")
    respx_mock.get(f"{BASE}/sites/{site}/wifi/broadcasts").mock(
        return_value=httpx.Response(200, json=fixture)
    )
    respx_mock.get(f"{BASE}/sites/{site}/wifi/broadcasts/bc-iot").mock(
        return_value=httpx.Response(200, json=fixture["data"][0])
    )
    async with mcp_app_factory(groups=WP9_GROUPS) as (app, settings):
        token = settings.mcp_auth_token.get_secret_value()
        async with mcp_client_session(app, token) as session:
            listing = await session.call_tool("list_wifi", {"site_id": site})
            detail = await session.call_tool(
                "get_wifi", {"site_id": site, "broadcast_id": "bc-iot"}
            )
    assert listing.is_error is not True
    assert detail.is_error is not True

    list_payload = listing.structured_content
    assert list_payload["count"] == 2
    _assert_psk_absent(list_payload)
    assert list_payload["items"][0]["securityConfiguration"]["passphrase"] == "[REDACTED]"

    detail_payload = detail.structured_content
    _assert_psk_absent(detail_payload)
    assert detail_payload["securityConfiguration"]["type"] == "WPA2_PERSONAL"
    assert detail_payload["securityConfiguration"]["passphrase"] == "[REDACTED]"
    assert detail_payload["name"] == "ID-IoT"


async def test_list_firewall_zones_not_configured(mcp_app_factory, fake_unifi) -> None:
    """fake_unifi already returns the live 400 not-configured response."""
    site = fake_unifi.site_id
    async with mcp_app_factory(groups=WP9_GROUPS) as (app, settings):
        token = settings.mcp_auth_token.get_secret_value()
        async with mcp_client_session(app, token) as session:
            result = await session.call_tool("list_firewall_zones", {"site_id": site})
    payload = result.structured_content
    assert payload["error"] == "unsupported"
    assert "not-configured" in payload["api_code"]
    assert payload["message"]


async def test_get_firewall_zone_not_configured(mcp_app_factory, fake_unifi, respx_mock) -> None:
    site = fake_unifi.site_id
    respx_mock.get(f"{BASE}/sites/{site}/firewall/zones/zone-x").mock(
        return_value=httpx.Response(
            400,
            json={
                "code": "api.firewall.zone-based-firewall-not-configured",
                "message": "Zone Based Firewall is not configured",
            },
        )
    )
    async with mcp_app_factory(groups=WP9_GROUPS) as (app, settings):
        token = settings.mcp_auth_token.get_secret_value()
        async with mcp_client_session(app, token) as session:
            result = await session.call_tool(
                "get_firewall_zone", {"site_id": site, "zone_id": "zone-x"}
            )
    payload = result.structured_content
    assert payload["error"] == "unsupported"
    assert "not-configured" in payload["api_code"]
