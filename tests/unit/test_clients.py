"""Unit tests for unifi_mcp.tools.clients (filter builder, resolution, registration)."""

from __future__ import annotations

import ipaddress

import httpx
import pytest
from mcp.server.mcpserver import MCPServer

from unifi_mcp.tools.clients import (
    build_client_filter,
    build_client_path_observations,
    exactly_one_identifier,
    filter_client_items,
    list_clients_data,
    register_client_tools,
    resolve_client,
)
from unifi_mcp.tools.errors import AmbiguousMatchError, InvalidValueError, NotFoundError

BASE = "http://gateway.test/proxy/network/integration/v1"
SITE = "site-default"


def _client(client_id: str = "client-1", **overrides: object) -> dict[str, object]:
    item: dict[str, object] = {
        "id": client_id,
        "name": "wk-5",
        "type": "WIRELESS",
        "ipAddress": "172.26.40.50",
        "macAddress": "00:11:22:33:44:55",
        "uplinkDeviceId": "dev-ap-1",
        "connectedAt": "2026-09-17T06:12:00Z",
        "access": {"type": "DEFAULT"},
    }
    for key, value in overrides.items():
        if value is None:
            item.pop(key, None)
        else:
            item[key] = value
    return item


def _page(
    items: list[dict[str, object]], *, total: int | None = None, limit: int = 2
) -> dict[str, object]:
    return {
        "offset": 0,
        "limit": limit,
        "count": len(items),
        "totalCount": total if total is not None else len(items),
        "data": items,
    }


# --- build_client_filter ---------------------------------------------------------


def test_build_client_filter_no_search_returns_none() -> None:
    assert build_client_filter() is None
    assert build_client_filter(search=None) is None
    assert build_client_filter(search="") is None
    assert build_client_filter(search="   ") is None
    assert build_client_filter(search="''") is None


def test_build_client_filter_exact_ip_becomes_eq() -> None:
    assert build_client_filter(search="172.26.40.50") == "ipAddress.eq('172.26.40.50')"
    assert build_client_filter(search="  10.0.0.1  ") == "ipAddress.eq('10.0.0.1')"
    assert build_client_filter(search="172.26.40.50'") == "ipAddress.eq('172.26.40.50')"


def test_build_client_filter_exact_mac_becomes_eq() -> None:
    assert build_client_filter(search="AA:BB:CC:DD:EE:FF") == "macAddress.eq('aa:bb:cc:dd:ee:ff')"
    assert build_client_filter(search="aa:bb:cc:dd:ee:ff") == "macAddress.eq('aa:bb:cc:dd:ee:ff')"


def test_build_client_filter_substring_search_not_server_expressible() -> None:
    # name is not a filter property (live-verified WP-8): substring terms
    # return None and are applied client-side.
    assert build_client_filter(search="wk-5") is None
    assert build_client_filter(search="172.26.40") is None
    assert build_client_filter(search="1.2.3.4.5") is None
    assert build_client_filter(search="aa:bb:cc:dd:ee:gg") is None
    assert build_client_filter(search="172.26.40:50") is None


# --- filter_client_items ---------------------------------------------------------


def test_filter_client_items_no_filters_keeps_dicts() -> None:
    items: list[object] = [_client("a"), "junk", 42, _client("b")]
    out = filter_client_items(items)
    assert [item["id"] for item in out] == ["a", "b"]


def test_filter_client_items_connected_to_device() -> None:
    items = [_client("a", uplinkDeviceId="dev-ap-1"), _client("b", uplinkDeviceId="dev-sw-1")]
    out = filter_client_items(items, connected_to_device_id="dev-sw-1")
    assert [item["id"] for item in out] == ["b"]
    # whitespace is stripped, case is exact; empty means "no filter"
    assert filter_client_items(items, connected_to_device_id="  dev-sw-1 ")
    assert not filter_client_items(items, connected_to_device_id="dev-none")
    assert filter_client_items(items, connected_to_device_id="")


def test_filter_client_items_search_substring_case_insensitive() -> None:
    items = [
        _client("a", name="wk-5", ipAddress="172.26.40.50", macAddress="00:11:22:33:44:55"),
        _client("b", name="PiVPN", ipAddress="172.26.70.33", macAddress="DC:A6:32:57:4E:3E"),
    ]
    assert [item["id"] for item in filter_client_items(items, search="WK")] == ["a"]
    assert [item["id"] for item in filter_client_items(items, search="172.26.70")] == ["b"]
    assert [item["id"] for item in filter_client_items(items, search="dc:a6:32")] == ["b"]
    assert filter_client_items(items, search="no-such-client") == []
    # empty term means "no filter"
    assert len(filter_client_items(items, search="  ")) == 2
    # items without an ipAddress/mac are only matched by name
    lonely = _client("c", ipAddress=None, macAddress=None, name="printer")
    assert [item["id"] for item in filter_client_items([lonely], search="printer")] == ["c"]
    assert filter_client_items([lonely], search="172") == []


