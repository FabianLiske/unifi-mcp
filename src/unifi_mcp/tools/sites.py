"""Site tools (design §10.2)."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from mcp_types import ToolAnnotations

from unifi_mcp.tools.common import fetch_list, wrap_tool

if TYPE_CHECKING:
    from mcp.server.mcpserver import MCPServer

    from unifi_mcp.config import Settings
    from unifi_mcp.unifi.client import UniFiClient

DESCRIPTION = """\
Lists all UniFi Network sites known to the gateway with their id and name.
Use the returned site_id values with site-scoped tools such as list_devices.
This tool is read-only.
"""


def register_site_tools(server: MCPServer, client: UniFiClient, settings: Settings) -> None:
    async def list_sites() -> dict[str, Any]:
        payload = await fetch_list(
            client,
            "/sites",
            params={"limit": settings.max_list_items},
        )
        # Design §10.2: site items expose id and name only.
        payload["items"] = [
            {"id": item["id"], "name": item.get("name")}
            for item in payload["items"]
            if isinstance(item, dict) and isinstance(item.get("id"), str)
        ]
        return payload

    server.add_tool(
        wrap_tool("list_sites", settings.max_tool_response_bytes, list_sites),
        name="list_sites",
        title="List Sites",
        description=DESCRIPTION,
        annotations=ToolAnnotations(read_only_hint=True),
    )
