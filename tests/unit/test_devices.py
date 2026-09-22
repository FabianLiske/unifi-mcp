"""Unit tests for unifi_mcp.tools.devices (filter builder, registration)."""

from __future__ import annotations

import httpx
import pytest
from mcp.server.mcpserver import MCPServer

from unifi_mcp.tools.devices import (
    ALLOWED_DEVICE_TYPES,
    ALLOWED_STATES,
    build_device_filter,
    clamp_params,
    register_device_tools,
)
from unifi_mcp.tools.errors import InvalidValueError

# --- build_device_filter -------------------------------------------------------


def test_no_filters_returns_none() -> None:
    assert build_device_filter() is None
    assert build_device_filter(device_type="", state="  ", search="") is None


def test_single_clauses() -> None:
    assert build_device_filter(device_type="switch") == "features.contains('switching')"
    assert build_device_filter(state="online") == "state.eq('ONLINE')"
    assert build_device_filter(search="USW") == "name.like('*USW*')"


def test_device_type_aliases() -> None:
    assert build_device_filter(device_type="ap") == "features.contains('accessPoint')"
    assert build_device_filter(device_type="access_point") == "features.contains('accessPoint')"
    assert build_device_filter(device_type="accesspoint") == "features.contains('accessPoint')"
    assert build_device_filter(device_type="GATEWAY") == "features.contains('gateway')"
    # raw API values pass through
    assert build_device_filter(device_type="switching") == "features.contains('switching')"


def test_state_case_insensitive() -> None:
    assert build_device_filter(state="Online") == "state.eq('ONLINE')"
    assert build_device_filter(state="OFFLINE") == "state.eq('OFFLINE')"
    assert build_device_filter(state="pending") == "state.eq('PENDING')"


def test_combined_filter_uses_and() -> None:
    expr = build_device_filter(device_type="switch", state="online", search="USW")
    assert expr == "and(features.contains('switching'),state.eq('ONLINE'),name.like('*USW*'))"


def test_search_strips_quotes_and_surrounding_whitespace() -> None:
    assert build_device_filter(search="  USW Pro ") == "name.like('*USW Pro*')"
    assert build_device_filter(search="o'brien") == "name.like('*obrien*')"
    assert build_device_filter(search="''") is None


@pytest.mark.parametrize("value", ["router", "firewall"])
def test_invalid_device_type_rejected(value: str) -> None:
    with pytest.raises(InvalidValueError) as excinfo:
        build_device_filter(device_type=value)
    assert excinfo.value.code == "validation"
    assert list(ALLOWED_DEVICE_TYPES) == excinfo.value.fields["allowed"]


@pytest.mark.parametrize("value", ["online-only", "unknown"])
def test_invalid_state_rejected(value: str) -> None:
    with pytest.raises(InvalidValueError) as excinfo:
        build_device_filter(state=value)
    assert excinfo.value.code == "validation"
    assert list(ALLOWED_STATES) == excinfo.value.fields["allowed"]


# --- clamp_params ---------------------------------------------------------------


def test_clamp_params_defaults_and_caps() -> None:
    assert clamp_params(None, None, max_limit=200) == {"limit": 50, "offset": 0}
    assert clamp_params(1000, None, max_limit=200) == {"limit": 200, "offset": 0}
    assert clamp_params(0, 7, max_limit=200) == {"limit": 50, "offset": 7}


def test_clamp_params_negative_offset_rejected() -> None:
    with pytest.raises(InvalidValueError):
        clamp_params(None, -1, max_limit=200)


# --- registration ----------------------------------------------------------------


async def test_register_device_tools(make_settings) -> None:
    settings = make_settings()
    server = MCPServer(name="t")
    register_device_tools(server, object(), settings)  # type: ignore[arg-type]
    tools = await server.list_tools()
    names = [tool.name for tool in tools]
    assert names == [
        "list_devices",
        "get_device",
        "get_device_statistics",
        "list_pending_devices",
    ]
    for tool in tools:
        assert tool.annotations is not None and tool.annotations.read_only_hint is True
    params = {tool.name: set(tool.input_schema["properties"]) for tool in tools}
    assert params["list_devices"] == {
        "site_id",
        "device_type",
        "state",
        "search",
        "limit",
        "offset",
    }
    assert params["get_device"] == {"device_id", "site_id"}
    assert params["get_device_statistics"] == {"device_id", "site_id"}
    # pending-devices is top-level: no site_id parameter
    assert params["list_pending_devices"] == {"limit", "offset"}


