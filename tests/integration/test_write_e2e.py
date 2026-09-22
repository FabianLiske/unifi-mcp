"""End-to-end tests for the WP-14 safe-writes foundation (design §35).

A *test-only* write tool (``update_widget``) is registered in a synthetic
:class:`ToolGroup` gated by ``enable_write_tools`` and drives the full
``guarded_update`` path against a mocked UniFi endpoint. It is never shipped —
it exists purely to exercise the foundation (read-before-write, origin guard,
field allowlist, audit log) over the real ASGI/MCP stack.

Acceptance (design §35), asserted here:

* flag off  -> the write tool is absent from ``tools/list`` (26 read-only);
* flag on   -> the write tool is present (27 total), not read-only;
* stale hash -> ``state_changed`` error, **no** PUT issued, attempt audited;
* forbidden field / non-user-defined -> ``validation`` error, no PUT, audited;
* correct hash -> exactly **one** PUT of the full object (id/metadata
  stripped), normalized result with a fresh state_hash, success audited.
"""

from __future__ import annotations

import json
from typing import TYPE_CHECKING, Any

import httpx
from mcp_types import ToolAnnotations

from tests.conftest import mcp_client_session
from unifi_mcp.safety.audit import reset_audit_log
from unifi_mcp.safety.state_hash import compute_state_hash
from unifi_mcp.tools.common import resolve_site, wrap_tool
from unifi_mcp.tools.registry import TOOL_GROUPS, ToolGroup
from unifi_mcp.tools.writes import guarded_update

if TYPE_CHECKING:  # pragma: no cover
    from mcp.server.mcpserver import MCPServer

    from unifi_mcp.config import Settings
    from unifi_mcp.unifi.client import UniFiClient

BASE = "http://gateway.test/proxy/network/integration/v1"
SITE = "site-default"
WIDGET_PATH = f"/sites/{SITE}/widgets/widget-1"

#: The mutable object as the gateway would return it (with server-managed
#: ``id``/``metadata`` and internal ``revision``/``etag``).
WIDGET_RAW: dict[str, Any] = {
    "id": "widget-1",
    "name": "Test Widget",
    "type": "DASHBOARD",
    "description": "old description",
    "metadata": {"origin": "USER_DEFINED", "creator": "user", "creationTime": 1},
    "revision": 3,
    "etag": "0x3",
}


def _widget_hash(raw: dict[str, Any]) -> str:
    return compute_state_hash(raw)


def register_test_write_tools(server: MCPServer, client: UniFiClient, settings: Settings) -> None:
    """Register the test-only ``update_widget`` write tool.

    ``name`` is deliberately **not** on the allowlist so the E2E suite can
    exercise the field-allowlist guard (an allowed field: ``description``).
    """

    async def update_widget(
        widget_id: str,
        expected_state_hash: str,
        description: str | None = None,
        name: str | None = None,
        site_id: str | None = None,
    ) -> dict[str, Any]:
        site = await resolve_site(client, settings, site_id)
        changes: dict[str, Any] = {}
        if description is not None:
            changes["description"] = description
        if name is not None:
            changes["name"] = name
        path = f"/sites/{site}/widgets/{widget_id}"
        return await guarded_update(
            client,
            path=path,
            object_id=widget_id,
            resource="widget",
            expected_state_hash=expected_state_hash,
            changes=changes,
            allowed_fields=frozenset({"description"}),
            tool="update_widget",
            site_id=site,
            object_name="Test Widget",
        )

    server.add_tool(
        wrap_tool(
            "update_widget", settings.max_tool_response_bytes, update_widget, write=True
        ),
        name="update_widget",
        title="Update Widget",
        description="Test-only write tool (WP-14 foundation).",
        annotations=ToolAnnotations(read_only_hint=False),
    )


WRITE_GROUP = ToolGroup(
    name="test-writes",
    gate=lambda s: s.enable_write_tools,
    register=register_test_write_tools,
)


def _mock_widget(respx_mock, raw: dict[str, Any], updated: dict[str, Any]) -> httpx.MockRoute:
    respx_mock.get(f"{BASE}{WIDGET_PATH}").mock(
        return_value=httpx.Response(200, json=raw)
    )
    return respx_mock.put(f"{BASE}{WIDGET_PATH}").mock(
        return_value=httpx.Response(200, json=updated)
    )


async def _list_tool_names(app: Any, token: str) -> list[str]:
    async with mcp_client_session(app, token) as session:
        tools = await session.list_tools()
    return [t.name for t in tools.tools]


# --- §35: feature-flag gating ------------------------------------------------


async def test_write_tool_absent_when_flag_off(mcp_app_factory) -> None:
    reset_audit_log()
    async with mcp_app_factory(groups=TOOL_GROUPS + (WRITE_GROUP,)) as (app, settings):
        names = await _list_tool_names(app, settings.mcp_auth_token.get_secret_value())
    assert "update_widget" not in names
    assert len(names) == 26


async def test_write_tool_present_when_flag_on(mcp_app_factory) -> None:
    reset_audit_log()
    async with mcp_app_factory(
        groups=TOOL_GROUPS + (WRITE_GROUP,), enable_write_tools=True
    ) as (app, settings):
        token = settings.mcp_auth_token.get_secret_value()
        names = await _list_tool_names(app, token)
        async with mcp_client_session(app, token) as session:
            tools = {t.name: t for t in (await session.list_tools()).tools}
    assert "update_widget" in names
    assert len(names) == 27
    # a write tool must not advertise the read-only hint.
    assert tools["update_widget"].annotations is not None
    assert tools["update_widget"].annotations.read_only_hint is False


# --- §35: stale write is rejected, no PUT, audited ---------------------------


