"""Unit tests for unifi_mcp.tools.networks (design §10.5)."""

from __future__ import annotations

import httpx
from mcp_types import ToolAnnotations

from unifi_mcp.tools.networks import register_network_tools
from unifi_mcp.unifi.normalization import ELLIPSIS

BASE = "http://gateway.test/proxy/network/integration/v1"
SITE = "site-test"


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
    register_network_tools(server, client, settings)
    return server


# --- registration -------------------------------------------------------------


async def test_register_network_tools(make_settings) -> None:
    settings = make_settings()
    server = _RecordingServer()
    register_network_tools(server, object(), settings)  # type: ignore[arg-type]
    assert list(server.tools) == ["list_networks", "get_network"]
    for entry in server.tools.values():
        assert entry["annotations"] is not None
        assert entry["annotations"].read_only_hint is True
        assert entry["title"]
        assert entry["description"]
        # §31: description states it is read-only
        assert "read-only" in entry["description"]


async def test_list_networks_schema_params(make_client, make_settings) -> None:
    import inspect

    server = await _tools(make_client, make_settings)
    params = inspect.signature(server.tools["list_networks"]["fn"]).parameters
    assert set(params) == {"site_id", "limit", "offset"}
    params = inspect.signature(server.tools["get_network"]["fn"]).parameters
    assert set(params) == {"network_id", "site_id"}


# --- list_networks ------------------------------------------------------------


async def test_list_networks_envelope_and_summary(
    respx_mock, make_client, make_settings, load_fixture
) -> None:
    server = await _tools(make_client, make_settings)
    respx_mock.get(f"{BASE}/sites/{SITE}/networks").mock(
        return_value=httpx.Response(200, json=load_fixture("networks.json"))
    )
    result = await server.tools["list_networks"]["fn"](limit=10, offset=5)
    assert result["count"] == 3
    assert result["total_count"] == 3
    assert result["next_offset"] is None
    first = result["items"][0]
    assert first["id"] == "net-lan"
    assert first["name"] == "LAN"
    assert first["vlanId"] == 1
    # internal field stripped by normalization
    assert "metadata" not in first
    # summary: the deep dhcpConfiguration container is pruned, shallow scalars kept
    assert first["ipv4Configuration"]["dhcpConfiguration"] == ELLIPSIS
    assert first["ipv4Configuration"]["hostIpAddress"] == "172.26.1.1"
    # pagination params were forwarded to the API
    params = dict(respx_mock.calls.last.request.url.params)
    assert params["limit"] == "10"
    assert params["offset"] == "5"


async def test_list_networks_default_pagination(
    respx_mock, make_client, make_settings, load_fixture
) -> None:
    server = await _tools(make_client, make_settings)
    respx_mock.get(f"{BASE}/sites/{SITE}/networks").mock(
        return_value=httpx.Response(200, json=load_fixture("networks.json"))
    )
    result = await server.tools["list_networks"]["fn"]()
    params = dict(respx_mock.calls.last.request.url.params)
    assert params["limit"] == "50"
    assert params["offset"] == "0"
    assert result["count"] == 3


async def test_list_networks_negative_offset_rejected(
    respx_mock, make_client, make_settings
) -> None:
    server = await _tools(make_client, make_settings)
    result = await server.tools["list_networks"]["fn"](offset=-1)
    assert result["error"] == "validation"
    assert result["field"] == "offset"
    assert len(respx_mock.calls) == 0


# --- get_network ------------------------------------------------------------------


async def test_get_network_detail(respx_mock, make_client, make_settings, load_fixture) -> None:
    server = await _tools(make_client, make_settings)
    fixture = load_fixture("networks.json")
    respx_mock.get(f"{BASE}/sites/{SITE}/networks/net-lan").mock(
        return_value=httpx.Response(200, json=fixture["data"][0])
    )
    result = await server.tools["get_network"]["fn"](network_id="net-lan")
    assert result["id"] == "net-lan"
    assert result["name"] == "LAN"
    assert result["vlanId"] == 1
    # detail: the full dhcpConfiguration is kept (not pruned)
    dhcp = result["ipv4Configuration"]["dhcpConfiguration"]
    assert dhcp["mode"] == "NAT"
    assert dhcp["ipAddressRange"] == {"start": "172.26.1.100", "stop": "172.26.1.199"}
    # internal metadata stripped
    assert "metadata" not in result


async def test_get_network_not_found(respx_mock, make_client, make_settings) -> None:
    server = await _tools(make_client, make_settings)
    respx_mock.get(f"{BASE}/sites/{SITE}/networks/nope").mock(
        return_value=httpx.Response(
            404, json={"code": "not-found", "message": "Network not found."}
        )
    )
    result = await server.tools["get_network"]["fn"](network_id="nope")
    assert result["error"] == "not_found"
    assert result["resource"] == "network"
    assert result["query"] == "nope"
    assert result["message"]
