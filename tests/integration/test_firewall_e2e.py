"""E2E tests for the zone-based firewall tools on a CONFIGURED gateway
(WP-9 zones, WP-13 policies).

Complements the not-configured 400 cases in test_networks_e2e.py: here the
fake UniFi API answers 200 with zone/policy data (shape: OpenAPI 10.4.57
"Firewall zone" / "Firewall policy"), over the real ``/mcp`` Streamable HTTP
surface (in-process, no gateway required). Own respx routes registered in
the test body win over the ``fake_unifi`` not-configured 400 stub (respx:
last-registered route wins).
"""

from __future__ import annotations

import httpx

BASE = "http://gateway.test/proxy/network/integration/v1"
SITE = "site-default"
ZONE_A = "0a2bd089-61df-49ad-a33c-633879146ade"
ZONE_B = "11b9482d-fbcd-4b08-a92f-ec520447c7ac"


async def test_list_firewall_zones_configured(mcp_session, respx_mock, load_fixture) -> None:
    respx_mock.get(f"{BASE}/sites/{SITE}/firewall/zones").mock(
        return_value=httpx.Response(200, json=load_fixture("firewall_zones.json"))
    )
    async with mcp_session() as session:
        result = await session.call_tool(
            "list_firewall_zones", {"site_id": SITE, "limit": 2, "offset": 1}
        )
    assert result.is_error is not True
    payload = result.structured_content
    assert payload["count"] == 3
    assert payload["total_count"] == 3
    assert payload["next_offset"] is None
    assert [item["id"] for item in payload["items"]] == [
        "0f8fad5b-2c3e-4a6b-9d1e-1a2b3c4d5e6f",
        "1e7ecb4a-3d2f-4b5c-8e0d-2b3c4d5e6f7a",
        "9a8b7c6d-5e4f-4a3b-b2c1-d0e9f8a7b6c5",
    ]
    first = payload["items"][0]
    assert first["name"] == "LAN"
    assert first["networkIds"] == ["net-lan", "net-iot"]
    # summary normalization strips internal fields
    assert "metadata" not in first
    # pagination parameters forwarded to the API
    params = dict(respx_mock.calls.last.request.url.params)
    assert params["limit"] == "2"
    assert params["offset"] == "1"


async def test_get_firewall_zone_configured(mcp_session, respx_mock, load_fixture) -> None:
    zone = load_fixture("firewall_zones.json")["data"][0]
    respx_mock.get(f"{BASE}/sites/{SITE}/firewall/zones/{zone['id']}").mock(
        return_value=httpx.Response(200, json=zone)
    )
    async with mcp_session() as session:
        result = await session.call_tool("get_firewall_zone", {"zone_id": zone["id"]})
    assert result.is_error is not True
    payload = result.structured_content
    assert payload["id"] == zone["id"]
    assert payload["name"] == "LAN"
    assert payload["networkIds"] == ["net-lan", "net-iot"]
    # detail normalization strips internal fields
    assert "metadata" not in payload


async def test_get_firewall_zone_unknown_returns_not_found(
    mcp_session, respx_mock, load_fixture
) -> None:
    respx_mock.get(f"{BASE}/sites/{SITE}/firewall/zones/does-not-exist").mock(
        return_value=httpx.Response(404, json={"code": "not-found", "message": "Zone not found."})
    )
    async with mcp_session() as session:
        result = await session.call_tool("get_firewall_zone", {"zone_id": "does-not-exist"})
    payload = result.structured_content
    assert payload["error"] == "not_found"
    assert payload["resource"] == "firewall_zone"
    assert payload["query"] == "does-not-exist"
    assert payload["message"]


# --- policies (WP-13) -----------------------------------------------------------


async def test_list_firewall_policies_configured(mcp_session, respx_mock, load_fixture) -> None:
    respx_mock.get(f"{BASE}/sites/{SITE}/firewall/policies").mock(
        return_value=httpx.Response(200, json=load_fixture("firewall_policies.json"))
    )
    async with mcp_session() as session:
        result = await session.call_tool(
            "list_firewall_policies",
            {"site_id": SITE, "name": "nas", "origin": "user", "source_zone_id": ZONE_A},
        )
    assert result.is_error is not True
    payload = result.structured_content
    assert payload["count"] == 4
    assert payload["total_count"] == 4
    assert payload["next_offset"] is None
    # filter clauses forwarded to the gateway, zone UUID unquoted
    params = dict(respx_mock.calls.last.request.url.params)
    assert params["filter"] == (
        f"and(name.like('*nas*'),metadata.origin.eq('USER_DEFINED'),source.zoneId.eq({ZONE_A}))"
    )
    items = payload["items"]
    assert "metadata" not in items[0]
    # system-derived policy without 'id' passes through the MCP surface
    assert "id" not in items[0]
    assert items[1]["id"] == "5bf82e53-61ca-4053-85b6-d90fd3f38dab"
    assert items[1]["source"]["zoneId"] == ZONE_B
    # deep traffic filters pruned at the summary level
    assert items[1]["destination"]["trafficFilter"] == "…"


