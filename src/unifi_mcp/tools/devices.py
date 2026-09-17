"""Device tools (design §10.3).

The filter DSL is function-based and URL-encoded (docs/unifi-api-notes.md
§3, live-verified in WP-2 and WP-7): ``field.fn('arg')``, composed with
``and(...)``. Enum values are UPPERCASE on the wire; device ``device_type``
maps to a ``features.contains(...)`` clause.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from mcp_types import ToolAnnotations

from unifi_mcp.tools.common import fetch_list, resolve_site, wrap_tool
from unifi_mcp.tools.errors import InvalidValueError
from unifi_mcp.unifi.normalization import clamp_limit

if TYPE_CHECKING:
    from mcp.server.mcpserver import MCPServer

    from unifi_mcp.config import Settings
    from unifi_mcp.unifi.client import UniFiClient

DESCRIPTION = """\
Lists adopted UniFi Network devices such as gateways, switches, and access
points, with state, model, firmware, and IP address. Filters: site_id,
device_type (switch | ap | gateway), state (online | offline | pending),
search (substring match on the device name); combine them freely. Returns
at most 'limit' devices (default 50, max 200); when next_offset is set,
call again with offset=next_offset to page. Note: devices report only the
features they actually provide, so 'gateway' matches not every gateway
model (e.g. some gateways report 'switch' instead). This tool is read-only.
"""

#: user-friendly ``device_type`` value -> UniFi ``features`` entry (live-verified).
_DEVICE_TYPE_API: dict[str, str] = {
    "switch": "switching",
    "switching": "switching",
    "ap": "accessPoint",
    "access_point": "accessPoint",
    "accesspoint": "accessPoint",
    "gateway": "gateway",
}

#: user-friendly ``state`` value -> UniFi ``state`` enum (UPPERCASE, live-verified).
_STATE_API: dict[str, str] = {
    "online": "ONLINE",
    "offline": "OFFLINE",
    "pending": "PENDING",
}

ALLOWED_DEVICE_TYPES = ("switch", "ap", "gateway")
ALLOWED_STATES = ("online", "offline", "pending")


def _device_type_feature(value: str) -> str:
    key = value.strip().lower()
    if key not in _DEVICE_TYPE_API:
        raise InvalidValueError("device_type", value, list(ALLOWED_DEVICE_TYPES))
    return _DEVICE_TYPE_API[key]


def _state_enum(value: str) -> str:
    key = value.strip().lower()
    if key not in _STATE_API:
        raise InvalidValueError("state", value, list(ALLOWED_STATES))
    return _STATE_API[key]


def build_device_filter(
    *,
    device_type: str | None = None,
    state: str | None = None,
    search: str | None = None,
) -> str | None:
    """Build the function-based UniFi filter expression for list_devices.

    Returns ``None`` when no clause applies. Argument values are validated
    against allowlists (no free-form filter passthrough); the search term is
    embedded into ``name.like('*…*')`` (single quotes stripped, the DSL has
    no quote escaping) and ignored when it is empty.
    """
    clauses: list[str] = []
    if device_type is not None and device_type.strip():
        clauses.append(f"features.contains('{_device_type_feature(device_type)}')")
    if state is not None and state.strip():
        clauses.append(f"state.eq('{_state_enum(state)}')")
    if search is not None:
        term = search.strip().replace("'", "")
        if term:
            clauses.append(f"name.like('*{term}*')")
    if not clauses:
        return None
    if len(clauses) == 1:
        return clauses[0]
    return f"and({','.join(clauses)})"


def clamp_params(
    limit: int | None,
    offset: int | None,
    *,
    max_limit: int,
) -> dict[str, Any]:
    """Clamp pagination parameters into a ready-to-send params dict.

    ``limit`` follows design §12 (default 50, hard max *max_limit*); a
    negative ``offset`` is a caller error.
    """
    if offset is not None and offset < 0:
        raise InvalidValueError("offset", str(offset), ["offset >= 0"])
    params: dict[str, Any] = {
        "limit": min(clamp_limit(limit, max_limit=max_limit), max_limit),
        "offset": offset or 0,
    }
    return params


def register_device_tools(server: MCPServer, client: UniFiClient, settings: Settings) -> None:
    async def list_devices(
        site_id: str | None = None,
        device_type: str | None = None,
        state: str | None = None,
        search: str | None = None,
        limit: int | None = None,
        offset: int | None = None,
    ) -> dict[str, Any]:
        site = await resolve_site(client, settings, site_id)
        params = clamp_params(limit, offset, max_limit=settings.max_list_items)
        filter_expr = build_device_filter(device_type=device_type, state=state, search=search)
        if filter_expr is not None:
            params["filter"] = filter_expr
        return await fetch_list(client, f"/sites/{site}/devices", params=params)

    server.add_tool(
        wrap_tool("list_devices", settings.max_tool_response_bytes, list_devices),
        name="list_devices",
        title="List Devices",
        description=DESCRIPTION,
        annotations=ToolAnnotations(read_only_hint=True),
    )
