"""Unit tests for unifi_mcp.tools.firewall (design §10.7).

The gateway under test has the zone-based firewall NOT configured, so the API
answers HTTP 400 with api_code ``api.firewall.zone-based-firewall-not-configured``.
The shared error mapping must surface that as a structured ``unsupported``
result (a gateway configuration state, not a request error — design §3/§40).
"""

from __future__ import annotations

import httpx
from mcp_types import ToolAnnotations

from unifi_mcp.tools.firewall import register_firewall_tools

BASE = "http://gateway.test/proxy/network/integration/v1"
SITE = "site-test"

NOT_CONFIGURED_BODY = {
    "code": "api.firewall.zone-based-firewall-not-configured",
    "message": "Zone Based Firewall is not configured",
}


class _RecordingServer:
    """Captures the (wrapped) tool callables registered on the server."""

    def __init__(self) -> None:
        self.tools: dict[str, dict] = {}

    def add_tool(
        self,
        fn,
        name: str | None = None,
        title: str | None = None,
        description: str | None = None,
        annotations: ToolAnnotations | None = None,
        **kwargs: object,
    ) -> None:
        self.tools[name or fn.__name__] = {
            "fn": fn,
            "title": title,
            "description": description,
            "annotations": annotations,
        }


async def _tools(make_client, make_settings) -> _RecordingServer:
    settings = make_settings(unifi_site_id=SITE)
    client = await make_client(settings)
    server = _RecordingServer()
    register_firewall_tools(server, client, settings)
    return server


# --- registration -------------------------------------------------------------


async def test_register_firewall_tools(make_settings) -> None:
    settings = make_settings()
    server = _RecordingServer()
    register_firewall_tools(server, object(), settings)  # type: ignore[arg-type]
    assert list(server.tools) == ["list_firewall_zones", "get_firewall_zone"]
    for entry in server.tools.values():
        assert entry["annotations"] is not None
        assert entry["annotations"].read_only_hint is True
        assert entry["title"]
        assert "read-only" in entry["description"]


async def test_firewall_schema_params(make_client, make_settings) -> None:
    import inspect

    server = await _tools(make_client, make_settings)
    params = inspect.signature(server.tools["list_firewall_zones"]["fn"]).parameters
    assert set(params) == {"site_id", "limit", "offset"}
    params = inspect.signature(server.tools["get_firewall_zone"]["fn"]).parameters
    assert set(params) == {"zone_id", "site_id"}


# --- not-configured mapping ------------------------------------------------------


async def test_list_firewall_zones_not_configured_maps_to_unsupported(
    respx_mock, make_client, make_settings
) -> None:
    server = await _tools(make_client, make_settings)
    respx_mock.get(f"{BASE}/sites/{SITE}/firewall/zones").mock(
        return_value=httpx.Response(400, json=NOT_CONFIGURED_BODY)
    )
    result = await server.tools["list_firewall_zones"]["fn"]()
    assert result["error"] == "unsupported"
    assert result["api_code"] == "api.firewall.zone-based-firewall-not-configured"
    assert "not-configured" in result["api_code"]
    assert result["message"]


async def test_get_firewall_zone_not_configured_maps_to_unsupported(
    respx_mock, make_client, make_settings
) -> None:
    server = await _tools(make_client, make_settings)
    respx_mock.get(f"{BASE}/sites/{SITE}/firewall/zones/zone-x").mock(
        return_value=httpx.Response(400, json=NOT_CONFIGURED_BODY)
    )
    result = await server.tools["get_firewall_zone"]["fn"](zone_id="zone-x")
    assert result["error"] == "unsupported"
    assert "not-configured" in result["api_code"]


async def test_get_firewall_zone_not_found(respx_mock, make_client, make_settings) -> None:
    """A configured gateway that lacks the zone reports not_found, not 400."""
    server = await _tools(make_client, make_settings)
    respx_mock.get(f"{BASE}/sites/{SITE}/firewall/zones/zone-x").mock(
        return_value=httpx.Response(404, json={"code": "not-found", "message": "Zone not found."})
    )
    result = await server.tools["get_firewall_zone"]["fn"](zone_id="zone-x")
    assert result["error"] == "not_found"
    assert result["resource"] == "firewall_zone"
    assert result["query"] == "zone-x"


async def test_list_firewall_zones_forwards_pagination(
    respx_mock, make_client, make_settings
) -> None:
    server = await _tools(make_client, make_settings)
    respx_mock.get(f"{BASE}/sites/{SITE}/firewall/zones").mock(
        return_value=httpx.Response(400, json=NOT_CONFIGURED_BODY)
    )
    await server.tools["list_firewall_zones"]["fn"](limit=7, offset=2)
    params = dict(respx_mock.calls.last.request.url.params)
    assert params["limit"] == "7"
    assert params["offset"] == "2"
