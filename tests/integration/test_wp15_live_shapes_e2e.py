"""E03: WP-15 write contracts validated against live 10.6.106 read shapes.

The fixtures ``*_10_6_106_*.json`` are sanitized captures of the live
gateway (2026-10-16, Network API 10.6.106): real key sets, real enum
values, real ``metadata.origin`` values — only identifiers, names, IPs
(TEST-NET-1), and the PSK (fixed dummy) were replaced. This file proves
the write contracts hold for exactly those shapes:

* get_* returns the live-shape detail, redacted, with a state_hash that
  the write path accepts (hash of what the LLM sees);
* the full-replace PUT body for a live-shape object is the raw object
  minus ``id``/server-managed fields, every live key preserved (incl. the
  WPA3-only ``securityConfiguration`` keys and the 9-IP
  ``ipAddressesOrSubnets`` filter);
* the origin guard fails closed on everything the live gateway can
  actually return besides exact ``USER_DEFINED``: ``SYSTEM_DEFINED``
  (present live on firewall zones / networks), list-level variants
  (``user`` / ``default``), missing ``metadata``, malformed ``metadata``,
  non-dict payloads;
* every rejected attempt is audited and issues **no** PUT.
"""

from __future__ import annotations

import json
from typing import Any

import httpx
import pytest

from tests.conftest import mcp_client_session
from unifi_mcp.safety.audit import reset_audit_log
from unifi_mcp.safety.redaction import REDACTED
from unifi_mcp.safety.state_hash import compute_state_hash
from unifi_mcp.unifi.normalization import normalize

BASE = "http://gateway.test/proxy/network/integration/v1"
SITE = "site-default"
WIFI_PATH = f"/sites/{SITE}/wifi/broadcasts/bc-live-b"
ACL_PATH = f"/sites/{SITE}/acl-rules/acl-rule-allow-0"
WIFI_PSK = "e03-dummy-psk-12345678"


def _mock_detail(
    respx_mock, path: str, raw: dict[str, Any], updated: dict[str, Any]
) -> httpx.MockRoute:
    respx_mock.get(f"{BASE}{path}").mock(return_value=httpx.Response(200, json=raw))
    return respx_mock.put(f"{BASE}{path}").mock(return_value=httpx.Response(200, json=updated))


def _read_events(path) -> list[dict[str, Any]]:
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]


# --- live-shape reads: detail tools over real 10.6.106 payloads ------------


@pytest.mark.parametrize(
    "fixture, resource",
    [
        ("wifi_broadcast_detail_10_6_106_wpa2_personal.json", "wifi_broadcast"),
        ("wifi_broadcast_detail_10_6_106_wpa2_wpa3_personal.json", "wifi_broadcast"),
        ("acl_rule_detail_10_6_106_0.json", "acl_rule"),
        ("acl_rule_detail_10_6_106_1.json", "acl_rule"),
    ],
)
async def test_get_returns_live_shape_redacted_with_hash(
    mcp_app_factory, respx_mock, load_fixture, fixture: str, resource: str
) -> None:
    raw = load_fixture(fixture)
    broadcast_id = raw["id"]
    if resource == "wifi_broadcast":
        path = f"/sites/{SITE}/wifi/broadcasts/{broadcast_id}"
        tool = "get_wifi"
        args = {"broadcast_id": broadcast_id}
    else:
        path = f"/sites/{SITE}/acl-rules/{broadcast_id}"
        tool = "get_acl_rule"
        args = {"rule_id": broadcast_id}
    respx_mock.get(f"{BASE}{path}").mock(return_value=httpx.Response(200, json=raw))
    async with mcp_app_factory(unifi_site_id=SITE) as (app, settings):
        token = settings.mcp_auth_token.get_secret_value()
        async with mcp_client_session(app, token) as session:
            result = await session.call_tool(tool, args)
    data = result.structured_content
    # the LLM view equals the normalized raw: all live keys present,
    # internal fields (metadata/etag/revision) stripped, PSK redacted.
    expected = normalize(raw, level="detail")
    assert {k: v for k, v in data.items() if k != "state_hash"} == expected
    assert "metadata" not in data
    if resource == "wifi_broadcast":
        assert data["securityConfiguration"]["passphrase"] == REDACTED
        assert WIFI_PSK not in json.dumps(data, ensure_ascii=False)
    # the state_hash covers exactly the normalized representation, so the
    # write path (which re-hashes a fresh fetch) accepts it.
    assert data["state_hash"] == compute_state_hash(raw)
    assert data["state_hash"] == compute_state_hash(expected)


