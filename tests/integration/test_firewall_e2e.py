"""E2E tests for the firewall zone tools on a CONFIGURED gateway (WP-9).

Complements the not-configured 400 cases in test_networks_e2e.py: here the
fake UniFi API answers 200 with zone data (shape: OpenAPI 10.4.57 "Firewall
zone"), over the real ``/mcp`` Streamable HTTP surface (in-process, no
gateway required). Own respx routes registered in the test body win over the
``fake_unifi`` not-configured 400 stub (respx: last-registered route wins).
"""

from __future__ import annotations

import httpx

BASE = "http://gateway.test/proxy/network/integration/v1"
SITE = "site-default"


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
