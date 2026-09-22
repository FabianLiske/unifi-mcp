"""Unit tests for unifi_mcp.tools.acl (filter builder, registration, 404)."""

from __future__ import annotations

import httpx
import pytest
from mcp.server.mcpserver import MCPServer

from unifi_mcp.tools.acl import (
    ALLOWED_ACTIONS,
    ALLOWED_ENDPOINT_TYPES,
    build_acl_filter,
    clamp_params,
    register_acl_tools,
)
from unifi_mcp.tools.errors import InvalidValueError

# --- build_acl_filter -------------------------------------------------------


def test_no_filters_returns_none() -> None:
    assert build_acl_filter() is None
    assert build_acl_filter(action="", source="  ", destination="") is None


def test_single_clauses() -> None:
    assert build_acl_filter(action="allow") == "action.eq('ALLOW')"
    assert build_acl_filter(action="deny") == "action.eq('BLOCK')"
    assert build_acl_filter(enabled=True) == "enabled.eq(true)"
    assert build_acl_filter(enabled=False) == "enabled.eq(false)"
    assert build_acl_filter(source="networks") == "sourceFilter.type.eq('NETWORKS')"
    assert (
        build_acl_filter(destination="ip_addresses")
        == "destinationFilter.type.eq('IP_ADDRESSES_OR_SUBNETS')"
    )


def test_action_aliases() -> None:
    assert build_acl_filter(action="ALLOW") == "action.eq('ALLOW')"
    assert build_acl_filter(action="block") == "action.eq('BLOCK')"
    assert build_acl_filter(action=" Deny ") == "action.eq('BLOCK')"


def test_endpoint_type_aliases() -> None:
    assert build_acl_filter(source="network") == "sourceFilter.type.eq('NETWORKS')"
    assert build_acl_filter(source="subnets") == "sourceFilter.type.eq('IP_ADDRESSES_OR_SUBNETS')"
    assert build_acl_filter(destination="ports") == "destinationFilter.type.eq('PORTS')"
    assert (
        build_acl_filter(destination="mac_addresses")
        == "destinationFilter.type.eq('MAC_ADDRESSES')"
    )


def test_combined_filter_uses_and() -> None:
    expr = build_acl_filter(enabled=True, source="networks", destination="networks", action="deny")
    assert expr == (
        "and(enabled.eq(true),sourceFilter.type.eq('NETWORKS'),"
        "destinationFilter.type.eq('NETWORKS'),action.eq('BLOCK'))"
    )


@pytest.mark.parametrize("value", ["permit", "DROP", "deny_all"])
def test_invalid_action_rejected(value: str) -> None:
    with pytest.raises(InvalidValueError) as excinfo:
        build_acl_filter(action=value)
    assert excinfo.value.code == "validation"
    assert excinfo.value.fields["field"] == "action"
    assert list(ALLOWED_ACTIONS) == excinfo.value.fields["allowed"]


@pytest.mark.parametrize("value", ["vlan", "any", "clients"])
def test_invalid_endpoint_type_rejected(value: str) -> None:
    with pytest.raises(InvalidValueError) as excinfo:
        build_acl_filter(source=value)
    assert excinfo.value.code == "validation"
    assert excinfo.value.fields["field"] == "source"
    assert list(ALLOWED_ENDPOINT_TYPES) == excinfo.value.fields["allowed"]

    with pytest.raises(InvalidValueError) as excinfo:
        build_acl_filter(destination=value)
    assert excinfo.value.fields["field"] == "destination"


# --- clamp_params ---------------------------------------------------------------


def test_clamp_params_defaults_and_caps() -> None:
    assert clamp_params(None, None, max_limit=200) == {"limit": 50, "offset": 0}
    assert clamp_params(1000, None, max_limit=200) == {"limit": 200, "offset": 0}
    assert clamp_params(0, 7, max_limit=200) == {"limit": 50, "offset": 7}


def test_clamp_params_negative_offset_rejected() -> None:
    with pytest.raises(InvalidValueError):
        clamp_params(None, -1, max_limit=200)


# --- registration ----------------------------------------------------------------


async def test_register_acl_tools(make_settings) -> None:
    settings = make_settings()
    server = MCPServer(name="t")
    register_acl_tools(server, object(), settings)  # type: ignore[arg-type]
    tools = await server.list_tools()
    names = [tool.name for tool in tools]
    assert names == ["list_acl_rules", "get_acl_rule", "get_acl_rule_ordering"]
    for tool in tools:
        assert tool.annotations is not None and tool.annotations.read_only_hint is True
    params = {tool.name: set(tool.input_schema["properties"]) for tool in tools}
    assert params["list_acl_rules"] == {
        "site_id",
        "action",
        "enabled",
        "source",
        "destination",
        "limit",
        "offset",
    }
    assert params["get_acl_rule"] == {"rule_id", "site_id"}
    assert params["get_acl_rule_ordering"] == {"site_id"}
    assert "rule_id" in tools[1].input_schema.get("required", [])