def test_filter_client_items_subnet() -> None:
    subnet = ipaddress.ip_network("172.26.40.0/24")
    items = [
        _client("in", ipAddress="172.26.40.50"),
        _client("out", ipAddress="172.26.70.33"),
        _client("noip", ipAddress=None),
        _client("badip", ipAddress="not-an-ip"),
    ]
    out = filter_client_items(items, subnet=subnet)
    assert [item["id"] for item in out] == ["in"]


def test_filter_client_items_combined() -> None:
    items = [
        _client("a", uplinkDeviceId="dev-ap-1", ipAddress="172.26.40.50", name="wk-5"),
        _client("b", uplinkDeviceId="dev-ap-1", ipAddress="172.26.40.10", name="printer"),
        _client("c", uplinkDeviceId="dev-sw-1", ipAddress="172.26.40.51", name="wk-5"),
    ]
    out = filter_client_items(items, connected_to_device_id="dev-ap-1", search="172.26.40")
    assert [item["id"] for item in out] == ["a", "b"]


# --- exactly_one_identifier ------------------------------------------------------


@pytest.mark.parametrize(
    "kwargs",
    [
        {},
        {"client_id": "  "},
        {"client_id": "a", "mac": "b"},
        {"client_id": "a", "ip": "b", "hostname": "c"},
        {"mac": "a", "ip": "b"},
    ],
)
def test_exactly_one_identifier_rejects_wrong_count(kwargs: dict[str, str]) -> None:
    with pytest.raises(InvalidValueError) as excinfo:
        exactly_one_identifier(**kwargs)
    assert excinfo.value.code == "validation"
    assert excinfo.value.fields["field"] == "identifier"
    assert set(excinfo.value.fields["allowed"]) == {"client_id", "mac", "ip", "hostname"}


@pytest.mark.parametrize(
    "field",
    ["client_id", "mac", "ip", "hostname"],
)
def test_exactly_one_identifier_accepts_single(field: str) -> None:
    assert exactly_one_identifier(**{field: "x"}) == field


# --- resolve_client (respx) ------------------------------------------------------


async def test_resolve_client_not_found_by_id(make_client, respx_mock) -> None:
    respx_mock.get(f"{BASE}/sites/{SITE}/clients/client-missing").mock(
        return_value=httpx.Response(404, json={"code": "not-found", "message": "Client not found."})
    )
    client = await make_client()
    with pytest.raises(NotFoundError) as excinfo:
        await resolve_client(client, SITE, client_id="client-missing")
    assert excinfo.value.fields["resource"] == "client"
    assert excinfo.value.fields["query"] == "client-missing"


async def test_resolve_client_not_found_by_hostname(make_client, respx_mock) -> None:
    respx_mock.get(f"{BASE}/sites/{SITE}/clients").mock(
        return_value=httpx.Response(200, json=_page([], limit=200))
    )
    client = await make_client()
    with pytest.raises(NotFoundError) as excinfo:
        await resolve_client(client, SITE, hostname="wk-5")
    assert excinfo.value.fields["resource"] == "client"
    assert excinfo.value.fields["query"] == "hostname=wk-5"


async def test_resolve_client_ambiguous_hostname(make_client, respx_mock) -> None:
    respx_mock.get(f"{BASE}/sites/{SITE}/clients").mock(
        return_value=httpx.Response(
            200,
            json=_page(
                [
                    _client("client-printer-1", name="printer", ipAddress="172.26.40.10"),
                    _client("client-printer-2", name="printer", ipAddress="172.26.40.11"),
                ],
                limit=200,
            ),
        )
    )
    client = await make_client()
    with pytest.raises(AmbiguousMatchError) as excinfo:
        await resolve_client(client, SITE, hostname="printer")
    matches = excinfo.value.fields["matches"]
    assert len(matches) == 2
    assert {m["id"] for m in matches} == {"client-printer-1", "client-printer-2"}
    for match in matches:
        assert set(match) <= {"id", "name", "ipAddress", "macAddress"}


