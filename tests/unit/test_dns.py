"""Unit tests for unifi_mcp.tools.dns (filter builder, registration, 404)."""

from __future__ import annotations

import httpx
import pytest
from mcp.server.mcpserver import MCPServer

from unifi_mcp.tools.dns import (
    ALLOWED_RECORD_TYPES,
    build_dns_filter,
    register_dns_tools,
)
from unifi_mcp.tools.errors import InvalidValueError

# --- build_dns_filter -------------------------------------------------------


def test_no_filters_returns_none() -> None:
    assert build_dns_filter() is None
    assert build_dns_filter(record_type="", domain="  ") is None


def test_single_clauses() -> None:
    assert build_dns_filter(record_type="a") == "type.eq('A_RECORD')"
    assert build_dns_filter(domain="k8s") == "domain.like('*k8s*')"
    assert build_dns_filter(enabled=True) == "enabled.eq(true)"
    assert build_dns_filter(enabled=False) == "enabled.eq(false)"


def test_record_type_aliases() -> None:
    assert build_dns_filter(record_type="A") == "type.eq('A_RECORD')"
    assert build_dns_filter(record_type="a_record") == "type.eq('A_RECORD')"
    assert build_dns_filter(record_type="AAAA") == "type.eq('AAAA_RECORD')"
    assert build_dns_filter(record_type="cname") == "type.eq('CNAME_RECORD')"
    assert build_dns_filter(record_type="MX") == "type.eq('MX_RECORD')"
    assert build_dns_filter(record_type="txt") == "type.eq('TXT_RECORD')"
    assert build_dns_filter(record_type="srv") == "type.eq('SRV_RECORD')"
    assert build_dns_filter(record_type="forward") == "type.eq('FORWARD_DOMAIN')"
    assert build_dns_filter(record_type="FORWARD_DOMAIN") == "type.eq('FORWARD_DOMAIN')"


def test_domain_strips_quotes_and_surrounding_whitespace() -> None:
    assert build_dns_filter(domain="  k8s.local ") == "domain.like('*k8s.local*')"
    assert build_dns_filter(domain="o'brien") == "domain.like('*obrien*')"
    assert build_dns_filter(domain="''") is None


def test_combined_filter_uses_and() -> None:
    expr = build_dns_filter(record_type="a", domain="k8s", enabled=True)
    assert expr == "and(type.eq('A_RECORD'),domain.like('*k8s*'),enabled.eq(true))"


@pytest.mark.parametrize("value", ["ns", "any", "record", "a_record_type"])
def test_invalid_record_type_rejected(value: str) -> None:
    with pytest.raises(InvalidValueError) as excinfo:
        build_dns_filter(record_type=value)
    assert excinfo.value.code == "validation"
    assert excinfo.value.fields["field"] == "record_type"
    assert list(ALLOWED_RECORD_TYPES) == excinfo.value.fields["allowed"]


# --- registration ---------------------------------------------------------------


async def test_register_dns_tools(make_settings) -> None:
    settings = make_settings()
    server = MCPServer(name="t")
    register_dns_tools(server, object(), settings)  # type: ignore[arg-type]
    tools = await server.list_tools()
    names = [tool.name for tool in tools]
    assert names == ["list_dns_policies", "get_dns_policy"]
    for tool in tools:
        assert tool.annotations is not None and tool.annotations.read_only_hint is True
    params = {tool.name: set(tool.input_schema["properties"]) for tool in tools}
    assert params["list_dns_policies"] == {
        "site_id",
        "record_type",
        "domain",
        "enabled",
        "limit",
        "offset",
    }
    assert params["get_dns_policy"] == {"policy_id", "site_id"}
    assert "policy_id" in tools[1].input_schema.get("required", [])


# --- tool behavior (real client, respx-mocked API) ------------------------------


async def test_list_dns_policies_sends_filter_and_clamped_params(
    make_settings, make_client, respx_mock
) -> None:
    settings = make_settings()
    client = await make_client(settings)
    base = "http://gateway.test/proxy/network/integration/v1"
    requests: list[httpx.Request] = []

    def _policies(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(
            200, json={"offset": 0, "limit": 50, "count": 0, "totalCount": 0, "data": []}
        )

    respx_mock.get(f"{base}/sites/site-x/dns/policies").mock(side_effect=_policies)
    server = MCPServer(name="t")
    register_dns_tools(server, client, settings)
    result = await server.call_tool(
        "list_dns_policies",
        {
            "site_id": "site-x",
            "record_type": "a",
            "domain": "k8s",
            "enabled": True,
            "limit": 10000,
            "offset": 3,
        },
    )
    payload = result.structured_content
    assert payload["count"] == 0 and payload["next_offset"] is None
    params = dict(requests[-1].url.params)
    assert params["filter"] == "and(type.eq('A_RECORD'),domain.like('*k8s*'),enabled.eq(true))"
    assert params["limit"] == "200"
    assert params["offset"] == "3"


async def test_get_dns_policy_returns_normalized_detail(
    make_settings, make_client, respx_mock
) -> None:
    settings = make_settings()
    client = await make_client(settings)
    base = "http://gateway.test/proxy/network/integration/v1"
    respx_mock.get(f"{base}/sites/site-x/dns/policies/dns-a-k8s").mock(
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
    server = MCPServer(name="t")
    register_dns_tools(server, client, settings)
    result = await server.call_tool(
        "get_dns_policy", {"policy_id": "dns-a-k8s", "site_id": "site-x"}
    )
    payload = result.structured_content
    assert payload["id"] == "dns-a-k8s"
    assert payload["type"] == "A_RECORD"
    assert payload["domain"] == "k8s.local"
    assert payload["ipv4Address"] == "172.26.40.1"
    # detail level: internal fields (metadata) are stripped
    assert "metadata" not in payload


async def test_get_dns_policy_unknown_returns_not_found(
    make_settings, make_client, respx_mock
) -> None:
    settings = make_settings()
    client = await make_client(settings)
    base = "http://gateway.test/proxy/network/integration/v1"
    respx_mock.get(f"{base}/sites/site-x/dns/policies/dns-missing").mock(
        return_value=httpx.Response(404, json={"code": "not-found", "message": "nope"})
    )
    server = MCPServer(name="t")
    register_dns_tools(server, client, settings)
    result = await server.call_tool(
        "get_dns_policy", {"policy_id": "dns-missing", "site_id": "site-x"}
    )
    payload = result.structured_content
    assert payload["error"] == "not_found"
    assert payload["resource"] == "dns_policy"
    assert payload["query"] == "dns-missing"