async def test_wpa3_only_security_fields_survive_live_shape(
    load_fixture,
) -> None:
    """The 10.6.106 WPA2/WPA3 profile carries fields absent from the older
    fixture; normalization must keep them (the write path round-trips them)."""
    raw = load_fixture("wifi_broadcast_detail_10_6_106_wpa2_wpa3_personal.json")
    detail = normalize(raw, level="detail")
    sec = detail["securityConfiguration"]
    for key in (
        "type",
        "pmfMode",
        "fastRoamingEnabled",
        "wpa3FastRoamingEnabled",
        "saeConfiguration",
    ):
        assert key in sec
    assert sec["saeConfiguration"] == raw["securityConfiguration"]["saeConfiguration"]
    for key in (
        "advertiseDeviceName",
        "arpProxyEnabled",
        "bandSteeringEnabled",
        "bssTransitionEnabled",
        "broadcastingFrequenciesGHz",
    ):
        assert key in detail


# --- live-shape full-replace payloads --------------------------------------


async def test_update_wifi_live_shape_put_preserves_all_live_fields(
    mcp_app_factory, respx_mock, load_fixture, tmp_path
) -> None:
    raw = load_fixture("wifi_broadcast_detail_10_6_106_wpa2_wpa3_personal.json")
    updated = {**raw, "name": "Renamed SSID"}
    audit_path = tmp_path / "audit.jsonl"
    reset_audit_log()
    put_route = _mock_detail(respx_mock, WIFI_PATH, raw, updated)
    async with mcp_app_factory(
        enable_write_tools=True,
        unifi_site_id=SITE,
        audit_log_path=str(audit_path),
    ) as (app, settings):
        token = settings.mcp_auth_token.get_secret_value()
        async with mcp_client_session(app, token) as session:
            result = await session.call_tool(
                "update_wifi",
                {
                    "broadcast_id": raw["id"],
                    "expected_state_hash": compute_state_hash(raw),
                    "name": "Renamed SSID",
                },
            )
    data = result.structured_content
    assert result.is_error is not True
    assert data["name"] == "Renamed SSID"
    assert data["securityConfiguration"]["passphrase"] == REDACTED
    assert WIFI_PSK not in json.dumps(data, ensure_ascii=False)
    assert "metadata" not in data
    assert data["state_hash"] == compute_state_hash(updated)
    # exactly one PUT: the raw live-shape object, id/metadata stripped, the
    # PSK round-tripped, and every 10.6.106-only key preserved.
    assert put_route.call_count == 1
    body = json.loads(put_route.calls.last.request.content)
    expected_body = {k: v for k, v in raw.items() if k not in ("id", "metadata")}
    expected_body["name"] = "Renamed SSID"
    assert body == expected_body
    assert body["securityConfiguration"]["passphrase"] == WIFI_PSK
    assert body["securityConfiguration"]["pmfMode"] == "OPTIONAL"
    assert "saeConfiguration" in body["securityConfiguration"]
    assert body["bandSteeringEnabled"] is True
    events = _read_events(audit_path)
    assert len(events) == 1
    assert events[0]["result"] == "ok"
    assert events[0]["changes"] == {"name": {"old": "e03-sanitized-name", "new": "Renamed SSID"}}