async def test_resolve_client_ambiguous_mac_by_total_count(make_client, respx_mock) -> None:
    # limit 2 page, but the gateway reports a third match
    respx_mock.get(f"{BASE}/sites/{SITE}/clients").mock(
        return_value=httpx.Response(
            200,
            json=_page(
                [
                    _client("client-a", macAddress="aa:bb:cc:00:00:01"),
                    _client("client-b", macAddress="aa:bb:cc:00:00:01"),
                ],
                total=3,
            ),
        )
    )
    client = await make_client()
    with pytest.raises(AmbiguousMatchError):
        await resolve_client(client, SITE, mac="aa:bb:cc:00:00:01")


async def test_resolve_client_by_mac_uses_gateway_filter(make_client, respx_mock) -> None:
    target = _client("client-wk-5", name="wk-5")
    seen: list[httpx.Request] = []

    def _list(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, json=_page([target]))

    respx_mock.get(f"{BASE}/sites/{SITE}/clients").mock(side_effect=_list)
    respx_mock.get(f"{BASE}/sites/{SITE}/clients/client-wk-5").mock(
        return_value=httpx.Response(200, json=target)
    )
    client = await make_client()
    detail = await resolve_client(client, SITE, mac="00:11:22:33:44:55")
    assert detail["id"] == "client-wk-5"
    params = dict(seen[0].url.params)
    assert params["filter"] == "macAddress.eq('00:11:22:33:44:55')"
    assert params["limit"] == "2"


async def test_resolve_client_by_ip_matches_case_insensitively_by_name(
    make_client, respx_mock
) -> None:
    seen: list[httpx.Request] = []

    def _list(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, json=_page([_client("client-wk-5", name="WK-5")], limit=200))

    respx_mock.get(f"{BASE}/sites/{SITE}/clients").mock(side_effect=_list)
    respx_mock.get(f"{BASE}/sites/{SITE}/clients/client-wk-5").mock(
        return_value=httpx.Response(200, json=_client("client-wk-5", name="WK-5"))
    )
    client = await make_client()
    detail = await resolve_client(client, SITE, hostname="wk-5")
    assert detail["id"] == "client-wk-5"
    # name is not a filter property: the tool scans the largest page
    assert dict(seen[0].url.params)["limit"] == "200"
    assert "filter" not in seen[0].url.params


@pytest.mark.parametrize(
    "kwargs",
    [
        {"mac": "nope"},
        {"mac": "aa:bb:cc:dd:ee:ff:00"},
        {"ip": "172.26.40"},
        {"ip": "abc"},
    ],
)
async def test_resolve_client_invalid_identifier_value(make_client, kwargs: dict[str, str]) -> None:
    client = await make_client()
    with pytest.raises(InvalidValueError) as excinfo:
        await resolve_client(client, SITE, **kwargs)
    assert excinfo.value.fields["field"] in {"mac", "ip"}


async def test_resolve_client_zero_or_two_identifiers_rejected(make_client) -> None:
    client = await make_client()
    with pytest.raises(InvalidValueError):
        await resolve_client(client, SITE)
    with pytest.raises(InvalidValueError):
        await resolve_client(client, SITE, mac="aa:bb:cc:dd:ee:ff", ip="172.26.40.50")


# --- list_clients_data ---------------------------------------------------------


async def test_list_clients_plain_path_preserves_pagination(make_client, respx_mock) -> None:
    respx_mock.get(f"{BASE}/sites/{SITE}/clients").mock(
        return_value=httpx.Response(200, json=_page([_client("a"), _client("b")], total=5, limit=2))
    )
    client = await make_client()
    payload = await list_clients_data(client, SITE, limit=2)
    assert payload["count"] == 2
    assert payload["total_count"] == 5
    assert payload["next_offset"] == 2
    assert [item["id"] for item in payload["items"]] == ["a", "b"]


async def test_list_clients_negative_offset_rejected(make_client) -> None:
    client = await make_client()
    with pytest.raises(InvalidValueError) as excinfo:
        await list_clients_data(client, SITE, offset=-1)
    assert excinfo.value.fields["field"] == "offset"


async def test_list_clients_unknown_network_not_found(make_client, respx_mock) -> None:
    respx_mock.get(f"{BASE}/sites/{SITE}/networks/net-missing").mock(
        return_value=httpx.Response(
            404, json={"code": "not-found", "message": "Network not found."}
        )
    )
    client = await make_client()
    with pytest.raises(NotFoundError) as excinfo:
        await list_clients_data(client, SITE, network_id="net-missing")
    assert excinfo.value.fields["resource"] == "network"
    assert excinfo.value.fields["query"] == "net-missing"


