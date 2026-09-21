"""Unit tests for unifi_mcp.tools.firewall (design §10.7).

The default gateway state in the tests is the zone-based firewall NOT
configured, so the API answers HTTP 400 with api_code
``api.firewall.zone-based-firewall-not-configured``. The shared error mapping
must surface that as a structured ``unsupported`` result (a gateway
configuration state, not a request error — design §3/§40). Configured
gateways are covered by the success cases below and the E2E suite.
"""

from __future__ import annotations

import httpx
import pytest
from mcp_types import ToolAnnotations

from unifi_mcp.tools.firewall import build_policy_filter, register_firewall_tools

BASE = "http://gateway.test/proxy/network/integration/v1"
SITE = "site-test"

NOT_CONFIGURED_BODY = {
    "code": "api.firewall.zone-based-firewall-not-configured",
    "message": "Zone Based Firewall is not configured",
}

ZONE_A = "0a2bd089-61df-49ad-a33c-633879146ade"
ZONE_B = "11b9482d-fbcd-4b08-a92f-ec520447c7ac"


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
    assert list(server.tools) == [
        "list_firewall_zones",
        "get_firewall_zone",
        "list_firewall_policies",
        "get_firewall_policy",
        "get_firewall_policy_ordering",
    ]
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
    params = inspect.signature(server.tools["list_firewall_policies"]["fn"]).parameters
    assert set(params) == {
        "site_id",
        "name",
        "origin",
        "source_zone_id",
        "destination_zone_id",
        "limit",
        "offset",
    }
    params = inspect.signature(server.tools["get_firewall_policy"]["fn"]).parameters
    assert set(params) == {"policy_id", "site_id"}
    params = inspect.signature(server.tools["get_firewall_policy_ordering"]["fn"]).parameters
    assert set(params) == {"source_zone_id", "destination_zone_id", "site_id"}


# --- policy filter builder ----------------------------------------------------


@pytest.mark.parametrize(
    ("kwargs", "expected"),
    [
        ({}, None),
        ({"name": "nas"}, "name.like('*nas*')"),
        ({"name": "  drop "}, "name.like('*drop*')"),
        ({"name": "it's"}, "name.like('*its*')"),
        ({"name": "''"}, None),
        ({"origin": "user"}, "metadata.origin.eq('USER_DEFINED')"),
        ({"origin": "user_defined"}, "metadata.origin.eq('USER_DEFINED')"),
        ({"origin": "system"}, "metadata.origin.eq('SYSTEM_DEFINED')"),
        ({"origin": "SYSTEM_DEFINED"}, "metadata.origin.eq('SYSTEM_DEFINED')"),
        ({"source_zone_id": ZONE_A}, f"source.zoneId.eq({ZONE_A})"),
        ({"destination_zone_id": ZONE_B}, f"destination.zoneId.eq({ZONE_B})"),
        (
            {"name": "nas", "origin": "user", "source_zone_id": ZONE_A},
            f"and(name.like('*nas*'),metadata.origin.eq('USER_DEFINED'),source.zoneId.eq({ZONE_A}))",
        ),
    ],
)
def test_build_policy_filter(kwargs: dict, expected: str | None) -> None:
    assert build_policy_filter(**kwargs) == expected


@pytest.mark.parametrize(
    ("kwargs", "field"),
    [
        ({"origin": "custom"}, "origin"),
        ({"source_zone_id": "not-a-uuid"}, "source_zone_id"),
        ({"destination_zone_id": "'quoted-uuid'"}, "destination_zone_id"),
    ],
)
def test_build_policy_filter_invalid_values(kwargs: dict, field: str) -> None:
    from unifi_mcp.tools.errors import InvalidValueError

    with pytest.raises(InvalidValueError) as excinfo:
        build_policy_filter(**kwargs)
    assert excinfo.value.fields["field"] == field


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


async def test_list_firewall_policies_not_configured_maps_to_unsupported(
    respx_mock, make_client, make_settings
) -> None:
    server = await _tools(make_client, make_settings)
    respx_mock.get(f"{BASE}/sites/{SITE}/firewall/policies").mock(
        return_value=httpx.Response(400, json=NOT_CONFIGURED_BODY)
    )
    result = await server.tools["list_firewall_policies"]["fn"]()
    assert result["error"] == "unsupported"
    assert "not-configured" in result["api_code"]


async def test_get_firewall_policy_not_configured_maps_to_unsupported(
    respx_mock, make_client, make_settings
) -> None:
    server = await _tools(make_client, make_settings)
    respx_mock.get(f"{BASE}/sites/{SITE}/firewall/policies/policy-x").mock(
        return_value=httpx.Response(400, json=NOT_CONFIGURED_BODY)
    )
    result = await server.tools["get_firewall_policy"]["fn"](policy_id="policy-x")
    assert result["error"] == "unsupported"
    assert "not-configured" in result["api_code"]


async def test_get_firewall_policy_ordering_not_configured_maps_to_unsupported(
    respx_mock, make_client, make_settings
) -> None:
    server = await _tools(make_client, make_settings)
    respx_mock.get(f"{BASE}/sites/{SITE}/firewall/policies/ordering").mock(
        return_value=httpx.Response(400, json=NOT_CONFIGURED_BODY)
    )
    result = await server.tools["get_firewall_policy_ordering"]["fn"](
        source_zone_id=ZONE_A, destination_zone_id=ZONE_B
    )
    assert result["error"] == "unsupported"
    assert "not-configured" in result["api_code"]


# --- zones: configured gateway --------------------------------------------------


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


# --- policies: configured gateway ----------------------------------------------


