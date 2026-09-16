"""Unit tests for unifi_mcp.tools.registry (feature-flag gating)."""

from __future__ import annotations

from mcp.server.mcpserver import MCPServer

from unifi_mcp.tools.registry import ToolGroup, always_on, register_tools


def _gated_group() -> ToolGroup:
    def gate(settings) -> bool:
        return settings.enable_write_tools

    def register(server, client, settings) -> None:
        async def sample_write_tool() -> str:
            return "hello"

        server.add_tool(
            sample_write_tool, name="sample_write_tool", description="Gated sample tool."
        )

    return ToolGroup(name="sample_write", gate=gate, register=register)


async def test_gate_off_registers_nothing(make_settings) -> None:
    settings = make_settings(enable_write_tools=False)
    server = MCPServer(name="t")
    register_tools(server, object(), settings, groups=(_gated_group(),))
    assert [tool.name for tool in await server.list_tools()] == []


async def test_gate_on_registers_tool(make_settings) -> None:
    settings = make_settings(enable_write_tools=True)
    server = MCPServer(name="t")
    register_tools(server, object(), settings, groups=(_gated_group(),))
    assert [tool.name for tool in await server.list_tools()] == ["sample_write_tool"]


def test_groups_default_to_registry() -> None:
    # With no groups argument the (empty) TOOL_GROUPS is used.
    assert always_on(None) is True  # type: ignore[arg-type]
