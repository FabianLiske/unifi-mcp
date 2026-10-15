"""End-to-end tests for the WP-15 write tools (update_wifi, update_acl_rule).

The real write groups from :data:`~unifi_mcp.tools.registry.TOOL_GROUPS`
drive the full guarded-update path (read-before-write, origin guard, field
allowlist, audit log) over the real ASGI/MCP stack against a mocked UniFi
endpoint.

Acceptance (design Phase 2: "Jede Änderung ist gegen stale state geschützt
und im Audit Log nachvollziehbar"), asserted here:

* flag off  -> the write tools are absent from ``tools/list`` (26 read-only);
* flag on   -> 28 tools, the two write tools without the read-only hint;
* stale hash / non-user-defined object / no changes / invalid name
  -> structured error, **no** PUT issued, attempt audited;
* correct hash -> exactly **one** full-replace PUT (id/metadata stripped,
  the raw WiFi PSK round-tripped in the body but **never** in the tool
  response), normalized result with a fresh state_hash, success audited
  with the diff and the object's current name.
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

BASE = "http://gateway.test/proxy/network/integration/v1"
SITE = "site-default"
WIFI_PATH = f"/sites/{SITE}/wifi/broadcasts/bc-main"
ACL_PATH = f"/sites/{SITE}/acl-rules/acl-rule-allow"
WIFI_PSK = "fixture-wifi-psk-87654321"

WRITE_TOOL_NAMES = ("update_wifi", "update_acl_rule")


def _mock_detail(
    respx_mock, path: str, raw: dict[str, Any], updated: dict[str, Any]
) -> httpx.MockRoute:
    respx_mock.get(f"{BASE}{path}").mock(return_value=httpx.Response(200, json=raw))
    return respx_mock.put(f"{BASE}{path}").mock(return_value=httpx.Response(200, json=updated))


async def _list_tools(app: Any, token: str) -> dict[str, Any]:
    async with mcp_client_session(app, token) as session:
        tools = await session.list_tools()
    return {t.name: t for t in tools.tools}


# --- feature-flag gating ----------------------------------------------------


async def test_write_tools_absent_when_flag_off(mcp_app_factory) -> None:
    reset_audit_log()
    async with mcp_app_factory() as (app, settings):
        token = settings.mcp_auth_token.get_secret_value()
        tools = await _list_tools(app, token)
    assert len(tools) == 26
    for name in WRITE_TOOL_NAMES:
        assert name not in tools


async def test_write_tools_present_when_flag_on(mcp_app_factory) -> None:
    reset_audit_log()
    async with mcp_app_factory(enable_write_tools=True) as (app, settings):
        token = settings.mcp_auth_token.get_secret_value()
        tools = await _list_tools(app, token)
    assert len(tools) == 28
    for name in WRITE_TOOL_NAMES:
        assert tools[name].annotations is not None
        assert tools[name].annotations.read_only_hint is False
    for name, tool in tools.items():
        if name not in WRITE_TOOL_NAMES:
            assert tool.annotations is not None
            assert tool.annotations.read_only_hint is True


# --- update_wifi: stale hash / origin / no-changes / invalid name -----------


async def test_update_wifi_stale_hash_rejects_without_put(
    mcp_app_factory, respx_mock, load_fixture, tmp_path
) -> None:
    audit_path = tmp_path / "audit.jsonl"
    raw = load_fixture("wifi_broadcast_detail.json")
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
                    "broadcast_id": "bc-main",
                    "expected_state_hash": "sha256:" + "0" * 64,  # stale
                    "name": "Renamed SSID",
                },
            )
    data = result.structured_content
    assert data["error"] == "state_changed"
    assert data["resource"] == "wifi_broadcast"
    assert data["current_state_hash"] == compute_state_hash(raw)
    assert put_route.call_count == 0  # no write was issued
    events = _read_events(audit_path)
    assert len(events) == 1
    assert events[0]["result"] == "state_changed"
    assert events[0]["tool"] == "update_wifi"
    assert events[0]["object_id"] == "bc-main"


async def test_update_wifi_non_user_defined_rejects_without_put(
    mcp_app_factory, respx_mock, load_fixture, tmp_path
) -> None:
    raw = {**load_fixture("wifi_broadcast_detail.json"), "metadata": {"origin": "default"}}
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
                    "broadcast_id": "bc-main",
                    "expected_state_hash": compute_state_hash(raw),
                    "name": "Renamed SSID",
                },
            )
    data = result.structured_content
    assert data["error"] == "validation"
    assert data["field"] == "metadata.origin"
    assert put_route.call_count == 0
    events = _read_events(audit_path)
    assert len(events) == 1
    assert events[0]["result"] == "validation"


@pytest.mark.parametrize("name", ["", "   ", "x" * 33])
async def test_update_wifi_invalid_name_rejects_without_put(
    mcp_app_factory, respx_mock, load_fixture, name: str
) -> None:
    raw = load_fixture("wifi_broadcast_detail.json")
    reset_audit_log()
    put_route = _mock_detail(respx_mock, WIFI_PATH, raw, raw)
    async with mcp_app_factory(
        enable_write_tools=True,
        unifi_site_id=SITE,
    ) as (app, settings):
        token = settings.mcp_auth_token.get_secret_value()
        async with mcp_client_session(app, token) as session:
            result = await session.call_tool(
                "update_wifi",
                {
                    "broadcast_id": "bc-main",
                    "expected_state_hash": compute_state_hash(raw),
                    "name": name,
                },
            )
    data = result.structured_content
    assert data["error"] == "validation"
    assert data["field"] == "name"
    assert put_route.call_count == 0


async def test_update_wifi_no_changes_rejects_without_put(
    mcp_app_factory, respx_mock, load_fixture
) -> None:
    raw = load_fixture("wifi_broadcast_detail.json")
    reset_audit_log()
    put_route = _mock_detail(respx_mock, WIFI_PATH, raw, raw)
    async with mcp_app_factory(enable_write_tools=True, unifi_site_id=SITE) as (
        app,
        settings,
    ):
        token = settings.mcp_auth_token.get_secret_value()
        async with mcp_client_session(app, token) as session:
            result = await session.call_tool(
                "update_wifi",
                {
                    "broadcast_id": "bc-main",
                    "expected_state_hash": compute_state_hash(raw),
                },
            )
    data = result.structured_content
    assert data["error"] == "validation"
    assert data["field"] == "changes"
    assert data["allowed"] == ["name", "enabled"]
    assert put_route.call_count == 0


# --- update_wifi: correct hash -> one PUT, PSK stays in-process -------------


async def test_update_wifi_writes_once_roundtrips_psk_and_redacts_response(
    mcp_app_factory, respx_mock, load_fixture, tmp_path
) -> None:
    raw = load_fixture("wifi_broadcast_detail.json")
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
                    "broadcast_id": "bc-main",
                    "expected_state_hash": compute_state_hash(raw),
                    "name": "Renamed SSID",
                },
            )
    data = result.structured_content
    assert result.is_error is not True
    assert data["name"] == "Renamed SSID"
    assert data["enabled"] is True
    # the PSK is redacted in the response and never appears in raw form.
    assert data["securityConfiguration"]["passphrase"] == REDACTED
    assert WIFI_PSK not in json.dumps(data, ensure_ascii=False)
    assert "metadata" not in data
    assert data["state_hash"] == compute_state_hash(updated)
    # exactly one PUT: the full object, id/metadata stripped, with the raw
    # PSK round-tripped (the gateway requires it for the full-replace DTO).
    assert put_route.call_count == 1
    body = json.loads(put_route.calls.last.request.content)
    expected_body = {k: v for k, v in raw.items() if k not in ("id", "metadata")}
    expected_body["name"] = "Renamed SSID"
    assert body == expected_body
    assert body["securityConfiguration"]["passphrase"] == WIFI_PSK
    # the successful write is audited with the diff and the object's name.
    events = _read_events(audit_path)
    assert len(events) == 1
    assert events[0]["result"] == "ok"
    assert events[0]["object_name"] == "UbiquitiAP"
    assert events[0]["changes"] == {"name": {"old": "UbiquitiAP", "new": "Renamed SSID"}}


# --- update_acl_rule ---------------------------------------------------------


async def test_update_acl_rule_stale_hash_rejects_without_put(
    mcp_app_factory, respx_mock, load_fixture, tmp_path
) -> None:
    audit_path = tmp_path / "audit.jsonl"
    raw = load_fixture("acl_rule_detail.json")
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
                    "rule_id": "acl-rule-allow",
                    "expected_state_hash": "sha256:" + "0" * 64,  # stale
                    "enabled": False,
                },
            )
    data = result.structured_content
    assert data["error"] == "state_changed"
    assert data["resource"] == "acl_rule"
    assert data["current_state_hash"] == compute_state_hash(raw)
    assert put_route.call_count == 0
    events = _read_events(audit_path)
    assert len(events) == 1
    assert events[0]["result"] == "state_changed"
    assert events[0]["tool"] == "update_acl_rule"


async def test_update_acl_rule_no_changes_rejects_without_put(
    mcp_app_factory, respx_mock, load_fixture
) -> None:
    raw = load_fixture("acl_rule_detail.json")
    reset_audit_log()
    put_route = _mock_detail(respx_mock, ACL_PATH, raw, raw)
    async with mcp_app_factory(enable_write_tools=True, unifi_site_id=SITE) as (
        app,
        settings,
    ):
        token = settings.mcp_auth_token.get_secret_value()
        async with mcp_client_session(app, token) as session:
            result = await session.call_tool(
                "update_acl_rule",
                {
                    "rule_id": "acl-rule-allow",
                    "expected_state_hash": compute_state_hash(raw),
                },
            )
    data = result.structured_content
    assert data["error"] == "validation"
    assert data["field"] == "enabled"
    assert put_route.call_count == 0


async def test_update_acl_rule_writes_once_preserving_filters(
    mcp_app_factory, respx_mock, load_fixture, tmp_path
) -> None:
    raw = load_fixture("acl_rule_detail.json")
    updated = {**raw, "enabled": False}
    audit_path = tmp_path / "audit.jsonl"
    reset_audit_log()
    put_route = _mock_detail(respx_mock, ACL_PATH, raw, updated)
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
                    "rule_id": "acl-rule-allow",
                    "expected_state_hash": compute_state_hash(raw),
                    "enabled": False,
                },
            )
    data = result.structured_content
    assert result.is_error is not True
    assert data["enabled"] is False
    # everything else is preserved.
    assert data["action"] == "ALLOW"
    assert data["index"] == 0
    assert data["sourceFilter"] == raw["sourceFilter"]
    assert data["destinationFilter"] == raw["destinationFilter"]
    assert "metadata" not in data
    assert data["state_hash"] == compute_state_hash(updated)
    # exactly one PUT: only 'enabled' changed, id/metadata stripped.
    assert put_route.call_count == 1
    body = json.loads(put_route.calls.last.request.content)
    expected_body = {k: v for k, v in raw.items() if k not in ("id", "metadata")}
    expected_body["enabled"] = False
    assert body == expected_body
    events = _read_events(audit_path)
    assert len(events) == 1
    assert events[0]["result"] == "ok"
    assert events[0]["object_name"] == "040-0-TRUSTED-ALLOW-K8S"
    assert events[0]["changes"] == {"enabled": {"old": True, "new": False}}


# --- helpers ------------------------------------------------------------------


def _read_events(path) -> list[dict[str, Any]]:
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]
