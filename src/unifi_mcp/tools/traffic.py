"""Traffic matching list tools (design §10.9).

Traffic matching lists (TMLs) are named collections of IPv4
addresses/subnets or port numbers that ACL rules and other firewall
features can reference. Shape (live-verified): ``{type:
IPV4_ADDRESSES | PORTS, id, name, items: [{type, value}, ...]}``.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, cast

from mcp_types import ToolAnnotations

from unifi_mcp.tools.common import fetch_list, resolve_site, wrap_tool
from unifi_mcp.tools.errors import InvalidValueError, NotFoundError
from unifi_mcp.unifi.errors import UniFiNotFoundError
from unifi_mcp.unifi.normalization import clamp_limit, normalize

if TYPE_CHECKING:
    from mcp.server.mcpserver import MCPServer

    from unifi_mcp.config import Settings
    from unifi_mcp.unifi.client import UniFiClient

DESCRIPTION_LIST = """\
Lists traffic matching lists (TMLs) defined on a UniFi Network site: named
collections of IPv4 addresses/subnets or port numbers that ACL rules and
other firewall features can reference. Each item shows its type
(IPV4_ADDRESSES | PORTS) and its entries (entry details are elided in the
list; use get_traffic_matching_list for the full list). Returns at most
'limit' lists (default 50, max 200); when next_offset is set, call again
with offset=next_offset to page. This tool is read-only.
"""

DESCRIPTION_GET = """\
Returns one traffic matching list by the id returned by
list_traffic_matching_lists, including all of its entries (subnets, IP
addresses, or port numbers). Pass site_id to look in another site. Unknown
ids return a not_found error. This tool is read-only.
"""


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


async def _get_tml_detail(client: UniFiClient, path: str, tml_id: str) -> dict[str, Any]:
    """Detail-GET one traffic matching list; 404 becomes a structured NotFoundError (§30)."""
    try:
        data = await client.get(path)
    except UniFiNotFoundError as exc:
        raise NotFoundError(resource="traffic_matching_list", query=tml_id) from exc
    return cast("dict[str, Any]", normalize(data, level="detail"))


def register_traffic_tools(server: MCPServer, client: UniFiClient, settings: Settings) -> None:
    async def list_traffic_matching_lists(
        site_id: str | None = None,
        limit: int | None = None,
        offset: int | None = None,
    ) -> dict[str, Any]:
        site = await resolve_site(client, settings, site_id)
        params = clamp_params(limit, offset, max_limit=settings.max_list_items)
        return await fetch_list(client, f"/sites/{site}/traffic-matching-lists", params=params)

    async def get_traffic_matching_list(tml_id: str, site_id: str | None = None) -> dict[str, Any]:
        site = await resolve_site(client, settings, site_id)
        path = f"/sites/{site}/traffic-matching-lists/{tml_id}"
        return await _get_tml_detail(client, path, tml_id)

    server.add_tool(
        wrap_tool(
            "list_traffic_matching_lists",
            settings.max_tool_response_bytes,
            list_traffic_matching_lists,
        ),
        name="list_traffic_matching_lists",
        title="List Traffic Matching Lists",
        description=DESCRIPTION_LIST,
        annotations=ToolAnnotations(read_only_hint=True),
    )
    server.add_tool(
        wrap_tool(
            "get_traffic_matching_list", settings.max_tool_response_bytes, get_traffic_matching_list
        ),
        name="get_traffic_matching_list",
        title="Get Traffic Matching List",
        description=DESCRIPTION_GET,
        annotations=ToolAnnotations(read_only_hint=True),
    )
