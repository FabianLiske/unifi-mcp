"""Network / VLAN tools (design §10.5).

Endpoints (docs/unifi-api-notes.md §5, live-verified):

- list:   ``/sites/{site}/networks``
- detail: ``/sites/{site}/networks/{id}`` (adds the full ``ipv4Configuration``
  with ``dhcpConfiguration.*``)

List items are exposed at the ``summary`` normalization level (nested
containers pruned); the detail tool uses ``detail`` (complete, secrets
redacted, internal fields stripped).
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

LIST_NETWORKS_DESCRIPTION = """\
Lists the networks (VLANs) configured for the site, with name, VLAN id,
enabled state, and management purpose. Each item is a summary: the nested
IPv4/DHCP configuration block is collapsed — use get_network for the full
configuration of one network. Returns at most 'limit' networks (default 50,
max 200); when next_offset is set, call again with offset=next_offset to
page. This tool is read-only.
"""

GET_NETWORK_DESCRIPTION = """\
Gets the full configuration of one network (VLAN) by id, including the IPv4
and DHCP settings (host address, prefix length, DHCP mode and address range)
and per-network options such as isolation. Obtain ids from list_networks.
Returns a structured not_found error when no network has the given id.
This tool is read-only.
"""


def register_network_tools(server: MCPServer, client: UniFiClient, settings: Settings) -> None:
    async def list_networks(
        site_id: str | None = None,
        limit: int | None = None,
        offset: int | None = None,
    ) -> dict[str, Any]:
        site = await resolve_site(client, settings, site_id)
        params = clamp_params(limit, offset, max_limit=settings.max_list_items)
        return await fetch_list(client, f"/sites/{site}/networks", params=params)

    async def get_network(network_id: str, site_id: str | None = None) -> dict[str, Any]:
        site = await resolve_site(client, settings, site_id)
        try:
            data = await client.get(f"/sites/{site}/networks/{network_id}")
        except UniFiNotFoundError:
            raise NotFoundError(resource="network", query=network_id) from None
        normalized: dict[str, Any] = normalize(data, level="detail")
        return normalized

    server.add_tool(
        wrap_tool("list_networks", settings.max_tool_response_bytes, list_networks),
        name="list_networks",
        title="List Networks",
        description=LIST_NETWORKS_DESCRIPTION,
        annotations=ToolAnnotations(read_only_hint=True),
    )
    server.add_tool(
        wrap_tool("get_network", settings.max_tool_response_bytes, get_network),
        name="get_network",
        title="Get Network",
        description=GET_NETWORK_DESCRIPTION,
        annotations=ToolAnnotations(read_only_hint=True),
    )
