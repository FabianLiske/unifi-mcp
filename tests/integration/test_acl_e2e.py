"""E2E tests for the WP-10 tools (ACL / traffic / reference).

MCP client -> ASGI app (local tool groups) -> respx-mocked UniFi API, over
the real ``/mcp`` Streamable HTTP surface. Own routes registered in the test
body win over ``fake_unifi``'s EMPTY_PAGE stubs (last-registered route wins).
"""

from __future__ import annotations

import re

import httpx
import pytest

from tests.conftest import mcp_client_session
from unifi_mcp.tools.acl import register_acl_tools
from unifi_mcp.tools.reference import register_reference_tools
from unifi_mcp.tools.registry import TOOL_GROUPS, ToolGroup, always_on
from unifi_mcp.tools.traffic import register_traffic_tools

ACL_GROUP = ToolGroup(name="acl", gate=always_on, register=register_acl_tools)
TRAFFIC_GROUP = ToolGroup(name="traffic", gate=always_on, register=register_traffic_tools)
REFERENCE_GROUP = ToolGroup(name="reference", gate=always_on, register=register_reference_tools)

GROUPS = TOOL_GROUPS + (ACL_GROUP, TRAFFIC_GROUP, REFERENCE_GROUP)

WP10_TOOLS = {
    "list_acl_rules",
    "get_acl_rule",
    "list_traffic_matching_lists",
    "get_traffic_matching_list",
    "list_reference_resources",
}

BASE = "http://gateway.test/proxy/network/integration/v1"
SITE = "site-default"

ACL_DETAIL = {
    "type": "IPV4",
    "id": "acl-rule-allow",
    "enabled": True,
    "name": "040-0-TRUSTED-ALLOW-K8S",
    "action": "ALLOW",
    "index": 0,
    "sourceFilter": {
        "type": "IP_ADDRESSES_OR_SUBNETS",
        "ipAddressesOrSubnets": ["172.26.40.104", "172.26.40.105"],
    },
    "destinationFilter": {"type": "NETWORKS", "networkIds": ["net-k8s"]},
    "metadata": {"origin": "USER_DEFINED"},
}


def _apply_filter(data: list[dict], filt: str) -> list[dict]:
    """Tiny evaluator for the filter forms list_acl_rules can produce."""
    out = list(data)
    match = re.search(r"action\.eq\('(\w+)'\)", filt)
    if match:
        out = [rule for rule in out if rule["action"] == match.group(1)]
    match = re.search(r"enabled\.eq\((true|false)\)", filt)
    if match:
        out = [rule for rule in out if rule["enabled"] is (match.group(1) == "true")]
    match = re.search(r"sourceFilter\.type\.eq\('(\w+)'\)", filt)
    if match:
        out = [rule for rule in out if rule["sourceFilter"]["type"] == match.group(1)]
    match = re.search(r"destinationFilter\.type\.eq\('(\w+)'\)", filt)
    if match:
        out = [rule for rule in out if rule["destinationFilter"]["type"] == match.group(1)]
    return out


