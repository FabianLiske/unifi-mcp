"""Unit tests for the startup capability check."""

from __future__ import annotations

import asyncio

import httpx
import pytest

from unifi_mcp.unifi.capabilities import CapabilityCache, detect_capabilities
from unifi_mcp.unifi.errors import UniFiError, UniFiUnavailableError

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


# ---------------------------------------------------------------------------
# CapabilityCache single-flight (design §44, F05)
# ---------------------------------------------------------------------------


async def test_capability_cache_single_flight_on_concurrent_miss(make_client, monkeypatch) -> None:
    """Concurrent callers that miss an empty cache together must share a
    single re-detection (design §44, F05) — not each fire the eleven-request
    probe, which against a degraded gateway is the request amplification F05
    exists to remove."""
    client = await make_client()
    calls = 0
    first_result = object()

    async def fake_detect(_client):
        nonlocal calls
        calls += 1
        await asyncio.sleep(0.05)  # the probe takes real time
        return first_result

    monkeypatch.setattr("unifi_mcp.unifi.capabilities.detect_capabilities", fake_detect)
    cache = CapabilityCache(client, ttl_seconds=60.0)
    results = await asyncio.gather(*(cache.get() for _ in range(8)))
    assert calls == 1  # 8 concurrent misses -> exactly 1 probe
    assert all(r is first_result for r in results)

    # A later call (within the TTL) is served from the cache: no new probe.
    assert await cache.get() is first_result
    assert calls == 1


async def test_capability_cache_failed_rediscover_not_cached(
    respx_mock, make_client, monkeypatch
) -> None:
    """A failed re-detection is not cached: it raises to the caller and the
    next caller retries the probe (no negative caching)."""
    client = await make_client()
    calls = 0
    ok_result = object()

    async def flaky_detect(_client):
        nonlocal calls
        calls += 1
        if calls == 1:
            raise UniFiUnavailableError("gateway saturated")
        return ok_result

    monkeypatch.setattr("unifi_mcp.unifi.capabilities.detect_capabilities", flaky_detect)
    cache = CapabilityCache(client, ttl_seconds=60.0)
    with pytest.raises(UniFiUnavailableError):
        await cache.get()
    assert calls == 1
    # Not cached: the next call retries the probe and succeeds.
    assert await cache.get() is ok_result
    assert calls == 2
