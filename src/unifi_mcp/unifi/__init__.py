"""Client and models for the official local UniFi Network API."""

from unifi_mcp.unifi.capabilities import Capabilities, Capability, detect_capabilities
from unifi_mcp.unifi.client import UniFiClient
from unifi_mcp.unifi.errors import (
    UniFiAuthenticationError,
    UniFiAuthorizationError,
    UniFiConflictError,
    UniFiError,
    UniFiNotFoundError,
    UniFiRateLimitError,
    UniFiResponseTooLargeError,
    UniFiUnavailableError,
    UniFiValidationError,
)
from unifi_mcp.unifi.models import ApplicationInfo, Page
from unifi_mcp.unifi.normalization import (
    DEFAULT_LIMIT,
    ELLIPSIS,
    INTERNAL_FIELDS,
    MAX_LIMIT,
    clamp_limit,
    normalize,
    page_to_mcp,
)

__all__ = [
    "DEFAULT_LIMIT",
    "ELLIPSIS",
    "INTERNAL_FIELDS",
    "MAX_LIMIT",
    "ApplicationInfo",
    "Capability",
    "Capabilities",
    "Page",
    "UniFiAuthenticationError",
    "UniFiAuthorizationError",
    "UniFiClient",
    "UniFiConflictError",
    "UniFiError",
    "UniFiNotFoundError",
    "UniFiRateLimitError",
    "UniFiResponseTooLargeError",
    "UniFiUnavailableError",
    "UniFiValidationError",
    "clamp_limit",
    "detect_capabilities",
    "normalize",
    "page_to_mcp",
]
