"""Zone-based firewall zone tools (design §10.7).

Endpoints (docs/unifi-api-notes.md §5/§6, live-verified):

- list:   ``/sites/{site}/firewall/zones``
- detail: ``/sites/{site}/firewall/zones/{id}``

On a gateway where the zone-based firewall is not configured, both endpoints
answer HTTP 400 with api_code ``api.firewall.zone-based-firewall-not-configured``.
The shared error mapping in :func:`~unifi_mcp.tools.common.wrap_tool` turns
that into a structured ``unsupported`` result — a gateway configuration state,
not a request error (design §3/§40).
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from mcp_types import ToolAnnotations

from unifi_mcp.tools.common import fetch_list, resolve_site, wrap_tool
from unifi_mcp.tools.devices import clamp_params
from unifi_mcp.tools.errors import NotFoundError
from unifi_mcp.unifi.errors import UniFiNotFoundError
from unifi_mcp.unifi.normalization import normalize

if TYPE_CHECKING:
    from mcp.server.mcpserver import MCPServer

    from unifi_mcp.config import Settings
    from unifi_mcp.unifi.client import UniFiClient

LIST_FIREWALL_ZONES_DESCRIPTION = """\
Lists the zone-based firewall zones configured for the site. When the gateway
has not set up the zone-based firewall, returns a structured 'unsupported'
error (a gateway configuration state, not a request error — configure the
zone-based firewall on the gateway first). Returns at most 'limit' zones
(default 50, max 200); when next_offset is set, call again with
offset=next_offset to page. This tool is read-only.
"""

GET_FIREWALL_ZONE_DESCRIPTION = """\
Gets one zone-based firewall zone by id. When the gateway has not set up the
zone-based firewall, returns a structured 'unsupported' error; when no zone
has the given id, a structured not_found error. Obtain ids from
list_firewall_zones. This tool is read-only.
"""


def register_firewall_tools(server: MCPServer, client: UniFiClient, settings: Settings) -> None:
    async def list_firewall_zones(
        site_id: str | None = None,
        limit: int | None = None,
        offset: int | None = None,
    ) -> dict[str, Any]:
        site = await resolve_site(client, settings, site_id)
        params = clamp_params(limit, offset, max_limit=settings.max_list_items)
        return await fetch_list(client, f"/sites/{site}/firewall/zones", params=params)

    async def get_firewall_zone(zone_id: str, site_id: str | None = None) -> dict[str, Any]:
        site = await resolve_site(client, settings, site_id)
        try:
            data = await client.get(f"/sites/{site}/firewall/zones/{zone_id}")
        except UniFiNotFoundError:
            raise NotFoundError(resource="firewall_zone", query=zone_id) from None
        normalized: dict[str, Any] = normalize(data, level="detail")
        return normalized

    server.add_tool(
        wrap_tool("list_firewall_zones", settings.max_tool_response_bytes, list_firewall_zones),
        name="list_firewall_zones",
        title="List Firewall Zones",
        description=LIST_FIREWALL_ZONES_DESCRIPTION,
        annotations=ToolAnnotations(read_only_hint=True),
    )
    server.add_tool(
        wrap_tool("get_firewall_zone", settings.max_tool_response_bytes, get_firewall_zone),
        name="get_firewall_zone",
        title="Get Firewall Zone",
        description=GET_FIREWALL_ZONE_DESCRIPTION,
        annotations=ToolAnnotations(read_only_hint=True),
    )
