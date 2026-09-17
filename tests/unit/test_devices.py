"""Unit tests for unifi_mcp.tools.devices (filter builder, registration)."""

from __future__ import annotations

import pytest
from mcp.server.mcpserver import MCPServer

from unifi_mcp.tools.devices import (
    ALLOWED_DEVICE_TYPES,
    ALLOWED_STATES,
    build_device_filter,
    clamp_params,
    register_device_tools,
)
from unifi_mcp.tools.errors import InvalidValueError

# --- build_device_filter -------------------------------------------------------


def test_no_filters_returns_none() -> None:
    assert build_device_filter() is None
    assert build_device_filter(device_type="", state="  ", search="") is None


def test_single_clauses() -> None:
    assert build_device_filter(device_type="switch") == "features.contains('switching')"
    assert build_device_filter(state="online") == "state.eq('ONLINE')"
    assert build_device_filter(search="USW") == "name.like('*USW*')"


def test_device_type_aliases() -> None:
    assert build_device_filter(device_type="ap") == "features.contains('accessPoint')"
    assert build_device_filter(device_type="access_point") == "features.contains('accessPoint')"
    assert build_device_filter(device_type="accesspoint") == "features.contains('accessPoint')"
    assert build_device_filter(device_type="GATEWAY") == "features.contains('gateway')"
    # raw API values pass through
    assert build_device_filter(device_type="switching") == "features.contains('switching')"


def test_state_case_insensitive() -> None:
    assert build_device_filter(state="Online") == "state.eq('ONLINE')"
    assert build_device_filter(state="OFFLINE") == "state.eq('OFFLINE')"
    assert build_device_filter(state="pending") == "state.eq('PENDING')"


def test_combined_filter_uses_and() -> None:
    expr = build_device_filter(device_type="switch", state="online", search="USW")
    assert expr == "and(features.contains('switching'),state.eq('ONLINE'),name.like('*USW*'))"


def test_search_strips_quotes_and_surrounding_whitespace() -> None:
    assert build_device_filter(search="  USW Pro ") == "name.like('*USW Pro*')"
    assert build_device_filter(search="o'brien") == "name.like('*obrien*')"
    assert build_device_filter(search="''") is None


@pytest.mark.parametrize("value", ["router", "firewall"])
def test_invalid_device_type_rejected(value: str) -> None:
    with pytest.raises(InvalidValueError) as excinfo:
        build_device_filter(device_type=value)
    assert excinfo.value.code == "validation"
    assert list(ALLOWED_DEVICE_TYPES) == excinfo.value.fields["allowed"]


@pytest.mark.parametrize("value", ["online-only", "unknown"])
def test_invalid_state_rejected(value: str) -> None:
    with pytest.raises(InvalidValueError) as excinfo:
        build_device_filter(state=value)
    assert excinfo.value.code == "validation"
    assert list(ALLOWED_STATES) == excinfo.value.fields["allowed"]


# --- clamp_params ---------------------------------------------------------------


def test_clamp_params_defaults_and_caps() -> None:
    assert clamp_params(None, None, max_limit=200) == {"limit": 50, "offset": 0}
    assert clamp_params(1000, None, max_limit=200) == {"limit": 200, "offset": 0}
    assert clamp_params(0, 7, max_limit=200) == {"limit": 50, "offset": 7}


def test_clamp_params_negative_offset_rejected() -> None:
    with pytest.raises(InvalidValueError):
        clamp_params(None, -1, max_limit=200)


# --- registration ----------------------------------------------------------------


async def test_register_device_tools_registers_list_devices(make_settings) -> None:
    settings = make_settings()
    server = MCPServer(name="t")
    register_device_tools(server, object(), settings)  # type: ignore[arg-type]
    tools = await server.list_tools()
    names = [tool.name for tool in tools]
    assert names == ["list_devices"]
    tool = tools[0]
    assert tool.annotations is not None and tool.annotations.read_only_hint is True
    params = tool.input_schema["properties"]
    assert set(params) == {"site_id", "device_type", "state", "search", "limit", "offset"}
