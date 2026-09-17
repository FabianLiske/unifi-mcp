"""Unit tests for unifi_mcp.tools.reference (type->path mapping, validation)."""

from __future__ import annotations

import httpx
import pytest
from mcp.server.mcpserver import MCPServer

from unifi_mcp.tools.errors import InvalidValueError
from unifi_mcp.tools.reference import (
    ALLOWED_RESOURCE_TYPES,
    SITE_SCOPED_RESOURCE_TYPES,
    build_reference_path,
    clamp_params,
    lookup_resource,
    register_reference_tools,
)

# --- type -> path mapping --------------------------------------------------------

_EXPECTED_PATHS: dict[str, str] = {
    "countries": "/countries",
    "dpi_applications": "/dpi/applications",
    "dpi_categories": "/dpi/categories",
    "device_tags": "/sites/site-x/device-tags",
    "radius_profiles": "/sites/site-x/radius/profiles",
    "site_to_site_vpn_tunnels": "/sites/site-x/vpn/site-to-site-tunnels",
    "vpn_servers": "/sites/site-x/vpn/servers",
    "wan_interfaces": "/sites/site-x/wans",
}


@pytest.mark.parametrize("resource_type", sorted(_EXPECTED_PATHS))
def test_build_reference_path_site_scoped(resource_type: str) -> None:
    assert build_reference_path(resource_type, "site-x") == _EXPECTED_PATHS[resource_type]


@pytest.mark.parametrize("resource_type", ["countries", "dpi_applications", "dpi_categories"])
def test_build_reference_path_top_level_ignores_site(resource_type: str) -> None:
    assert build_reference_path(resource_type, "site-x") == build_reference_path(resource_type)
    assert build_reference_path(resource_type, None) == _EXPECTED_PATHS[resource_type]


@pytest.mark.parametrize(
    "resource_type",
    ["device_tags", "radius_profiles", "site_to_site_vpn_tunnels", "vpn_servers", "wan_interfaces"],
)
def test_site_scoped_types_require_site(resource_type: str) -> None:
    with pytest.raises(InvalidValueError):
        build_reference_path(resource_type, None)


def test_lookup_resource_reports_scoping() -> None:
    for resource_type in ALLOWED_RESOURCE_TYPES:
        template, site_scoped = lookup_resource(resource_type)
        assert site_scoped == (resource_type in SITE_SCOPED_RESOURCE_TYPES)
        assert ("{site}" in template) is site_scoped
    assert len(SITE_SCOPED_RESOURCE_TYPES) == 5


def test_all_eight_design_doc_types_present() -> None:
    assert set(ALLOWED_RESOURCE_TYPES) == {
        "wan_interfaces",
        "site_to_site_vpn_tunnels",
        "vpn_servers",
        "radius_profiles",
        "device_tags",
        "dpi_categories",
        "dpi_applications",
        "countries",
    }
    assert tuple(sorted(ALLOWED_RESOURCE_TYPES)) == ALLOWED_RESOURCE_TYPES


@pytest.mark.parametrize("value", ["networks", "wlan", "country", "dpi_application"])
def test_invalid_type_rejected(value: str) -> None:
    with pytest.raises(InvalidValueError) as excinfo:
        build_reference_path(value)
    assert excinfo.value.code == "validation"
    assert excinfo.value.fields["field"] == "resource_type"
    assert list(ALLOWED_RESOURCE_TYPES) == excinfo.value.fields["allowed"]


# --- clamp_params ---------------------------------------------------------------


def test_clamp_params_defaults_and_caps() -> None:
    assert clamp_params(None, None, max_limit=200) == {"limit": 50, "offset": 0}
    assert clamp_params(1000, None, max_limit=200) == {"limit": 200, "offset": 0}
    assert clamp_params(0, 7, max_limit=200) == {"limit": 50, "offset": 7}


def test_clamp_params_negative_offset_rejected() -> None:
    with pytest.raises(InvalidValueError):
        clamp_params(None, -1, max_limit=200)


# --- registration ----------------------------------------------------------------


async def test_register_reference_tools(make_settings) -> None:
    settings = make_settings()
    server = MCPServer(name="t")
    register_reference_tools(server, object(), settings)  # type: ignore[arg-type]
    tools = await server.list_tools()
    assert [tool.name for tool in tools] == ["list_reference_resources"]
    tool = tools[0]
    assert tool.annotations is not None and tool.annotations.read_only_hint is True
    params = tool.input_schema["properties"]
    assert set(params) == {"resource_type", "site_id", "limit", "offset"}
    assert tool.input_schema.get("required") == ["resource_type"]


# --- tool behavior (real client, respx-mocked API) ------------------------------


async def test_list_reference_resources_top_level_skips_site_resolution(
    make_settings, make_client, respx_mock
) -> None:
    """Top-level types must not call /sites at all (site_id is ignored)."""
    settings = make_settings()
    client = await make_client(settings)
    base = "http://gateway.test/proxy/network/integration/v1"
    requests: list[httpx.Request] = []

    def _countries(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(
            200,
            json={
                "offset": 0,
                "limit": 50,
                "count": 3,
                "totalCount": 3,
                "data": [
                    {"code": "AD", "name": "Andorra"},
                    {"code": "DE", "name": "Germany"},
                    {"code": "US", "name": "United States"},
                ],
            },
        )

    respx_mock.get(f"{base}/countries").mock(side_effect=_countries)
    server = MCPServer(name="t")
    register_reference_tools(server, client, settings)
    result = await server.call_tool(
        "list_reference_resources",
        {"resource_type": "countries", "site_id": "ignored-site", "limit": 2, "offset": 1},
    )
    payload = result.structured_content
    assert payload["count"] == 3
    assert payload["total_count"] == 3
    assert [item["code"] for item in payload["items"]] == ["AD", "DE", "US"]
    # exactly one API request, to the top-level path, with clamped pagination
    assert len(requests) == 1
    assert requests[0].url.path == "/proxy/network/integration/v1/countries"
    params = dict(requests[0].url.params)
    assert params["limit"] == "2"
    assert params["offset"] == "1"