async def test_stale_hash_rejects_without_put(mcp_app_factory, respx_mock, tmp_path) -> None:
    audit_path = tmp_path / "audit.jsonl"
    reset_audit_log()
    put_route = _mock_widget(respx_mock, WIDGET_RAW, WIDGET_RAW)
    async with mcp_app_factory(
        groups=TOOL_GROUPS + (WRITE_GROUP,),
        enable_write_tools=True,
        unifi_site_id=SITE,
        audit_log_path=str(audit_path),
    ) as (app, settings):
        token = settings.mcp_auth_token.get_secret_value()
        async with mcp_client_session(app, token) as session:
            result = await session.call_tool(
                "update_widget",
                {
                    "widget_id": "widget-1",
                    "expected_state_hash": "sha256:" + "0" * 64,  # stale
                    "description": "new description",
                },
            )
    data = result.structured_content
    assert data["error"] == "state_changed"
    assert data["resource"] == "widget"
    # carries the CURRENT hash so the LLM can retry immediately.
    assert data["current_state_hash"] == _widget_hash(WIDGET_RAW)
    assert put_route.call_count == 0  # no write was issued
    # the rejected attempt is still audited (design §27).
    events = _read_events(audit_path)
    assert len(events) == 1
    assert events[0]["result"] == "state_changed"
    assert events[0]["tool"] == "update_widget"
    assert events[0]["object_id"] == "widget-1"


# --- §35: non-user-defined object is rejected --------------------------------


async def test_non_user_defined_rejects_without_put(mcp_app_factory, respx_mock, tmp_path) -> None:
    audit_path = tmp_path / "audit.jsonl"
    system_widget = {**WIDGET_RAW, "metadata": {"origin": "SYSTEM"}}
    reset_audit_log()
    put_route = _mock_widget(respx_mock, system_widget, system_widget)
    async with mcp_app_factory(
        groups=TOOL_GROUPS + (WRITE_GROUP,),
        enable_write_tools=True,
        unifi_site_id=SITE,
        audit_log_path=str(audit_path),
    ) as (app, settings):
        token = settings.mcp_auth_token.get_secret_value()
        async with mcp_client_session(app, token) as session:
            result = await session.call_tool(
                "update_widget",
                {
                    "widget_id": "widget-1",
                    "expected_state_hash": _widget_hash(system_widget),
                    "description": "new description",
                },
            )
    data = result.structured_content
    assert data["error"] == "validation"
    assert data["field"] == "metadata.origin"
    assert put_route.call_count == 0
    events = _read_events(audit_path)
    assert len(events) == 1
    assert events[0]["result"] == "validation"


# --- §35: field allowlist is enforced ---------------------------------------


async def test_forbidden_field_rejects_without_put(mcp_app_factory, respx_mock, tmp_path) -> None:
    audit_path = tmp_path / "audit.jsonl"
    reset_audit_log()
    put_route = _mock_widget(respx_mock, WIDGET_RAW, WIDGET_RAW)
    async with mcp_app_factory(
        groups=TOOL_GROUPS + (WRITE_GROUP,),
        enable_write_tools=True,
        unifi_site_id=SITE,
        audit_log_path=str(audit_path),
    ) as (app, settings):
        token = settings.mcp_auth_token.get_secret_value()
        async with mcp_client_session(app, token) as session:
            result = await session.call_tool(
                "update_widget",
                {
                    "widget_id": "widget-1",
                    "expected_state_hash": _widget_hash(WIDGET_RAW),
                    "name": "hijacked",  # not on the allowlist
                },
            )
    data = result.structured_content
    assert data["error"] == "validation"
    assert data["field"] == "changes"
    assert data["value"] == "name"
    assert data["allowed"] == ["description"]
    assert put_route.call_count == 0
    events = _read_events(audit_path)
    assert len(events) == 1
    assert events[0]["result"] == "validation"


# --- §35: correct hash -> exactly one full-replace PUT, audited --------------


async def test_correct_hash_writes_once_and_audits(mcp_app_factory, respx_mock, tmp_path) -> None:
    audit_path = tmp_path / "audit.jsonl"
    updated = {**WIDGET_RAW, "description": "new description"}
    reset_audit_log()
    put_route = _mock_widget(respx_mock, WIDGET_RAW, updated)
    async with mcp_app_factory(
        groups=TOOL_GROUPS + (WRITE_GROUP,),
        enable_write_tools=True,
        unifi_site_id=SITE,
        audit_log_path=str(audit_path),
    ) as (app, settings):
        token = settings.mcp_auth_token.get_secret_value()
        async with mcp_client_session(app, token) as session:
            result = await session.call_tool(
                "update_widget",
                {
                    "widget_id": "widget-1",
                    "expected_state_hash": _widget_hash(WIDGET_RAW),
                    "description": "new description",
                },
            )
    data = result.structured_content
    assert result.is_error is not True
    assert data["description"] == "new description"
    # the result is normalized (no server-managed/internal fields) and
    # carries a fresh state_hash of the updated object.
    assert "metadata" not in data
    assert "etag" not in data
    assert "revision" not in data
    assert data["state_hash"] == _widget_hash(updated)
    # exactly one PUT, of the full object with id/metadata stripped.
    assert put_route.call_count == 1
    body = json.loads(put_route.calls.last.request.content)
    assert body == {
        "name": "Test Widget",
        "type": "DASHBOARD",
        "description": "new description",
    }
    assert "id" not in body
    assert "metadata" not in body
    # the successful write is audited with the diff.
    events = _read_events(audit_path)
    assert len(events) == 1
    assert events[0]["result"] == "ok"
    assert events[0]["changes"] == {
        "description": {"old": "old description", "new": "new description"}
    }


# --- helpers ----------------------------------------------------------------


def _read_events(path) -> list[dict[str, Any]]:
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]