# --- tool behavior (real client, respx-mocked API) ------------------------------


async def test_get_acl_rule_returns_normalized_detail(
    make_settings, make_client, respx_mock
) -> None:
    settings = make_settings()
    client = await make_client(settings)
    base = "http://gateway.test/proxy/network/integration/v1"
    respx_mock.get(f"{base}/sites/site-x/acl-rules/acl-rule-allow").mock(
        return_value=httpx.Response(
            200,
            json={
                "type": "IPV4",
                "id": "acl-rule-allow",
                "enabled": True,
                "name": "040-0-TRUSTED-ALLOW-K8S",
                "action": "ALLOW",
                "index": 0,
                "sourceFilter": {
                    "type": "NETWORKS",
                    "networkIds": ["net-k8s"],
                },
                "destinationFilter": {
                    "type": "IP_ADDRESSES_OR_SUBNETS",
                    "ipAddressesOrSubnets": ["172.26.40.104"],
                },
                "metadata": {"origin": "USER_DEFINED"},
            },
        )
    )
    server = MCPServer(name="t")
    register_acl_tools(server, client, settings)
    result = await server.call_tool(
        "get_acl_rule", {"rule_id": "acl-rule-allow", "site_id": "site-x"}
    )
    payload = result.structured_content
    assert payload["id"] == "acl-rule-allow"
    assert payload["action"] == "ALLOW"
    assert payload["sourceFilter"] == {"type": "NETWORKS", "networkIds": ["net-k8s"]}
    assert payload["destinationFilter"] == {
        "type": "IP_ADDRESSES_OR_SUBNETS",
        "ipAddressesOrSubnets": ["172.26.40.104"],
    }
    # detail level: internal fields (metadata) are stripped
    assert "metadata" not in payload


async def test_get_acl_rule_unknown_returns_not_found(
    make_settings, make_client, respx_mock
) -> None:
    settings = make_settings()
    client = await make_client(settings)
    base = "http://gateway.test/proxy/network/integration/v1"
    respx_mock.get(f"{base}/sites/site-x/acl-rules/acl-rule-missing").mock(
        return_value=httpx.Response(
            404, json={"code": "not-found", "message": "ACL rule not found."}
        )
    )
    server = MCPServer(name="t")
    register_acl_tools(server, client, settings)
    result = await server.call_tool(
        "get_acl_rule", {"rule_id": "acl-rule-missing", "site_id": "site-x"}
    )
    payload = result.structured_content
    assert payload["error"] == "not_found"
    assert payload["resource"] == "acl_rule"
    assert payload["query"] == "acl-rule-missing"


async def test_list_acl_rules_sends_clamped_params(make_settings, make_client, respx_mock) -> None:
    settings = make_settings()
    client = await make_client(settings)
    base = "http://gateway.test/proxy/network/integration/v1"
    requests: list[httpx.Request] = []

    def _rules(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(
            200, json={"offset": 0, "limit": 50, "count": 0, "totalCount": 0, "data": []}
        )

    respx_mock.get(f"{base}/sites/site-x/acl-rules").mock(side_effect=_rules)
    server = MCPServer(name="t")
    register_acl_tools(server, client, settings)
    result = await server.call_tool(
        "list_acl_rules",
        {"site_id": "site-x", "action": "allow", "enabled": True, "limit": 10000, "offset": 3},
    )
    payload = result.structured_content
    assert payload["count"] == 0 and payload["next_offset"] is None
    params = dict(requests[-1].url.params)
    assert params["filter"] == "and(enabled.eq(true),action.eq('ALLOW'))"
    assert params["limit"] == "200"
    assert params["offset"] == "3"


async def test_get_acl_rule_ordering_returns_snake_case_ids(
    make_settings, make_client, respx_mock
) -> None:
    settings = make_settings()
    client = await make_client(settings)
    base = "http://gateway.test/proxy/network/integration/v1"
    respx_mock.get(f"{base}/sites/site-x/acl-rules/ordering").mock(
        return_value=httpx.Response(
            200,
            json={
                "orderedAclRuleIds": [
                    "rule-1",
                    "rule-2",
                    "rule-3",
                ]
            },
        )
    )
    server = MCPServer(name="t")
    register_acl_tools(server, client, settings)
    result = await server.call_tool("get_acl_rule_ordering", {"site_id": "site-x"})
    payload = result.structured_content
    assert payload == {"ordered_rule_ids": ["rule-1", "rule-2", "rule-3"]}