# --- tool behavior (real client, respx-mocked API) ------------------------------


async def test_get_device_returns_normalized_detail(
    make_settings, make_client, respx_mock, load_fixture
) -> None:
    settings = make_settings()
    client = await make_client(settings)
    base = "http://gateway.test/proxy/network/integration/v1"
    respx_mock.get(f"{base}/sites/site-x/devices/dev-ap-1").mock(
        return_value=httpx.Response(200, json=load_fixture("device_detail.json"))
    )
    server = MCPServer(name="t")
    register_device_tools(server, client, settings)
    result = await server.call_tool("get_device", {"device_id": "dev-ap-1", "site_id": "site-x"})
    payload = result.structured_content
    assert payload["id"] == "dev-ap-1"
    assert payload["name"] == "UAP AC Lite EG"
    assert payload["features"] == {"accessPoint": {}}
    assert len(payload["interfaces"]["radios"]) == 2
    # detail level: internal fields (metadata) are stripped
    assert "metadata" not in payload


async def test_get_device_unknown_returns_not_found(make_settings, make_client, respx_mock) -> None:
    settings = make_settings()
    client = await make_client(settings)
    base = "http://gateway.test/proxy/network/integration/v1"
    respx_mock.get(f"{base}/sites/site-x/devices/dev-missing").mock(
        return_value=httpx.Response(404, json={"code": "not-found", "message": "nope"})
    )
    server = MCPServer(name="t")
    register_device_tools(server, client, settings)
    result = await server.call_tool("get_device", {"device_id": "dev-missing", "site_id": "site-x"})
    payload = result.structured_content
    assert payload["error"] == "not_found"
    assert payload["resource"] == "device"
    assert payload["query"] == "dev-missing"


async def test_get_device_statistics(make_settings, make_client, respx_mock) -> None:
    stats = {
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
    }
    settings = make_settings()
    client = await make_client(settings)
    base = "http://gateway.test/proxy/network/integration/v1"
    respx_mock.get(f"{base}/sites/site-x/devices/dev-sw-1/statistics/latest").mock(
        return_value=httpx.Response(200, json=stats)
    )
    server = MCPServer(name="t")
    register_device_tools(server, client, settings)
    result = await server.call_tool(
        "get_device_statistics", {"device_id": "dev-sw-1", "site_id": "site-x"}
    )
    payload = result.structured_content
    assert payload["uptimeSec"] == 1124321
    assert payload["cpuUtilizationPct"] == 11.6
    assert payload["uplink"] == {"txRateBps": 48, "rxRateBps": 24}


async def test_get_device_statistics_unknown_returns_not_found(
    make_settings, make_client, respx_mock
) -> None:
    settings = make_settings()
    client = await make_client(settings)
    base = "http://gateway.test/proxy/network/integration/v1"
    respx_mock.get(f"{base}/sites/site-x/devices/dev-missing/statistics/latest").mock(
        return_value=httpx.Response(404, json={"code": "not-found", "message": "nope"})
    )
    server = MCPServer(name="t")
    register_device_tools(server, client, settings)
    result = await server.call_tool(
        "get_device_statistics", {"device_id": "dev-missing", "site_id": "site-x"}
    )
    payload = result.structured_content
    assert payload["error"] == "not_found"
    assert payload["resource"] == "device"


async def test_list_pending_devices_is_top_level(make_settings, make_client, respx_mock) -> None:
    settings = make_settings()
    client = await make_client(settings)
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
                        "adoptionTargetSiteIds": ["site-x"],
                    }
                ],
            },
        )

    respx_mock.get(f"{base}/pending-devices").mock(side_effect=_pending)
    server = MCPServer(name="t")
    register_device_tools(server, client, settings)
    result = await server.call_tool("list_pending_devices", {"limit": 10000, "offset": 2})
    payload = result.structured_content
    assert payload["count"] == 1
    assert payload["total_count"] == 1
    item = payload["items"][0]
    assert item["macAddress"] == "94:2a:6f:26:c6:ca"
    assert item["state"] == "PENDING_ADOPTION"
    # top-level endpoint: no /sites/{site} path segment
    assert requests[-1].url.path == "/proxy/network/integration/v1/pending-devices"
    params = dict(requests[-1].url.params)
    assert params["limit"] == "200"
    assert params["offset"] == "2"
