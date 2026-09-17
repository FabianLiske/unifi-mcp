"""Reference resource tools (design §10.10).

One tool bundles the rarely used UniFi lookup tables instead of one MCP
tool per table. The eight ``resource_type`` values are the snake_case names
from design §10.10. Endpoint paths are live-verified: five types are
site-scoped (path built from the resolved site), the DPI tables and
``countries`` are top-level (``site_id`` is ignored for them).
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
Lists rarely used UniFi lookup tables in one tool. resource_type selects
the table (snake_case): countries (top-level, ISO country codes),
dpi_applications and dpi_categories (top-level DPI database), and the
site-scoped device_tags, radius_profiles, site_to_site_vpn_tunnels,
vpn_servers, wan_interfaces. For site-scoped types, site_id selects the
site (default: the configured site); top-level types ignore site_id.
Returns at most 'limit' items (default 50, max 200); when next_offset is
set, call again with offset=next_offset to page. This tool is read-only.
"""

#: resource_type -> (path template, site-scoped?). Live-verified 2026-09-17.
_REFERENCE_RESOURCES: dict[str, tuple[str, bool]] = {
    "wan_interfaces": ("/sites/{site}/wans", True),
    "site_to_site_vpn_tunnels": ("/sites/{site}/vpn/site-to-site-tunnels", True),
    "vpn_servers": ("/sites/{site}/vpn/servers", True),
    "radius_profiles": ("/sites/{site}/radius/profiles", True),
    "device_tags": ("/sites/{site}/device-tags", True),
    "dpi_categories": ("/dpi/categories", False),
    "dpi_applications": ("/dpi/applications", False),
    "countries": ("/countries", False),
}

ALLOWED_RESOURCE_TYPES: tuple[str, ...] = tuple(sorted(_REFERENCE_RESOURCES))

SITE_SCOPED_RESOURCE_TYPES: frozenset[str] = frozenset(
    name for name, (_, site_scoped) in _REFERENCE_RESOURCES.items() if site_scoped
)


def lookup_resource(resource_type: str) -> tuple[str, bool]:
    """Return ``(path template, site-scoped?)`` for *resource_type*.

    Raises :class:`InvalidValueError` (code ``validation``) for unknown
    types, listing all allowed types.
    """
    try:
        return _REFERENCE_RESOURCES[resource_type]
    except KeyError:
        raise InvalidValueError(
            "resource_type", resource_type, list(ALLOWED_RESOURCE_TYPES)
        ) from None


def build_reference_path(resource_type: str, site: str | None = None) -> str:
    """Map *resource_type* to its API path (pure, unit-testable).

    Top-level types ignore *site*; site-scoped types require it. Unknown
    types raise :class:`InvalidValueError`.
    """
    template, site_scoped = lookup_resource(resource_type)
    if not site_scoped:
        return template
    if not site:
        raise InvalidValueError("site_id", "<none>", ["site_id or configured UNIFI_SITE_ID"])
    return template.format(site=site)


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


def register_reference_tools(server: MCPServer, client: UniFiClient, settings: Settings) -> None:
    async def list_reference_resources(
        resource_type: str,
        site_id: str | None = None,
        limit: int | None = None,
        offset: int | None = None,
    ) -> dict[str, Any]:
        _, site_scoped = lookup_resource(resource_type)
        site = await resolve_site(client, settings, site_id) if site_scoped else None
        path = build_reference_path(resource_type, site)
        params = clamp_params(limit, offset, max_limit=settings.max_list_items)
        return await fetch_list(client, path, params=params)

    server.add_tool(
        wrap_tool(
            "list_reference_resources", settings.max_tool_response_bytes, list_reference_resources
        ),
        name="list_reference_resources",
        title="List Reference Resources",
        description=DESCRIPTION,
        annotations=ToolAnnotations(read_only_hint=True),
    )