def _acl_rules_route(page: dict, requests: list[httpx.Request]) -> None:
    def _rules(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        data = _apply_filter(page["data"], request.url.params.get("filter", ""))
        envelope = dict(page)
        envelope["count"] = len(data)
        envelope["totalCount"] = len(data)
        envelope["data"] = data
        return httpx.Response(200, json=envelope)

    return _rules


async def test_tools_list_contains_wp10_tools(mcp_app_factory) -> None:
    async with mcp_app_factory(groups=GROUPS) as (app, settings):
        token = settings.mcp_auth_token.get_secret_value()
        async with mcp_client_session(app, token) as session:
            tools = await session.list_tools()
    names = {tool.name for tool in tools.tools}
    assert names >= WP10_TOOLS


async def test_list_acl_rules_action_filter(
    mcp_app_factory, fake_unifi, respx_mock, load_fixture
) -> None:
    requests: list[httpx.Request] = []
    respx_mock.get(f"{BASE}/sites/{SITE}/acl-rules").mock(
        side_effect=_acl_rules_route(load_fixture("acl_rules.json"), requests)
    )
    async with mcp_app_factory(groups=GROUPS) as (app, settings):
        token = settings.mcp_auth_token.get_secret_value()
        async with mcp_client_session(app, token) as session:
            result = await session.call_tool(
                "list_acl_rules", {"action": "allow", "limit": 5, "offset": 1}
            )
    assert result.is_error is not True
    params = dict(requests[-1].url.params)
    assert params["filter"] == "action.eq('ALLOW')"
    assert params["limit"] == "5"
    assert params["offset"] == "1"
    payload = result.structured_content
    assert payload["count"] == 2
    assert payload["total_count"] == 2
    assert payload["next_offset"] is None
    assert {item["id"] for item in payload["items"]} == {"acl-rule-allow", "acl-rule-ports"}
    assert all(item["action"] == "ALLOW" for item in payload["items"])


async def test_list_acl_rules_combined_filters(
    mcp_app_factory, fake_unifi, respx_mock, load_fixture
) -> None:
    requests: list[httpx.Request] = []
    respx_mock.get(f"{BASE}/sites/{SITE}/acl-rules").mock(
        side_effect=_acl_rules_route(load_fixture("acl_rules.json"), requests)
    )
    async with mcp_app_factory(groups=GROUPS) as (app, settings):
        token = settings.mcp_auth_token.get_secret_value()
        async with mcp_client_session(app, token) as session:
            result = await session.call_tool(
                "list_acl_rules",
                {"action": "deny", "enabled": False, "source": "networks"},
            )
    assert result.is_error is not True
    params = dict(requests[-1].url.params)
    assert params["filter"] == (
        "and(enabled.eq(false),sourceFilter.type.eq('NETWORKS'),action.eq('BLOCK'))"
    )
    payload = result.structured_content
    assert [item["id"] for item in payload["items"]] == ["acl-rule-block"]
    assert payload["count"] == 1


async def test_get_acl_rule(mcp_app_factory, fake_unifi, respx_mock) -> None:
    respx_mock.get(f"{BASE}/sites/{SITE}/acl-rules/acl-rule-allow").mock(
        return_value=httpx.Response(200, json=ACL_DETAIL)
    )
    async with mcp_app_factory(groups=GROUPS) as (app, settings):
        token = settings.mcp_auth_token.get_secret_value()
        async with mcp_client_session(app, token) as session:
            result = await session.call_tool("get_acl_rule", {"rule_id": "acl-rule-allow"})
    assert result.is_error is not True
    payload = result.structured_content
    assert payload["id"] == "acl-rule-allow"
    assert payload["action"] == "ALLOW"
    assert payload["enabled"] is True
    assert payload["sourceFilter"] == {
        "type": "IP_ADDRESSES_OR_SUBNETS",
        "ipAddressesOrSubnets": ["172.26.40.104", "172.26.40.105"],
    }
    assert payload["destinationFilter"] == {"type": "NETWORKS", "networkIds": ["net-k8s"]}
    # detail normalization strips internal fields
    assert "metadata" not in payload


async def test_get_acl_rule_unknown_returns_not_found(
    mcp_app_factory, fake_unifi, respx_mock
) -> None:
    respx_mock.get(f"{BASE}/sites/{SITE}/acl-rules/acl-rule-missing").mock(
        return_value=httpx.Response(
            404, json={"code": "not-found", "message": "ACL rule not found."}
        )
    )
    async with mcp_app_factory(groups=GROUPS) as (app, settings):
        token = settings.mcp_auth_token.get_secret_value()
        async with mcp_client_session(app, token) as session:
            result = await session.call_tool("get_acl_rule", {"rule_id": "acl-rule-missing"})
    payload = result.structured_content
    assert payload["error"] == "not_found"
    assert payload["resource"] == "acl_rule"
    assert payload["query"] == "acl-rule-missing"
    assert payload["message"]


async def test_list_and_get_traffic_matching_list(
    mcp_app_factory, fake_unifi, respx_mock, load_fixture
) -> None:
    respx_mock.get(f"{BASE}/sites/{SITE}/traffic-matching-lists").mock(
        return_value=httpx.Response(200, json=load_fixture("traffic_matching_lists.json"))
    )
    tml = load_fixture("traffic_matching_lists.json")["data"][1]
    respx_mock.get(f"{BASE}/sites/{SITE}/traffic-matching-lists/{tml['id']}").mock(
        return_value=httpx.Response(200, json=tml)
    )
    async with mcp_app_factory(groups=GROUPS) as (app, settings):
        token = settings.mcp_auth_token.get_secret_value()
        async with mcp_client_session(app, token) as session:
            listed = await session.call_tool("list_traffic_matching_lists", {})
            fetched = await session.call_tool("get_traffic_matching_list", {"tml_id": tml["id"]})
    assert listed.is_error is not True
    payload = listed.structured_content
    assert payload["count"] == 2
    assert payload["total_count"] == 2
    assert {item["id"] for item in payload["items"]} == {"tml-subnets", "tml-ports"}
    assert fetched.is_error is not True
    detail = fetched.structured_content
    assert detail["id"] == "tml-ports"
    assert detail["name"] == "DNS_PORTS"
    assert detail["items"] == [
        {"type": "PORT_NUMBER", "value": 53},
        {"type": "PORT_NUMBER", "value": 853},
    ]


async def test_list_reference_resources_countries(mcp_app_factory, fake_unifi, respx_mock) -> None:
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

    respx_mock.get(f"{BASE}/countries").mock(side_effect=_countries)
    async with mcp_app_factory(groups=GROUPS) as (app, settings):
        token = settings.mcp_auth_token.get_secret_value()
        async with mcp_client_session(app, token) as session:
            result = await session.call_tool(
                "list_reference_resources", {"resource_type": "countries", "limit": 2, "offset": 1}
            )
    assert result.is_error is not True
    params = dict(requests[-1].url.params)
    assert params["limit"] == "2"
    assert params["offset"] == "1"
    payload = result.structured_content
    assert payload["count"] == 3
    assert payload["total_count"] == 3
    assert [item["code"] for item in payload["items"]] == ["AD", "DE", "US"]


async def test_list_reference_resources_site_scoped_path(
    mcp_app_factory, fake_unifi, respx_mock
) -> None:
    requests: list[httpx.Request] = []

    def _radius(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(
            200,
            json={
                "offset": 0,
                "limit": 50,
                "count": 2,
                "totalCount": 2,
                "data": [
                    {"id": "radius-default", "name": "Default"},
                    {"id": "radius-uid", "name": "UID"},
                ],
            },
        )

    respx_mock.get(f"{BASE}/sites/{SITE}/radius/profiles").mock(side_effect=_radius)
    async with mcp_app_factory(groups=GROUPS) as (app, settings):
        token = settings.mcp_auth_token.get_secret_value()
        async with mcp_client_session(app, token) as session:
            result = await session.call_tool(
                "list_reference_resources", {"resource_type": "radius_profiles"}
            )
    assert result.is_error is not True
    # the resolved default site is used for the site-scoped path
    assert requests[-1].url.path == f"/proxy/network/integration/v1/sites/{SITE}/radius/profiles"
    payload = result.structured_content
    assert payload["count"] == 2
    assert {item["name"] for item in payload["items"]} == {"Default", "UID"}


@pytest.mark.parametrize("value", ["networks", "wlan", "country"])
async def test_list_reference_resources_invalid_type(mcp_app_factory, fake_unifi, value) -> None:
    async with mcp_app_factory(groups=GROUPS) as (app, settings):
        token = settings.mcp_auth_token.get_secret_value()
        async with mcp_client_session(app, token) as session:
            result = await session.call_tool("list_reference_resources", {"resource_type": value})
    payload = result.structured_content
    assert payload["error"] == "validation"
    assert payload["field"] == "resource_type"
    assert "countries" in payload["allowed"]
    assert "wan_interfaces" in payload["allowed"]
