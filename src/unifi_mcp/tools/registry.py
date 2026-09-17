"""Feature-flag-driven MCP tool registration (design doc §37/§49).

Tools are registered in *groups*. Every group has a feature-flag *gate*; when
the gate is off the group's tools are never registered, so they are absent
from ``tools/list``. Gating at registration time is deliberately stronger than
rejecting a call at runtime (design §37).

The MVP read-only groups are always-on; write/action/delete groups are gated
by ``enable_write_tools`` / ``enable_action_tools`` / ``enable_delete_tools``
and must stay absent while disabled.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import TYPE_CHECKING

from unifi_mcp.tools.acl import register_acl_tools
from unifi_mcp.tools.clients import register_client_tools
from unifi_mcp.tools.devices import register_device_tools
from unifi_mcp.tools.firewall import register_firewall_tools
from unifi_mcp.tools.networks import register_network_tools
from unifi_mcp.tools.reference import register_reference_tools
from unifi_mcp.tools.sites import register_site_tools
from unifi_mcp.tools.system import register_system_tools
from unifi_mcp.tools.traffic import register_traffic_tools
from unifi_mcp.tools.wifi import register_wifi_tools

if TYPE_CHECKING:
    from mcp.server.mcpserver import MCPServer

    from unifi_mcp.config import Settings
    from unifi_mcp.unifi.client import UniFiClient

#: Registers one or more tools on the server for a group.
RegisterFn = Callable[["MCPServer", "UniFiClient", "Settings"], None]
#: Feature-flag gate: ``True`` when the group's tools should be registered.
GateFn = Callable[["Settings"], bool]


def always_on(_settings: Settings) -> bool:
    """Gate for always-available (read-only) tool groups."""
    return True


@dataclass(frozen=True)
class ToolGroup:
    """A named set of tools plus the feature-flag gate that enables it."""

    name: str
    gate: GateFn
    register: RegisterFn


def register_tools(
    server: MCPServer,
    client: UniFiClient,
    settings: Settings,
    groups: tuple[ToolGroup, ...] | None = None,
) -> None:
    """Register every tool group whose gate passes for *settings*.

    *groups* defaults to :data:`TOOL_GROUPS`; tests may inject temporary
    groups to exercise the gating without shipping tools.
    """
    for group in TOOL_GROUPS if groups is None else groups:
        if group.gate(settings):
            group.register(server, client, settings)


#: The tool groups registered by the server (read-only MVP, WP-7–10).
#:
#: Write/action/delete groups (gated by their feature flags) are added from
#: WP-13 onward and must stay absent while their flag is off.
TOOL_GROUPS: tuple[ToolGroup, ...] = (
    ToolGroup(name="system", gate=always_on, register=register_system_tools),
    ToolGroup(name="sites", gate=always_on, register=register_site_tools),
    ToolGroup(name="devices", gate=always_on, register=register_device_tools),
    ToolGroup(name="clients", gate=always_on, register=register_client_tools),
    ToolGroup(name="networks", gate=always_on, register=register_network_tools),
    ToolGroup(name="wifi", gate=always_on, register=register_wifi_tools),
    ToolGroup(name="firewall", gate=always_on, register=register_firewall_tools),
    ToolGroup(name="acl", gate=always_on, register=register_acl_tools),
    ToolGroup(name="traffic", gate=always_on, register=register_traffic_tools),
    ToolGroup(name="reference", gate=always_on, register=register_reference_tools),
)
