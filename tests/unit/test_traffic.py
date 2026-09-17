"""Unit tests for unifi_mcp.tools.traffic (registration, list/get behavior)."""

from __future__ import annotations

import httpx
import pytest
from mcp.server.mcpserver import MCPServer

from unifi_mcp.tools.errors import InvalidValueError
from unifi_mcp.tools.traffic import clamp_params, register_traffic_tools

# --- clamp_params ---------------------------------------------------------------


def test_clamp_params_defaults_and_caps() -> None:
    assert clamp_params(None, None, max_limit=200) == {"limit": 50, "offset": 0}
    assert clamp_params(1000, None, max_limit=200) == {"limit": 200, "offset": 0}
    assert clamp_params(0, 7, max_limit=200) == {"limit": 50, "offset": 7}


def test_clamp_params_negative_offset_rejected() -> None:
    with pytest.raises(InvalidValueError):
        clamp_params(None, -1, max_limit=200)


# --- registration ----------------------------------------------------------------


async def test_register_traffic_tools(make_settings) -> None:
    settings = make_settings()
    server = MCPServer(name="t")
    register_traffic_tools(server, object(), settings)  # type: ignore[arg-type]
    tools = await server.list_tools()
    names = [tool.name for tool in tools]
    assert names == ["list_traffic_matching_lists", "get_traffic_matching_list"]
    for tool in tools:
        assert tool.annotations is not None and tool.annotations.read_only_hint is True
    params = {tool.name: set(tool.input_schema["properties"]) for tool in tools}
    assert params["list_traffic_matching_lists"] == {"site_id", "limit", "offset"}
    assert params["get_traffic_matching_list"] == {"tml_id", "site_id"}
    assert "tml_id" in tools[1].input_schema.get("required", [])


# --- tool behavior (real client, respx-mocked API) ------------------------------


async def test_list_traffic_matching_lists_envelope(
    make_settings, make_client, respx_mock, load_fixture
) -> None:
    settings = make_settings()
    client = await make_client(settings)
    base = "http://gateway.test/proxy/network/integration/v1"
    requests: list[httpx.Request] = []

    def _tmls(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(200, json=load_fixture("traffic_matching_lists.json"))

    respx_mock.get(f"{base}/sites/site-x/traffic-matching-lists").mock(side_effect=_tmls)
    server = MCPServer(name="t")
    register_traffic_tools(server, client, settings)
    result = await server.call_tool(
        "list_traffic_matching_lists", {"site_id": "site-x", "limit": 5, "offset": 2}
    )
    payload = result.structured_content
    assert payload["count"] == 2
    assert payload["total_count"] == 2
    assert payload["next_offset"] is None
    first = payload["items"][0]
    assert first["id"] == "tml-subnets"
    assert first["name"] == "TRUSTED_SUBNETS"
    assert first["type"] == "IPV4_ADDRESSES"
    # summary level: entry objects are pruned, the entry count is kept
    assert first["items"] == ["…", "…"]
    assert "metadata" not in first
    params = dict(requests[-1].url.params)
    assert params["limit"] == "5"
    assert params["offset"] == "2"
    assert "filter" not in params


async def test_get_traffic_matching_list_returns_entries(
    make_settings, make_client, respx_mock
) -> None:
    settings = make_settings()
    client = await make_client(settings)
    base = "http://gateway.test/proxy/network/integration/v1"
    respx_mock.get(f"{base}/sites/site-x/traffic-matching-lists/tml-ports").mock(
        return_value=httpx.Response(
            200,
            json={
                "type": "PORTS",
                "id": "tml-ports",
                "name": "DNS_PORTS",
                "items": [
                    {"type": "PORT_NUMBER", "value": 53},
                    {"type": "PORT_NUMBER", "value": 853},
                ],
                "metadata": {"origin": "USER_DEFINED"},
            },
        )
    )
    server = MCPServer(name="t")
    register_traffic_tools(server, client, settings)
    result = await server.call_tool(
        "get_traffic_matching_list", {"tml_id": "tml-ports", "site_id": "site-x"}
    )
    payload = result.structured_content
    assert payload["id"] == "tml-ports"
    assert payload["type"] == "PORTS"
    assert payload["items"] == [
        {"type": "PORT_NUMBER", "value": 53},
        {"type": "PORT_NUMBER", "value": 853},
    ]
    assert "metadata" not in payload


async def test_get_traffic_matching_list_unknown_returns_not_found(
    make_settings, make_client, respx_mock
) -> None:
    settings = make_settings()
    client = await make_client(settings)
    base = "http://gateway.test/proxy/network/integration/v1"
    respx_mock.get(f"{base}/sites/site-x/traffic-matching-lists/tml-missing").mock(
        return_value=httpx.Response(
            404, json={"code": "not-found", "message": "Traffic matching list not found."}
        )
    )
    server = MCPServer(name="t")
    register_traffic_tools(server, client, settings)
    result = await server.call_tool(
        "get_traffic_matching_list", {"tml_id": "tml-missing", "site_id": "site-x"}
    )
    payload = result.structured_content
    assert payload["error"] == "not_found"
    assert payload["resource"] == "traffic_matching_list"
    assert payload["query"] == "tml-missing"