async def test_update_acl_rule_live_shape_put_preserves_filters(
    mcp_app_factory, respx_mock, load_fixture, tmp_path
) -> None:
    raw = load_fixture("acl_rule_detail_10_6_106_2.json")
    updated = {**raw, "enabled": False}
    audit_path = tmp_path / "audit.jsonl"
    reset_audit_log()
    put_route = _mock_detail(respx_mock, f"/sites/{SITE}/acl-rules/{raw['id']}", raw, updated)
    async with mcp_app_factory(
        enable_write_tools=True,
        unifi_site_id=SITE,
        audit_log_path=str(audit_path),
    ) as (app, settings):
        token = settings.mcp_auth_token.get_secret_value()
        async with mcp_client_session(app, token) as session:
            result = await session.call_tool(
                "update_acl_rule",
                {
                    "rule_id": raw["id"],
                    "expected_state_hash": compute_state_hash(raw),
                    "enabled": False,
                },
            )
    data = result.structured_content
    assert result.is_error is not True
    assert data["enabled"] is False
    assert data["action"] == "ALLOW"
    assert data["index"] == 2
    # the live 9-subnet destination filter and the source network filter
    # survive the full-replace unchanged.
    assert data["destinationFilter"]["ipAddressesOrSubnets"] == [
        f"192.0.2.{i}" for i in range(1, 10)
    ]
    assert data["sourceFilter"] == raw["sourceFilter"]
    assert "metadata" not in data
    assert data["state_hash"] == compute_state_hash(updated)
    assert put_route.call_count == 1
    body = json.loads(put_route.calls.last.request.content)
    expected_body = {k: v for k, v in raw.items() if k not in ("id", "metadata")}
    expected_body["enabled"] = False
    assert body == expected_body
    events = _read_events(audit_path)
    assert len(events) == 1
    assert events[0]["result"] == "ok"
    assert events[0]["changes"] == {"enabled": {"old": True, "new": False}}


# --- origin guard: live evidence + fail-closed matrix -----------------------


def test_live_origin_evidence_in_fixtures(load_fixture) -> None:
    """What the live gateway actually returns: details always carry
    metadata.origin == USER_DEFINED; the list level of other read surfaces
    (firewall zones, networks — live 2026-10-16) returns SYSTEM_DEFINED as
    well, so the guard must reject exactly that enum, not just 'default'."""
    for fixture in (
        "wifi_broadcast_detail_10_6_106_wpa2_personal.json",
        "wifi_broadcast_detail_10_6_106_wpa2_wpa3_personal.json",
        "acl_rule_detail_10_6_106_0.json",
        "acl_rule_detail_10_6_106_1.json",
    ):
        raw = load_fixture(fixture)
        assert raw["metadata"] == {"origin": "USER_DEFINED"}
        # metadata.origin is the ONLY metadata key the live gateway sends
        # for these objects — the guard must not require more.
        assert set(raw["metadata"]) == {"origin"}
    for fixture in ("wifi_10_6_106.json", "acl_rules_10_6_106.json"):
        page = load_fixture(fixture)
        for item in page["data"]:
            assert item["metadata"] == {"origin": "USER_DEFINED"}


def test_system_defined_enum_value_is_rejected(load_fixture) -> None:
    """SYSTEM_DEFINED is the real live enum value for non-user objects
    (firewall zones / networks). The guard must reject it end-to-end."""
    from unifi_mcp.safety.guards import assert_user_defined
    from unifi_mcp.tools.errors import InvalidValueError

    raw = load_fixture("acl_rule_detail_10_6_106_0.json")
    system = {**raw, "metadata": {"origin": "SYSTEM_DEFINED"}}
    with pytest.raises(InvalidValueError) as excinfo:
        assert_user_defined(system, resource="acl_rule")
    data = excinfo.value.to_dict()
    assert data["error"] == "validation"
    assert data["field"] == "metadata.origin"
    assert data["allowed"] == ["USER_DEFINED"]


@pytest.mark.parametrize(
    "metadata",
    [
        {"origin": "SYSTEM_DEFINED"},  # real live enum value (zones/networks)
        {"origin": "default"},  # older gateway list-level value
        {"origin": "user"},  # legacy variant
        {"origin": "USER_DEFINED "},  # whitespace-padded: not exact
        {"origin": "user_defined"},  # case-sensitive: not exact
        {"origin": "USER_DEFINED", "configurable": True},  # exact origin, extra key: fine
        {},  # missing origin -> fails closed
    ],
    ids=[
        "system_defined",
        "default",
        "user",
        "padded",
        "lowercase",
        "user_defined_with_extra_key",
        "missing_origin",
    ],
)
def test_origin_guard_matrix(metadata: dict[str, Any]) -> None:
    """Only exact ``USER_DEFINED`` passes; everything else fails closed
    (missing, unknown, or deviating origin)."""
    from unifi_mcp.safety.guards import assert_user_defined
    from unifi_mcp.tools.errors import InvalidValueError

    raw = {
        "id": "widget-live",
        "name": "live shape",
        "metadata": metadata,
    }
    if metadata.get("origin") == "USER_DEFINED":
        assert_user_defined(raw, resource="widget")  # no raise
    else:
        with pytest.raises(InvalidValueError) as excinfo:
            assert_user_defined(raw, resource="widget")
        data = excinfo.value.to_dict()
        assert data["error"] == "validation"
        assert data["field"] == "metadata.origin"
        assert data["allowed"] == ["USER_DEFINED"]


