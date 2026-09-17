"""MCP tool implementations (semantic, filtered, read-only in the MVP)."""

from unifi_mcp.tools.common import (
    fetch_list,
    resolve_site,
    site_overview,
    unifi_error_to_dict,
    wrap_tool,
)
from unifi_mcp.tools.errors import (
    AmbiguousMatchError,
    InvalidValueError,
    NotFoundError,
    ResponseTooLargeError,
    ToolError,
    check_response_size,
)
from unifi_mcp.tools.registry import TOOL_GROUPS, ToolGroup, always_on, register_tools

__all__ = [
    "AmbiguousMatchError",
    "InvalidValueError",
    "NotFoundError",
    "ResponseTooLargeError",
    "TOOL_GROUPS",
    "ToolError",
    "ToolGroup",
    "always_on",
    "check_response_size",
    "fetch_list",
    "register_tools",
    "resolve_site",
    "site_overview",
    "unifi_error_to_dict",
    "wrap_tool",
]