async def test_list_firewall_policies_success(
    respx_mock, make_client, make_settings, load_fixture
) -> None:
    server = await _tools(make_client, make_settings)
    respx_mock.get(f"{BASE}/sites/{SITE}/firewall/policies").mock(
        return_value=httpx.Response(200, json=load_fixture("firewall_policies.json"))
    )
    result = await server.tools["list_firewall_policies"]["fn"]()
    assert result["count"] == 4
    assert result["total_count"] == 4
    assert result["next_offset"] is None
    items = result["items"]
    # summary normalization strips internal fields from every item
    for item in items:
        assert "metadata" not in item
    # system-derived policies lack 'id' and must pass through untouched
    assert "id" not in items[0]
    assert items[0]["name"] == "000-0-ALLOW-ESTABLISHED-RELATED"
    assert items[0]["action"]["type"] == "ALLOW"
    assert items[0]["source"]["zoneId"] == ZONE_A
    # deep traffic filters are pruned at the summary level
    assert items[1]["destination"]["trafficFilter"] == "…"
    assert items[1]["id"] == "5bf82e53-61ca-4053-85b6-d90fd3f38dab"


async def test_list_firewall_policies_forwards_filters_and_pagination(
    respx_mock, make_client, make_settings
) -> None:
    server = await _tools(make_client, make_settings)
    respx_mock.get(f"{BASE}/sites/{SITE}/firewall/policies").mock(
        return_value=httpx.Response(400, json=NOT_CONFIGURED_BODY)
    )
    await server.tools["list_firewall_policies"]["fn"](
        name="nas", origin="user", source_zone_id=ZONE_A, limit=7, offset=2
    )
    params = dict(respx_mock.calls.last.request.url.params)
    assert params["limit"] == "7"
    assert params["offset"] == "2"
    assert params["filter"] == (
        f"and(name.like('*nas*'),metadata.origin.eq('USER_DEFINED'),source.zoneId.eq({ZONE_A}))"
    )


async def test_list_firewall_policies_invalid_origin(
    respx_mock, make_client, make_settings
) -> None:
    server = await _tools(make_client, make_settings)
    result = await server.tools["list_firewall_policies"]["fn"](origin="custom")
    assert result["error"] == "validation"
    assert result["field"] == "origin"


async def test_list_firewall_policies_invalid_zone_id(
    respx_mock, make_client, make_settings
) -> None:
    server = await _tools(make_client, make_settings)
    result = await server.tools["list_firewall_policies"]["fn"](destination_zone_id="not-a-uuid")
    assert result["error"] == "validation"
    assert result["field"] == "destination_zone_id"


async def test_get_firewall_policy_success(
    respx_mock, make_client, make_settings, load_fixture
) -> None:
    server = await _tools(make_client, make_settings)
    policy = load_fixture("firewall_policies.json")["data"][1]
    respx_mock.get(f"{BASE}/sites/{SITE}/firewall/policies/{policy['id']}").mock(
        return_value=httpx.Response(200, json=policy)
    )
    result = await server.tools["get_firewall_policy"]["fn"](policy_id=policy["id"])
    assert result["id"] == policy["id"]
    assert result["name"] == "040-9-CLIENTS-DROP-ANY"
    # detail level keeps the full traffic filters, strips internal fields
    assert "metadata" not in result
    assert result["destination"]["trafficFilter"]["ipAddressFilter"]["items"][0]["value"] == (
        "172.26.40.0/24"
    )


async def test_get_firewall_policy_not_found(respx_mock, make_client, make_settings) -> None:
    server = await _tools(make_client, make_settings)
    respx_mock.get(f"{BASE}/sites/{SITE}/firewall/policies/unknown").mock(
        return_value=httpx.Response(404, json={"code": "not-found", "message": "Policy not found."})
    )
    result = await server.tools["get_firewall_policy"]["fn"](policy_id="unknown")
    assert result["error"] == "not_found"
    assert result["resource"] == "firewall_policy"
    assert result["query"] == "unknown"


async def test_get_firewall_policy_ordering_success(respx_mock, make_client, make_settings) -> None:
    server = await _tools(make_client, make_settings)
    respx_mock.get(f"{BASE}/sites/{SITE}/firewall/policies/ordering").mock(
        return_value=httpx.Response(
            200,
            json={
                "orderedFirewallPolicyIds": {
                    "beforeSystemDefined": ["11111111-1111-4111-8111-111111111111"],
                    "afterSystemDefined": [
                        "22222222-2222-4222-8222-222222222222",
                        "33333333-3333-4333-8333-333333333333",
                    ],
                }
            },
        )
    )
    result = await server.tools["get_firewall_policy_ordering"]["fn"](
        source_zone_id=ZONE_A, destination_zone_id=ZONE_B
    )
    assert result == {
        "source_zone_id": ZONE_A,
        "destination_zone_id": ZONE_B,
        "before_system_defined": ["11111111-1111-4111-8111-111111111111"],
        "after_system_defined": [
            "22222222-2222-4222-8222-222222222222",
            "33333333-3333-4333-8333-333333333333",
        ],
    }
    params = dict(respx_mock.calls.last.request.url.params)
    assert params["sourceFirewallZoneId"] == ZONE_A
    assert params["destinationFirewallZoneId"] == ZONE_B


async def test_get_firewall_policy_ordering_invalid_zone_id(
    respx_mock, make_client, make_settings
) -> None:
    server = await _tools(make_client, make_settings)
    result = await server.tools["get_firewall_policy_ordering"]["fn"](
        source_zone_id="nope", destination_zone_id=ZONE_B
    )
    assert result["error"] == "validation"
    assert result["field"] == "source_zone_id"
