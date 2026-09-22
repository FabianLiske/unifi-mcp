"""Unit tests for the startup capability check."""

from __future__ import annotations

import httpx
import pytest

from unifi_mcp.unifi.capabilities import detect_capabilities
from unifi_mcp.unifi.errors import UniFiError

BASE = "http://gateway.test/proxy/network/integration/v1"
SITE = "site-abc"
SITE_SCOPED = (
    "/devices",
    "/clients",
    "/networks",
    "/dns/policies",
    "/wifi/broadcasts",
    "/firewall/zones",
    "/acl-rules",
    "/traffic-matching-lists",
)


def _page() -> dict[str, object]:
    return {"offset": 0, "limit": 1, "count": 0, "totalCount": 0, "data": []}


def _mock_env(
    respx_mock,
    site_id: str,
    overrides: dict[str, httpx.Response] | None = None,
) -> None:
    """Mock /info + /sites + all probe routes (with optional overrides).

    The top-level ``/pending-devices`` probe is mocked alongside the
    site-scoped ones; pass a ``/pending-devices`` key in *overrides* to
    serve a different response.
    """
    overrides = overrides or {}
    respx_mock.get(f"{BASE}/info").mock(
        return_value=httpx.Response(200, json={"applicationVersion": "10.6.101"})
    )
    respx_mock.get(f"{BASE}/sites").mock(
        return_value=httpx.Response(
            200,
            json={
                "offset": 0,
                "limit": 1,
                "count": 1,
                "totalCount": 1,
                "data": [{"id": site_id, "name": "Default"}],
            },
        )
    )
    for suffix in SITE_SCOPED:
        full = f"/sites/{site_id}{suffix}"
        if full in overrides:
            respx_mock.get(f"{BASE}{full}").mock(return_value=overrides[full])
        else:
            respx_mock.get(f"{BASE}{full}").mock(return_value=httpx.Response(200, json=_page()))
    respx_mock.get(f"{BASE}/pending-devices").mock(
        return_value=overrides.get("/pending-devices", httpx.Response(200, json=_page()))
    )


async def test_all_categories_ok(respx_mock, make_client, make_settings) -> None:
    _mock_env(respx_mock, SITE)
    client = await make_client(make_settings(unifi_site_id=SITE))
    caps = await detect_capabilities(client)
    assert caps.application_info.application_version == "10.6.101"
    for name in (
        "sites",
        "devices",
        "clients",
        "networks",
        "dns_policies",
        "wifi",
        "firewall",
        "acl",
        "traffic_matching_lists",
        "pending_devices",
    ):
        assert caps.is_available(name), name
    as_dict = caps.to_dict()
    assert as_dict["application_version"] == "10.6.101"
    assert as_dict["capabilities"]["firewall"]["status"] == "ok"


async def test_firewall_not_configured(respx_mock, make_client, make_settings) -> None:
    _mock_env(
        respx_mock,
        SITE,
        {
            f"/sites/{SITE}/firewall/zones": httpx.Response(
                400,
                json={
                    "code": "api.firewall.zone-based-firewall-not-configured",
                    "message": "zone-based firewall not configured",
                },
            )
        },
    )
    client = await make_client(make_settings(unifi_site_id=SITE))
    caps = await detect_capabilities(client)
    assert caps.is_available("firewall") is False
    assert caps.categories["firewall"].status == "not_configured"
    assert caps.is_available("devices") is True


async def test_missing_endpoint_unavailable(respx_mock, make_client, make_settings) -> None:
    _mock_env(
        respx_mock,
        SITE,
        {
            f"/sites/{SITE}/wifi/broadcasts": httpx.Response(
                404, json={"code": "api.resource.not-found", "message": "nope"}
            )
        },
    )
    client = await make_client(make_settings(unifi_site_id=SITE))
    caps = await detect_capabilities(client)
    assert caps.categories["wifi"].status == "unavailable"
    assert caps.is_available("wifi") is False


async def test_auth_failure_marked_unavailable(respx_mock, make_client, make_settings) -> None:
    _mock_env(
        respx_mock,
        SITE,
        {
            f"/sites/{SITE}/devices": httpx.Response(
                403, json={"code": "api.authorization.denied", "message": "nope"}
            )
        },
    )
    client = await make_client(make_settings(unifi_site_id=SITE))
    caps = await detect_capabilities(client)
    assert caps.categories["devices"].status == "unavailable"


async def test_first_site_used_when_none_configured(respx_mock, make_client, make_settings) -> None:
    _mock_env(respx_mock, "auto-site-1")
    client = await make_client(make_settings())  # unifi_site_id=None
    caps = await detect_capabilities(client)
    assert caps.is_available("sites") is True
    assert caps.is_available("devices") is True
    assert caps.is_available("traffic_matching_lists") is True


async def test_sites_failure_cascades_without_site_id(
    respx_mock, make_client, make_settings
) -> None:
    respx_mock.get(f"{BASE}/info").mock(
        return_value=httpx.Response(200, json={"applicationVersion": "10.6.101"})
    )
    respx_mock.get(f"{BASE}/sites").mock(
        return_value=httpx.Response(500, json={"code": "api.internal", "message": "boom"})
    )
    # The top-level probe is independent of the site but hits the same
    # broken gateway.
    respx_mock.get(f"{BASE}/pending-devices").mock(
        return_value=httpx.Response(500, json={"code": "api.internal", "message": "boom"})
    )
    client = await make_client(make_settings(unifi_max_retries=0))  # no site id known
    caps = await detect_capabilities(client)
    assert caps.categories["sites"].status == "unavailable"
    assert caps.categories["devices"].status == "unavailable"
    assert caps.categories["acl"].status == "unavailable"
    assert caps.categories["pending_devices"].status == "unavailable"


async def test_core_failure_raises(respx_mock, make_client, make_settings) -> None:
    respx_mock.get(f"{BASE}/info").mock(
        return_value=httpx.Response(500, json={"code": "api.internal", "message": "boom"})
    )
    client = await make_client(make_settings(unifi_max_retries=0))
    with pytest.raises(UniFiError):
        await detect_capabilities(client)
