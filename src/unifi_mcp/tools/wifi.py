"""WiFi broadcast (SSID) tools (design §10.6).

Endpoints (docs/unifi-api-notes.md §5/§7, live-verified):

- list:   ``/sites/{site}/wifi/broadcasts``
- detail: ``/sites/{site}/wifi/broadcasts/{id}`` — ⚠️ returns the WiFi
  passphrase (``securityConfiguration.passphrase``) in clear text for
  WPA2/WPA3 personal networks.

The shared normalization layer redacts every secret-named field (PSK,
passphrase, ...) at both summary and detail level — the PSK can therefore
never reach the LLM (design §8/§10.6: "Nie PSK zurückgeben").
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from mcp_types import ToolAnnotations

from unifi_mcp.tools.common import (
    fetch_list,
    resolve_site,
    with_state_hash,
    wrap_tool,
)
from unifi_mcp.tools.devices import clamp_params
from unifi_mcp.tools.errors import InvalidValueError, NotFoundError
from unifi_mcp.tools.writes import guarded_update
from unifi_mcp.unifi.errors import UniFiNotFoundError
from unifi_mcp.unifi.normalization import normalize

if TYPE_CHECKING:
    from mcp.server.mcpserver import MCPServer

    from unifi_mcp.config import Settings
    from unifi_mcp.unifi.client import UniFiClient

LIST_WIFI_DESCRIPTION = """\
Lists the WiFi networks (broadcast profiles / SSIDs) configured for the
site, with SSID name, security mode, the linked network, and bands. The WiFi
passphrase (PSK) is redacted and never returned by this tool. Returns at most
'limit' profiles (default 50, max 200); when next_offset is set, call again
with offset=next_offset to page. This tool is read-only.
"""

GET_WIFI_DESCRIPTION = """\
Gets the full configuration of one WiFi broadcast profile (SSID) by id,
including the security mode, the linked network, and band settings. The WiFi
passphrase (PSK) is redacted and never returned by this tool. Obtain ids from
list_wifi. Returns a structured not_found error when no broadcast has the
given id. The result includes a state_hash of the object; write tools (if
enabled) require it as expected_state_hash to prevent stale writes. This
tool is read-only.
"""

UPDATE_WIFI_DESCRIPTION = """\
Updates the name and/or enabled state of one WiFi broadcast profile (SSID).
This tool modifies the live network configuration: setting enabled=false
disconnects every client currently associated with the SSID. Obtain
broadcast_id from list_wifi and expected_state_hash from a recent get_wifi —
the object is re-fetched and re-hashed before the write, so a stale hash
fails with state_changed. At least one of name (new SSID name, non-empty,
max 32 characters) or enabled is required. Only user-created profiles
(metadata.origin USER_DEFINED) can be modified. The result is the updated
profile with a fresh state_hash. The WiFi passphrase is never changed and
never returned by this tool.
"""


def register_wifi_tools(server: MCPServer, client: UniFiClient, settings: Settings) -> None:
    async def list_wifi(
        site_id: str | None = None,
        limit: int | None = None,
        offset: int | None = None,
    ) -> dict[str, Any]:
        site = await resolve_site(client, settings, site_id)
        params = clamp_params(limit, offset, max_limit=settings.max_list_items)
        return await fetch_list(client, f"/sites/{site}/wifi/broadcasts", params=params)

    async def get_wifi(broadcast_id: str, site_id: str | None = None) -> dict[str, Any]:
        site = await resolve_site(client, settings, site_id)
        try:
            data = await client.get(f"/sites/{site}/wifi/broadcasts/{broadcast_id}")
        except UniFiNotFoundError:
            raise NotFoundError(resource="wifi_broadcast", query=broadcast_id) from None
        return with_state_hash(normalize(data, level="detail"))

    server.add_tool(
        wrap_tool(
            "list_wifi",
            settings.max_tool_response_bytes,
            list_wifi,
            budget_seconds=settings.unifi_tool_call_budget_seconds,
        ),
        name="list_wifi",
        title="List WiFi",
        description=LIST_WIFI_DESCRIPTION,
        annotations=ToolAnnotations(read_only_hint=True),
    )
    server.add_tool(
        wrap_tool(
            "get_wifi",
            settings.max_tool_response_bytes,
            get_wifi,
            budget_seconds=settings.unifi_tool_call_budget_seconds,
        ),
        name="get_wifi",
        title="Get WiFi",
        description=GET_WIFI_DESCRIPTION,
        annotations=ToolAnnotations(read_only_hint=True),
    )


def register_wifi_write_tools(server: MCPServer, client: UniFiClient, settings: Settings) -> None:
    """Register the WiFi write tools (gated by ``enable_write_tools``)."""

    async def update_wifi(
        broadcast_id: str,
        expected_state_hash: str,
        name: str | None = None,
        enabled: bool | None = None,
        site_id: str | None = None,
    ) -> dict[str, Any]:
        site = await resolve_site(client, settings, site_id)
        changes: dict[str, Any] = {}
        if name is not None:
            trimmed = name.strip()
            if not trimmed:
                raise InvalidValueError(
                    "name", value=name, allowed=["non-empty SSID name (max 32 characters)"]
                )
            if len(trimmed) > 32:
                raise InvalidValueError(
                    "name", value=f"{len(trimmed)} characters", allowed=["max 32 characters"]
                )
            changes["name"] = trimmed
        if enabled is not None:
            changes["enabled"] = enabled
        if not changes:
            raise InvalidValueError(
                "changes",
                value="<none>",
                allowed=["name", "enabled"],
                message="Provide at least one field to change: name or enabled.",
            )
        try:
            return await guarded_update(
                client,
                path=f"/sites/{site}/wifi/broadcasts/{broadcast_id}",
                object_id=broadcast_id,
                resource="wifi_broadcast",
                expected_state_hash=expected_state_hash,
                changes=changes,
                allowed_fields=frozenset({"name", "enabled"}),
                tool="update_wifi",
                site_id=site,
            )
        except UniFiNotFoundError:
            raise NotFoundError(resource="wifi_broadcast", query=broadcast_id) from None

    server.add_tool(
        wrap_tool(
            "update_wifi",
            settings.max_tool_response_bytes,
            update_wifi,
            write=True,
            budget_seconds=settings.unifi_tool_call_budget_seconds,
        ),
        name="update_wifi",
        title="Update WiFi",
        description=UPDATE_WIFI_DESCRIPTION,
        annotations=ToolAnnotations(read_only_hint=False),
    )