async def test_list_firewall_policies_not_configured_unsupported(mcp_session) -> None:
    """The default fake gateway has the zone-based firewall not configured."""
    async with mcp_session() as session:
        result = await session.call_tool("list_firewall_policies", {"site_id": SITE})
    payload = result.structured_content
    assert payload["error"] == "unsupported"
    assert "not-configured" in payload["api_code"]


async def test_get_firewall_policy_configured(mcp_session, respx_mock, load_fixture) -> None:
    policy = load_fixture("firewall_policies.json")["data"][2]
    respx_mock.get(f"{BASE}/sites/{SITE}/firewall/policies/{policy['id']}").mock(
        return_value=httpx.Response(200, json=policy)
    )
    async with mcp_session() as session:
        result = await session.call_tool("get_firewall_policy", {"policy_id": policy["id"]})
    assert result.is_error is not True
    payload = result.structured_content
    assert payload["id"] == policy["id"]
    assert payload["name"] == "060-0-NAS-ALLOW-VPN"
    # detail normalization strips internal fields, keeps the full filters
    assert "metadata" not in payload
    assert payload["destination"]["trafficFilter"]["portFilter"]["items"][0]["value"] == 80


async def test_get_firewall_policy_unknown_returns_not_found(mcp_session, respx_mock) -> None:
    respx_mock.get(f"{BASE}/sites/{SITE}/firewall/policies/does-not-exist").mock(
        return_value=httpx.Response(404, json={"code": "not-found", "message": "Policy not found."})
    )
    async with mcp_session() as session:
        result = await session.call_tool("get_firewall_policy", {"policy_id": "does-not-exist"})
    payload = result.structured_content
    assert payload["error"] == "not_found"
    assert payload["resource"] == "firewall_policy"
    assert payload["query"] == "does-not-exist"


async def test_get_firewall_policy_ordering_configured(mcp_session, respx_mock) -> None:
    respx_mock.get(f"{BASE}/sites/{SITE}/firewall/policies/ordering").mock(
        return_value=httpx.Response(
            200,
            json={
                "orderedFirewallPolicyIds": {
                    "beforeSystemDefined": ["11111111-1111-4111-8111-111111111111"],
                    "afterSystemDefined": [],
                }
            },
        )
    )
    async with mcp_session() as session:
        result = await session.call_tool(
            "get_firewall_policy_ordering",
            {"source_zone_id": ZONE_A, "destination_zone_id": ZONE_B, "site_id": SITE},
        )
    assert result.is_error is not True
    payload = result.structured_content
    assert payload == {
        "source_zone_id": ZONE_A,
        "destination_zone_id": ZONE_B,
        "before_system_defined": ["11111111-1111-4111-8111-111111111111"],
        "after_system_defined": [],
    }
    params = dict(respx_mock.calls.last.request.url.params)
    assert params["sourceFirewallZoneId"] == ZONE_A
    assert params["destinationFirewallZoneId"] == ZONE_B


async def test_get_firewall_policy_ordering_not_configured_unsupported(
    mcp_session, respx_mock
) -> None:
    respx_mock.get(f"{BASE}/sites/{SITE}/firewall/policies/ordering").mock(
        return_value=httpx.Response(
            400,
            json={
                "code": "api.firewall.zone-based-firewall-not-configured",
                "message": "Zone-based firewall is not configured.",
            },
        )
    )
    async with mcp_session() as session:
        result = await session.call_tool(
            "get_firewall_policy_ordering",
            {"source_zone_id": ZONE_A, "destination_zone_id": ZONE_B},
        )
    payload = result.structured_content
    assert payload["error"] == "unsupported"
    assert "not-configured" in payload["api_code"]


async def test_get_firewall_policy_ordering_invalid_zone_id(mcp_session) -> None:
    """Client-side UUID validation short-circuits before any API call."""
    async with mcp_session() as session:
        result = await session.call_tool(
            "get_firewall_policy_ordering",
            {"source_zone_id": "not-a-uuid", "destination_zone_id": ZONE_B},
        )
    payload = result.structured_content
    assert payload["error"] == "validation"
    assert payload["field"] == "source_zone_id"
