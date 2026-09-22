"""E2E tests for the WP-13b DNS policy tools.

MCP client -> ASGI app (default tool groups) -> respx-mocked UniFi API, over
the real ``/mcp`` Streamable HTTP surface. Own routes registered in the test
body win over ``fake_unifi``'s EMPTY_PAGE stubs (last-registered route wins).
"""

from __future__ import annotations

import re

import httpx

DNS_TOOLS = {"list_dns_policies", "get_dns_policy"}

BASE = "http://gateway.test/proxy/network/integration/v1"
SITE = "site-default"


def _apply_filter(data: list[dict], filt: str) -> list[dict]:
    """Tiny evaluator for the filter forms list_dns_policies can produce."""
    out = list(data)
    match = re.search(r"type\.eq\('(\w+)'\)", filt)
    if match:
        out = [policy for policy in out if policy["type"] == match.group(1)]
    match = re.search(r"domain\.like\('\*([^*]*)\*'\)", filt)
    if match:
        out = [policy for policy in out if match.group(1).lower() in policy["domain"].lower()]
    match = re.search(r"enabled\.eq\((true|false)\)", filt)
    if match:
        out = [policy for policy in out if policy["enabled"] is (match.group(1) == "true")]
    return out


def _policies_route(page: dict, requests: list[httpx.Request]):
    def _policies(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        data = _apply_filter(page["data"], request.url.params.get("filter", ""))
        envelope = dict(page)
        envelope["count"] = len(data)
        envelope["totalCount"] = len(data)
        envelope["data"] = data
        return httpx.Response(200, json=envelope)

    return _policies


async def test_tools_list_contains_dns_tools(mcp_session) -> None:
    async with mcp_session() as session:
        tools = await session.list_tools()
    names = {tool.name for tool in tools.tools}
    assert names >= DNS_TOOLS


async def test_list_dns_policies_default(mcp_session, respx_mock, load_fixture):
    respx_mock.get(f"{BASE}/sites/{SITE}/dns/policies").mock(
        return_value=httpx.Response(200, json=load_fixture("dns_policies.json"))
    )
    async with mcp_session() as session:
        result = await session.call_tool("list_dns_policies", {})
    assert result.is_error is not True
    payload = result.structured_content
    assert payload["count"] == 3
    assert payload["total_count"] == 3
    assert payload["next_offset"] is None
    assert [item["id"] for item in payload["items"]] == [
        "dns-a-k8s",
        "dns-a-nas",
        "dns-fwd-example",
    ]
    # list level: internal fields (metadata) are stripped
    assert all("metadata" not in item for item in payload["items"])


async def test_list_dns_policies_combined_filters(mcp_session, respx_mock, load_fixture):
    requests: list[httpx.Request] = []
    respx_mock.get(f"{BASE}/sites/{SITE}/dns/policies").mock(
        side_effect=_policies_route(load_fixture("dns_policies.json"), requests)
    )
    async with mcp_session() as session:
        result = await session.call_tool(
            "list_dns_policies",
            {"record_type": "a", "domain": "local", "enabled": True, "limit": 10000},
        )
    assert result.is_error is not True
    params = dict(requests[-1].url.params)
    assert params["filter"] == ("and(type.eq('A_RECORD'),domain.like('*local*'),enabled.eq(true))")
    assert params["limit"] == "200"
    payload = result.structured_content
    assert [item["id"] for item in payload["items"]] == ["dns-a-k8s"]


async def test_get_dns_policy(mcp_session, respx_mock):
    respx_mock.get(f"{BASE}/sites/{SITE}/dns/policies/dns-a-k8s").mock(
        return_value=httpx.Response(
            200,
            json={
                "type": "A_RECORD",
                "id": "dns-a-k8s",
                "enabled": True,
                "metadata": {"origin": "USER_DEFINED"},
                "domain": "k8s.local",
                "ipv4Address": "172.26.40.1",
                "ttlSeconds": 300,
            },
        )
    )
    async with mcp_session() as session:
        result = await session.call_tool("get_dns_policy", {"policy_id": "dns-a-k8s"})
    assert result.is_error is not True
    payload = result.structured_content
    assert payload["id"] == "dns-a-k8s"
    assert payload["type"] == "A_RECORD"
    assert payload["domain"] == "k8s.local"
    assert payload["ipv4Address"] == "172.26.40.1"
    assert "metadata" not in payload


async def test_get_dns_policy_unknown_returns_not_found(mcp_session, respx_mock):
    respx_mock.get(f"{BASE}/sites/{SITE}/dns/policies/dns-missing").mock(
        return_value=httpx.Response(
            404, json={"code": "not-found", "message": "DNS policy not found."}
        )
    )
    async with mcp_session() as session:
        result = await session.call_tool("get_dns_policy", {"policy_id": "dns-missing"})
    payload = result.structured_content
    assert payload["error"] == "not_found"
    assert payload["resource"] == "dns_policy"
    assert payload["query"] == "dns-missing"
    assert payload["message"]


async def test_list_dns_policies_invalid_record_type(mcp_session) -> None:
    async with mcp_session() as session:
        result = await session.call_tool("list_dns_policies", {"record_type": "bogus"})
    payload = result.structured_content
    assert payload["error"] == "validation"
    assert payload["field"] == "record_type"
    assert "forward" in payload["allowed"]
    assert "a" in payload["allowed"]