@pytest.mark.parametrize(
    "raw",
    [
        {"id": "w", "name": "n"},  # metadata missing entirely
        {"id": "w", "name": "n", "metadata": None},
        {"id": "w", "name": "n", "metadata": "USER_DEFINED"},
        {"id": "w", "name": "n", "metadata": ["USER_DEFINED"]},
        {"id": "w", "name": "n", "metadata": {"origin": None}},
        {"id": "w", "name": "n", "metadata": {"origin": "USER_DEFINED"}},  # dict OK
    ],
    ids=[
        "no-metadata",
        "metadata-none",
        "metadata-str",
        "metadata-list",
        "origin-none",
        "origin-exact",
    ],
)
def test_origin_guard_malformed_metadata_fails_closed(raw: dict[str, Any]) -> None:
    from unifi_mcp.safety.guards import assert_user_defined
    from unifi_mcp.tools.errors import InvalidValueError

    if raw.get("metadata") == {"origin": "USER_DEFINED"}:
        assert_user_defined(raw, resource="widget")
        return
    with pytest.raises(InvalidValueError) as excinfo:
        assert_user_defined(raw, resource="widget")
    assert excinfo.value.to_dict()["field"] == "metadata.origin"


async def test_update_acl_rule_system_defined_rejects_without_put(
    mcp_app_factory, respx_mock, load_fixture, tmp_path
) -> None:
    """End-to-end over the live ACL shape: the SYSTEM_DEFINED object the
    gateway can actually return is rejected with a validation error,
    audited, and no PUT is issued."""
    raw = {
        **load_fixture("acl_rule_detail_10_6_106_0.json"),
        "metadata": {"origin": "SYSTEM_DEFINED"},
    }
    audit_path = tmp_path / "audit.jsonl"
    reset_audit_log()
    put_route = _mock_detail(respx_mock, ACL_PATH, raw, raw)
    async with mcp_app_factory(
        enable_write_tools=True,
        unifi_site_id=SITE,
        audit_log_path=str(audit_path),
    ) as (app, settings):
        token = settings.mcp_auth_token.get_secret_value()
        async with mcp_client_session(app, token) as session:
            result = await session.call_tool(
                "update_acl_rule",
                {
                    "rule_id": raw["id"],
                    "expected_state_hash": compute_state_hash(raw),
                    "enabled": False,
                },
            )
    data = result.structured_content
    assert data["error"] == "validation"
    assert data["field"] == "metadata.origin"
    assert put_route.call_count == 0
    events = _read_events(audit_path)
    assert len(events) == 1
    assert events[0]["result"] == "validation"


async def test_update_wifi_missing_metadata_fails_closed_end_to_end(
    mcp_app_factory, respx_mock, load_fixture, tmp_path
) -> None:
    """Missing metadata on a live-shape object -> fail-closed (the hash
    still matches: only the origin check fails, and it must)."""
    raw = {
        k: v
        for k, v in load_fixture("wifi_broadcast_detail_10_6_106_wpa2_wpa3_personal.json").items()
        if k != "metadata"
    }
    audit_path = tmp_path / "audit.jsonl"
    reset_audit_log()
    put_route = _mock_detail(respx_mock, WIFI_PATH, raw, raw)
    async with mcp_app_factory(
        enable_write_tools=True,
        unifi_site_id=SITE,
        audit_log_path=str(audit_path),
    ) as (app, settings):
        token = settings.mcp_auth_token.get_secret_value()
        async with mcp_client_session(app, token) as session:
            result = await session.call_tool(
                "update_wifi",
                {
                    "broadcast_id": raw["id"],
                    "expected_state_hash": compute_state_hash(raw),
                    "enabled": False,
                },
            )
    data = result.structured_content
    assert data["error"] == "validation"
    assert data["field"] == "metadata.origin"
    assert put_route.call_count == 0
    events = _read_events(audit_path)
    assert len(events) == 1
    assert events[0]["result"] == "validation"
