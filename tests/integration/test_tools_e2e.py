"""E2E tests for the WP-7 tools: MCP client -> ASGI app -> fake UniFi API.

Design §36/§37: ``MCP Tool -> Tool Handler -> UniFi Client -> Mock HTTP API ->
normalized response``, over the real ``/mcp`` Streamable HTTP surface
(in-process, no gateway required).
"""

from __future__ import annotations

import httpx

from tests.conftest import asgi_lifespan, mcp_client_session
from unifi_mcp.app import create_app

EXPECTED_TOOLS = {
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


async def test_tools_list_contains_exactly_the_readonly_mvp_tools(mcp_session) -> None:
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
    assert caps["dns_policies"]["status"] == "ok"
    assert caps["pending_devices"]["status"] == "ok"
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


async def test_get_device(mcp_session, fake_unifi, respx_mock, load_fixture) -> None:
    base = "http://gateway.test/proxy/network/integration/v1"
    respx_mock.get(f"{base}/sites/{fake_unifi.site_id}/devices/dev-ap-1").mock(
        return_value=httpx.Response(200, json=load_fixture("device_detail.json"))
    )
    async with mcp_session() as session:
        result = await session.call_tool("get_device", {"device_id": "dev-ap-1"})
    assert result.is_error is not True
    payload = result.structured_content
    assert payload["id"] == "dev-ap-1"
    assert payload["name"] == "UAP AC Lite EG"
    assert payload["firmwareVersion"] == "5.1.33"
    assert payload["interfaces"]["radios"][0]["channel"] == 11
    # detail normalization strips internal fields
    assert "metadata" not in payload


async def test_get_device_unknown_returns_not_found(mcp_session, fake_unifi, respx_mock) -> None:
    base = "http://gateway.test/proxy/network/integration/v1"
    respx_mock.get(f"{base}/sites/{fake_unifi.site_id}/devices/dev-missing").mock(
        return_value=httpx.Response(404, json={"code": "not-found", "message": "Device not found."})
    )
    async with mcp_session() as session:
        result = await session.call_tool("get_device", {"device_id": "dev-missing"})
    payload = result.structured_content
    assert payload["error"] == "not_found"
    assert payload["resource"] == "device"
    assert payload["query"] == "dev-missing"


async def test_get_device_statistics(mcp_session, fake_unifi, respx_mock) -> None:
    base = "http://gateway.test/proxy/network/integration/v1"
    respx_mock.get(
        f"{base}/sites/{fake_unifi.site_id}/devices/dev-switch-1/statistics/latest"
    ).mock(
        return_value=httpx.Response(
            200,
            json={
                "uptimeSec": 1124321,
                "lastHeartbeatAt": "2026-09-22T01:34:43Z",
                "nextHeartbeatAt": "2026-09-22T01:35:29Z",
                "loadAverage1Min": 0.57,
                "loadAverage5Min": 0.74,
                "loadAverage15Min": 0.77,
                "cpuUtilizationPct": 11.6,
                "memoryUtilizationPct": 13.2,
                "uplink": {"txRateBps": 48, "rxRateBps": 24},
                "interfaces": {},
            },
        )
    )
    async with mcp_session() as session:
        result = await session.call_tool("get_device_statistics", {"device_id": "dev-switch-1"})
    assert result.is_error is not True
    payload = result.structured_content
    assert payload["uptimeSec"] == 1124321
    assert payload["loadAverage1Min"] == 0.57
    assert payload["uplink"] == {"txRateBps": 48, "rxRateBps": 24}


async def test_list_pending_devices(mcp_session, respx_mock) -> None:
    base = "http://gateway.test/proxy/network/integration/v1"
    requests: list[httpx.Request] = []

    def _pending(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(
            200,
            json={
                "offset": 0,
                "limit": 50,
                "count": 1,
                "totalCount": 1,
                "data": [
                    {
                        "macAddress": "94:2a:6f:26:c6:ca",
                        "ipAddress": "172.26.1.99",
                        "model": "USW-Pro-8-PoE",
                        "state": "PENDING_ADOPTION",
                        "supported": True,
                        "firmwareVersion": "7.5.15",
                        "firmwareUpdatable": False,
                        "features": ["switching"],
                        "adoptionTargetSiteIds": ["site-default"],
                    }
                ],
            },
        )

    respx_mock.get(f"{base}/pending-devices").mock(side_effect=_pending)
    async with mcp_session() as session:
        result = await session.call_tool("list_pending_devices", {"limit": 2, "offset": 1})
    assert result.is_error is not True
    payload = result.structured_content
    assert payload["count"] == 1
    assert payload["total_count"] == 1
    assert payload["items"][0]["macAddress"] == "94:2a:6f:26:c6:ca"
    assert payload["items"][0]["state"] == "PENDING_ADOPTION"
    # top-level endpoint: no /sites/{site} path segment, paging params pass through
    assert requests[-1].url.path == "/proxy/network/integration/v1/pending-devices"
    params = dict(requests[-1].url.params)
    assert params["limit"] == "2"
    assert params["offset"] == "1"


async def test_get_acl_rule_ordering(mcp_session, fake_unifi, respx_mock) -> None:
    base = "http://gateway.test/proxy/network/integration/v1"
    respx_mock.get(f"{base}/sites/{fake_unifi.site_id}/acl-rules/ordering").mock(
        return_value=httpx.Response(
            200,
            json={
                "orderedAclRuleIds": [
                    "acl-rule-allow",
                    "acl-rule-ports",
                    "acl-rule-block",
                ]
            },
        )
    )
    async with mcp_session() as session:
        result = await session.call_tool("get_acl_rule_ordering", {})
    assert result.is_error is not True
    payload = result.structured_content
    assert payload == {"ordered_rule_ids": ["acl-rule-allow", "acl-rule-ports", "acl-rule-block"]}


async def test_list_sites_response_too_large(
    respx_mock, fake_unifi, make_settings, make_client
) -> None:
    """A full app with a tiny MAX_TOOL_RESPONSE_BYTES: every payload fails."""
    settings = make_settings(max_tool_response_bytes=64)
    client = await make_client(settings)
    app = create_app(settings, client=client)
    token = settings.mcp_auth_token.get_secret_value()
    async with asgi_lifespan(app), mcp_client_session(app, token) as session:
        result = await session.call_tool("list_sites", {})
    payload = result.structured_content
    assert payload["error"] == "response_too_large"
