"""Unit tests for unifi_mcp.unifi.normalization."""

from __future__ import annotations

from unifi_mcp.safety.redaction import REDACTED
from unifi_mcp.unifi.models import Page
from unifi_mcp.unifi.normalization import (
    DEFAULT_LIMIT,
    ELLIPSIS,
    MAX_LIMIT,
    clamp_limit,
    normalize,
    page_to_mcp,
)


def _device() -> dict:
    return {
        "id": "dev1",
        "name": "USW-Pro-24",
        "model": "UXG-Pro",
        "firmwareVersion": "10.6.101",
        "ipAddress": "172.26.10.12",
        "macAddress": "aa:bb:cc:dd:ee:ff",
        "state": "online",
        "features": ["switch", "ap"],
        "interfaces": [{"name": "port1", "mac": "11:22"}, {"name": "port2", "mac": "33:44"}],
        "metadata": {"id": "dev1", "revision": 7, "origin": "local"},
        "etag": "abc123",
        "passphrase": "supersecret",
    }


def test_constants() -> None:
    assert DEFAULT_LIMIT == 50
    assert MAX_LIMIT == 200


def test_detail_strips_internal_fields() -> None:
    result = normalize(_device(), level="detail")
    assert "metadata" not in result
    assert "etag" not in result
    assert result["id"] == "dev1"


def test_detail_keeps_nested_structures() -> None:
    result = normalize(_device(), level="detail")
    assert result["interfaces"] == [
        {"name": "port1", "mac": "11:22"},
        {"name": "port2", "mac": "33:44"},
    ]
    assert result["features"] == ["switch", "ap"]


def test_detail_redacts_secrets() -> None:
    result = normalize(_device(), level="detail")
    assert result["passphrase"] == REDACTED
    assert "supersecret" not in str(result)


def test_strip_internal_is_recursive() -> None:
    payload = {"outer": {"metadata": {"revision": 3}, "etag": "e", "keep": 1}}
    result = normalize(payload, level="detail")
    assert result["outer"] == {"keep": 1}


def test_summary_collapses_nested_objects() -> None:
    result = normalize(_device(), level="summary")
    assert result["interfaces"] == [ELLIPSIS, ELLIPSIS]
    assert result["id"] == "dev1"
    assert result["name"] == "USW-Pro-24"


def test_summary_keeps_shallow_scalars_and_scalar_lists() -> None:
    result = normalize(_device(), level="summary")
    assert result["features"] == ["switch", "ap"]
    assert result["state"] == "online"
    assert result["firmwareVersion"] == "10.6.101"


def test_summary_strips_internal_and_redacts() -> None:
    result = normalize(_device(), level="summary")
    assert "metadata" not in result
    assert "etag" not in result
    assert result["passphrase"] == REDACTED


def test_summary_deeper_max_depth_keeps_more() -> None:
    result = normalize(_device(), level="summary", max_depth=2)
    # At max_depth=2 the interface objects (depth 2) survive.
    assert result["interfaces"] == [
        {"name": "port1", "mac": "11:22"},
        {"name": "port2", "mac": "33:44"},
    ]


def test_normalize_does_not_mutate_input() -> None:
    original = _device()
    snapshot = repr(original)
    normalize(original, level="summary")
    normalize(original, level="detail")
    assert repr(original) == snapshot


def test_clamp_limit_defaults_and_caps() -> None:
    assert clamp_limit(None) == DEFAULT_LIMIT
    assert clamp_limit(0) == DEFAULT_LIMIT
    assert clamp_limit(-5) == DEFAULT_LIMIT
    assert clamp_limit(1) == 1
    assert clamp_limit(50) == 50
    assert clamp_limit(200) == MAX_LIMIT
    assert clamp_limit(201) == MAX_LIMIT
    assert clamp_limit(10_000) == MAX_LIMIT
    assert clamp_limit(10_000, max_limit=100) == 100


def _page(offset: int, total: int, n: int) -> Page:
    data = [{"id": f"c{i}", "hostname": f"host-{i}", "apiKey": "k"} for i in range(n)]
    return Page(offset=offset, limit=n, count=n, total_count=total, data=data)


def test_page_to_mcp_shape_and_next_offset() -> None:
    result = page_to_mcp(_page(0, total=5, n=2))
    assert result["count"] == 2
    assert result["total_count"] == 5
    assert result["next_offset"] == 2
    assert len(result["items"]) == 2


def test_page_to_mcp_last_page_has_no_next_offset() -> None:
    result = page_to_mcp(_page(4, total=5, n=1))
    assert result["count"] == 1
    assert result["next_offset"] is None


def test_page_to_mcp_normalizes_items() -> None:
    result = page_to_mcp(_page(0, total=1, n=1), level="detail")
    item = result["items"][0]
    assert item["id"] == "c0"
    assert item["hostname"] == "host-0"
    assert item["apiKey"] == REDACTED


def test_page_to_mcp_summary_level() -> None:
    result = page_to_mcp(_page(0, total=1, n=1), level="summary")
    assert result["items"][0]["apiKey"] == REDACTED