async def test_list_clients_network_filter_is_client_side(make_client, respx_mock) -> None:
    respx_mock.get(f"{BASE}/sites/{SITE}/networks/net-x").mock(
        return_value=httpx.Response(
            200,
            json={
                "id": "net-x",
                "name": "Clients",
                "ipv4Configuration": {"hostIpAddress": "172.26.40.1", "prefixLength": 24},
            },
        )
    )
    respx_mock.get(f"{BASE}/sites/{SITE}/clients").mock(
        return_value=httpx.Response(
            200,
            json=_page(
                [
                    _client("in-net", ipAddress="172.26.40.50"),
                    _client("out-net", ipAddress="172.26.70.33"),
                ],
                limit=200,
            ),
        )
    )
    client = await make_client()
    payload = await list_clients_data(client, SITE, network_id="net-x")
    assert [item["id"] for item in payload["items"]] == ["in-net"]
    assert payload["total_count"] == 1
    assert payload["next_offset"] is None


async def test_list_clients_network_and_exact_ip_search_combined(make_client, respx_mock) -> None:
    respx_mock.get(f"{BASE}/sites/{SITE}/networks/net-x").mock(
        return_value=httpx.Response(
            200,
            json={
                "id": "net-x",
                "name": "Clients",
                "ipv4Configuration": {"hostIpAddress": "172.26.40.1", "prefixLength": 24},
            },
        )
    )
    respx_mock.get(f"{BASE}/sites/{SITE}/clients").mock(
        return_value=httpx.Response(
            200,
            json=_page(
                [
                    _client("a", ipAddress="172.26.40.50"),
                    _client("b", ipAddress="172.26.40.60"),
                    _client("c", ipAddress="172.26.70.33"),
                ],
                limit=200,
            ),
        )
    )
    client = await make_client()
    payload = await list_clients_data(client, SITE, network_id="net-x", search="172.26.40.50")
    assert [item["id"] for item in payload["items"]] == ["a"]
    assert payload["total_count"] == 1


async def test_list_clients_network_without_ipv4_matches_nothing(make_client, respx_mock) -> None:
    respx_mock.get(f"{BASE}/sites/{SITE}/networks/net-empty").mock(
        return_value=httpx.Response(200, json={"id": "net-empty", "name": "VLAN-only"})
    )
    client = await make_client()
    payload = await list_clients_data(client, SITE, network_id="net-empty")
    assert payload == {"items": [], "count": 0, "total_count": 0, "next_offset": None}


# --- build_client_path_observations ---------------------------------------------


def test_observations_wireless_with_network_and_device() -> None:
    client_obj = _client(networkId="net-x", uplinkDeviceId="dev-ap-1")
    observations = build_client_path_observations(
        client_obj, network={"name": "Clients"}, attachment_device={"name": "AP One"}
    )
    assert observations == [
        "wireless",
        "ssid_not_reported_by_api",
        "client_connected",
        "network:Clients",
        "uplink_device:AP One",
    ]


def test_observations_wired_missing_parts() -> None:
    client_obj = _client(type="WIRED", networkId=None, uplinkDeviceId=None, connectedAt=None)
    observations = build_client_path_observations(client_obj, network=None, attachment_device=None)
    assert observations == [
        "wired",
        "port_not_reported_by_api",
        "no_network_assignment",
        "no_uplink_device_reported",
    ]


def test_observations_reported_but_unresolvable_parts() -> None:
    client_obj = _client(type="WIRED", networkId="net-x", uplinkDeviceId="dev-x")
    observations = build_client_path_observations(client_obj, network=None, attachment_device=None)
    assert "network_not_found" in observations
    assert "uplink_device_not_found" in observations


# --- registration ----------------------------------------------------------------


async def test_register_client_tools_registers_three_tools(make_settings) -> None:
    settings = make_settings()
    server = MCPServer(name="t")
    register_client_tools(server, object(), settings)  # type: ignore[arg-type]
    tools = await server.list_tools()
    assert [tool.name for tool in tools] == ["list_clients", "get_client", "inspect_client_path"]
    for tool in tools:
        assert tool.annotations is not None
        assert tool.annotations.read_only_hint is True
    props = {tool.name: set(tool.input_schema["properties"]) for tool in tools}
    assert props["list_clients"] == {
        "site_id",
        "network_id",
        "connected_to_device_id",
        "search",
        "limit",
        "offset",
    }
    assert props["get_client"] == {"client_id", "mac", "ip", "hostname", "site_id"}
    assert props["inspect_client_path"] == {"client_id", "mac", "ip", "hostname", "site_id"}
