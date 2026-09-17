"""System/capability tool (design §10.1)."""

from __future__ import annotations

from importlib.metadata import PackageNotFoundError
from importlib.metadata import version as _package_version
from typing import TYPE_CHECKING, Any

from mcp_types import ToolAnnotations

from unifi_mcp.tools.common import site_overview, wrap_tool

if TYPE_CHECKING:
    from mcp.server.mcpserver import MCPServer

    from unifi_mcp.config import Settings
    from unifi_mcp.unifi.client import UniFiClient

DESCRIPTION = """\
Reports the UniFi Network application version of the connected gateway, the
active site, this MCP server's version, and the availability of each API
capability category (for example "firewall: not_configured" when zone-based
firewall is not set up on the gateway). Returns no secrets. Call this first
to orient yourself before using other tools. This tool is read-only.
"""


def _mcp_version() -> str:
    try:
        return _package_version("unifi-mcp")
    except PackageNotFoundError:  # pragma: no cover - dev checkouts
        return ""


def register_system_tools(server: MCPServer, client: UniFiClient, settings: Settings) -> None:
    async def get_system_info() -> dict[str, Any]:
        info = await client.get_application_info()
        site = await site_overview(client, settings)
        caps = await client.capability_cache.get()
        return {
            "application_version": info.application_version,
            "mcp_version": _mcp_version(),
            "active_site": site,
            "capabilities": caps.to_dict()["capabilities"],
        }

    server.add_tool(
        wrap_tool("get_system_info", settings.max_tool_response_bytes, get_system_info),
        name="get_system_info",
        title="System Info",
        description=DESCRIPTION,
        annotations=ToolAnnotations(read_only_hint=True),
    )
