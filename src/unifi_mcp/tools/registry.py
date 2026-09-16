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


#: The tool groups registered by the server.
#:
#: Empty in the WP-6 skeleton — the read-only tool groups (``get_system_info``,
#: ``list_sites``, ``list_devices``, …) are added here from WP-7 onward.
TOOL_GROUPS: tuple[ToolGroup, ...] = ()
