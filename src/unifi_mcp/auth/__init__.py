"""Authentication and request authorization for the MCP server."""

from unifi_mcp.auth.middleware import (
    PROTECTED_PREFIXES,
    BearerAuthMiddleware,
    extract_bearer_token,
)

__all__ = [
    "BearerAuthMiddleware",
    "PROTECTED_PREFIXES",
    "extract_bearer_token",
]
