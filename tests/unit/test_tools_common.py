"""Unit tests for unifi_mcp.tools.common (tool framework)."""

from __future__ import annotations

import inspect

import httpx
import pytest
from prometheus_client import REGISTRY

from unifi_mcp.tools.common import (
    fetch_list,
    resolve_site,
    site_overview,
    unifi_error_to_dict,
    wrap_tool,
)
from unifi_mcp.tools.errors import AmbiguousMatchError, NotFoundError
from unifi_mcp.unifi.errors import (
    UniFiAuthenticationError,
    UniFiAuthorizationError,
    UniFiConflictError,
    UniFiError,
    UniFiNotFoundError,
    UniFiRateLimitError,
    UniFiResponseTooLargeError,
    UniFiUnavailableError,
    UniFiValidationError,
)


def _counter(labels: dict[str, str]) -> float:
    value = REGISTRY.get_sample_value("unifi_mcp_requests_total", labels)
    return 0.0 if value is None else value


class _StubClient:
    """Client stand-in for site resolution tests (no HTTP involved)."""

    def __init__(self, site_id: str | None = None) -> None:
        self._site_id = site_id
        self._pages: dict[str, dict] = {}

    @property
    def site_id(self) -> str | None:
        return self._site_id

    async def get_list(self, path: str, *, params: dict | None = None) -> object:
        from unifi_mcp.unifi.models import Page

        return Page.model_validate(self._pages[path])


# --- unifi_error_to_dict -----------------------------------------------------


def test_unifi_error_mapping() -> None:
    cases = [
        (UniFiNotFoundError("x", status_code=404, api_code="not-found"), "not_found"),
        (UniFiAuthenticationError("x", status_code=401), "authentication"),
        (UniFiAuthorizationError("x", status_code=403), "authorization"),
        (
            UniFiValidationError(
                "x", status_code=400, api_code="api.firewall.zone-based-firewall-not-configured"
            ),
            "unsupported",
        ),
        (
            UniFiValidationError("x", status_code=400, api_code="api.request.invalid-filter"),
            "validation",
        ),
        (UniFiRateLimitError("x", status_code=429, retry_after=1.5), "rate_limited"),
        (UniFiConflictError("x", status_code=409), "conflict"),
        (UniFiResponseTooLargeError("x"), "response_too_large"),
        (UniFiUnavailableError("x", status_code=503), "unavailable"),
        (UniFiError("x", status_code=500), "unifi_error"),
    ]
    for exc, code in cases:
        payload = unifi_error_to_dict(exc)
        assert payload["error"] == code, code
        assert payload["message"] == "x"
        assert list(payload)[0] == "error"
        assert list(payload)[-1] == "message"


def test_unifi_error_api_code_and_retry_after() -> None:
    payload = unifi_error_to_dict(
        UniFiRateLimitError("slow down", status_code=429, api_code="rate-limited", retry_after=2.0)
    )
    assert payload["api_code"] == "rate-limited"
    assert payload["retry_after_seconds"] == 2.0
    assert "api_code" not in unifi_error_to_dict(UniFiNotFoundError("x"))


# --- wrap_tool ---------------------------------------------------------------


async def test_wrap_tool_ok_records_metrics() -> None:
    async def tool(x: int = 1) -> dict[str, object]:
        return {"value": x}

    wrapped = wrap_tool("unit_tool_ok", 1024, tool)
    payload = await wrapped(x=2)
    assert payload == {"value": 2}
    assert _counter({"tool": "unit_tool_ok", "status": "ok"}) >= 1.0


async def test_wrap_tool_maps_tool_error() -> None:
    async def tool() -> dict[str, object]:
        raise AmbiguousMatchError(
            [{"id": "a", "hostname": "wk-5"}, {"id": "b", "hostname": "wk-5-old"}]
        )

    wrapped = wrap_tool("unit_tool_ambiguous", 1024, tool)
    payload = await wrapped()
    assert payload["error"] == "ambiguous_match"
    assert len(payload["matches"]) == 2
    assert payload["message"]
    assert _counter({"tool": "unit_tool_ambiguous", "status": "ambiguous_match"}) >= 1.0


async def test_wrap_tool_maps_unifi_error() -> None:
    async def tool() -> dict[str, object]:
        raise UniFiUnavailableError("gateway unreachable", status_code=503)

    wrapped = wrap_tool("unit_tool_unavailable", 1024, tool)
    payload = await wrapped()
    assert payload["error"] == "unavailable"
    assert payload["message"] == "gateway unreachable"


async def test_wrap_tool_internal_error() -> None:
    async def tool() -> dict[str, object]:
        raise RuntimeError("boom")

    wrapped = wrap_tool("unit_tool_crash", 1024, tool)
    payload = await wrapped()
    assert payload["error"] == "internal_error"
    assert "boom" not in str(payload)
    assert _counter({"tool": "unit_tool_crash", "status": "internal_error"}) >= 1.0


async def test_wrap_tool_enforces_size_cap() -> None:
    async def tool() -> dict[str, object]:
        return {"blob": "x" * 100}

    wrapped = wrap_tool("unit_tool_too_large", 50, tool)
    payload = await wrapped()
    assert payload["error"] == "response_too_large"


def test_wrap_tool_keeps_signature_for_schema() -> None:
    async def tool(site_id: str | None = None, limit: int = 50) -> dict[str, object]:
        return {}

    wrapped = wrap_tool("unit_tool_schema", 1024, tool)
    params = inspect.signature(wrapped).parameters
    assert set(params) == {"site_id", "limit"}
    assert params["limit"].default == 50
    # functools.wraps keeps the original function reachable for the SDK schema.
    assert getattr(wrapped, "__wrapped__", None) is tool


# --- resolve_site / site_overview ---------------------------------------------


async def test_resolve_site_precedence(make_settings) -> None:
    configured = make_settings(unifi_site_id="configured-site")
    client = _StubClient("configured-site")
    # explicit parameter wins
    assert await resolve_site(client, configured, " requested ") == "requested"
    # configured site (client.site_id derives from settings)
    assert await resolve_site(client, configured, None) == "configured-site"
    assert await resolve_site(client, configured) == "configured-site"


async def test_resolve_site_falls_back_to_first_site(make_settings) -> None:
    client = _StubClient(None)
    client._pages["/sites"] = {
        "offset": 0,
        "limit": 1,
        "count": 1,
        "totalCount": 1,
        "data": [{"id": "first", "name": "Default"}],
    }
    assert await resolve_site(client, make_settings(), None) == "first"


async def test_resolve_site_no_site_available(make_settings) -> None:
    client = _StubClient(None)
    client._pages["/sites"] = {"offset": 0, "limit": 1, "count": 0, "totalCount": 0, "data": []}
    with pytest.raises(NotFoundError):
        await resolve_site(client, make_settings(), None)


async def test_site_overview_finds_active_site(make_settings, load_fixture) -> None:
    client = _StubClient(None)
    client._pages["/sites"] = load_fixture("sites.json")
    overview = await site_overview(client, make_settings())
    assert overview == {"id": "site-default", "name": "Default"}


async def test_site_overview_picks_configured_site(make_settings, load_fixture) -> None:
    client = _StubClient("site-guest")
    client._pages["/sites"] = load_fixture("sites.json")
    overview = await site_overview(client, make_settings(unifi_site_id="site-guest"))
    assert overview == {"id": "site-guest", "name": "Guest"}


async def test_site_overview_configured_site_not_in_list(make_settings) -> None:
    client = _StubClient("hidden-site")
    client._pages["/sites"] = {"offset": 0, "limit": 200, "count": 0, "totalCount": 0, "data": []}
    overview = await site_overview(client, make_settings(unifi_site_id="hidden-site"))
    assert overview == {"id": "hidden-site", "name": None}


async def test_site_overview_no_sites(make_settings) -> None:
    client = _StubClient(None)
    client._pages["/sites"] = {"offset": 0, "limit": 200, "count": 0, "totalCount": 0, "data": []}
    with pytest.raises(NotFoundError):
        await site_overview(client, make_settings())


# --- fetch_list ----------------------------------------------------------------


async def test_fetch_list_returns_mcp_envelope(respx_mock, make_client, load_fixture) -> None:
    client = await make_client()
    respx_mock.get("http://gateway.test/proxy/network/integration/v1/sites/site-x/devices").mock(
        return_value=httpx.Response(200, json=load_fixture("devices.json"))
    )
    payload = await fetch_list(client, "/sites/site-x/devices")
    assert payload["count"] == 4
    assert payload["total_count"] == 4
    assert payload["next_offset"] is None
    first = payload["items"][0]
    assert first["id"] == "dev-gateway"
    # the list endpoint reports interfaces as plain strings — kept as-is
    assert first["interfaces"] == ["ports"]


async def test_fetch_list_prunes_detail_shape_containers(respx_mock, make_client) -> None:
    """Detail-shaped items (dict containers) are pruned at the summary level."""
    client = await make_client()
    page = {
        "offset": 0,
        "limit": 50,
        "count": 1,
        "totalCount": 1,
        "data": [
            {
                "id": "dev-1",
                "name": "Switch",
                "interfaces": [{"id": "eth0", "name": "eth0", "stats": {"rx": 1}}],
            }
        ],
    }
    respx_mock.get("http://gateway.test/proxy/network/integration/v1/sites/site-x/devices").mock(
        return_value=httpx.Response(200, json=page)
    )
    payload = await fetch_list(client, "/sites/site-x/devices")
    # summary normalization prunes containers deeper than depth 1
    assert payload["items"][0]["interfaces"] == ["…"]
