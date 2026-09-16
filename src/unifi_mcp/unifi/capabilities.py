"""Startup capability check against the UniFi gateway (design doc section 3).

The core connection (``GET /info``) is a hard requirement: if it fails the
server must not start. Optional categories may be missing (endpoint not
supported in this API version, feature not configured on the gateway,
gateway unreachable at that moment) — the server still starts and reports
their status, so tools can answer ``unsupported`` instead of a raw HTTP 400.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Literal

from unifi_mcp.observability.logging import get_logger
from unifi_mcp.unifi.client import UniFiClient
from unifi_mcp.unifi.errors import (
    UniFiAuthenticationError,
    UniFiAuthorizationError,
    UniFiError,
    UniFiNotFoundError,
    UniFiUnavailableError,
    UniFiValidationError,
)
from unifi_mcp.unifi.models import ApplicationInfo

logger = get_logger(__name__)

CapabilityState = Literal["ok", "not_configured", "unavailable", "error"]

# category -> probe path template (relative to /proxy/network/integration/v1)
_SITE_SCOPED_PROBES: tuple[tuple[str, str], ...] = (
    ("devices", "/sites/{site}/devices"),
    ("clients", "/sites/{site}/clients"),
    ("networks", "/sites/{site}/networks"),
    ("wifi", "/sites/{site}/wifi/broadcasts"),
    ("firewall", "/sites/{site}/firewall/zones"),
    ("acl", "/sites/{site}/acl-rules"),
    ("traffic_matching_lists", "/sites/{site}/traffic-matching-lists"),
)


@dataclass(frozen=True)
class Capability:
    """Probe result for one category."""

    status: CapabilityState
    detail: str | None = None


@dataclass(frozen=True)
class Capabilities:
    """Startup probe results, cached for the process lifetime."""

    application_info: ApplicationInfo
    categories: dict[str, Capability] = field(default_factory=dict)

    def is_available(self, name: str) -> bool:
        cap = self.categories.get(name)
        return cap is not None and cap.status == "ok"

    def to_dict(self) -> dict[str, Any]:
        return {
            "application_version": self.application_info.application_version,
            "capabilities": {
                name: {"status": cap.status, "detail": cap.detail}
                for name, cap in sorted(self.categories.items())
            },
        }


def _probe_capability(exc: UniFiError) -> Capability:
    if isinstance(exc, UniFiNotFoundError):
        return Capability("unavailable", f"endpoint not found (HTTP {exc.status_code})")
    if isinstance(exc, UniFiValidationError):
        if exc.api_code is not None and "not-configured" in exc.api_code:
            return Capability("not_configured", exc.api_code)
        return Capability("error", f"HTTP {exc.status_code}: {exc.api_code or exc.message}")
    if isinstance(exc, (UniFiAuthenticationError, UniFiAuthorizationError)):
        return Capability("unavailable", f"auth failure (HTTP {exc.status_code})")
    if isinstance(exc, UniFiUnavailableError):
        return Capability("unavailable", exc.message)
    return Capability("error", exc.message)


async def _probe(
    client: UniFiClient,
    categories: dict[str, Capability],
    name: str,
    path: str,
) -> None:
    try:
        await client.get_list(path, params={"limit": 1})
        categories[name] = Capability("ok")
    except UniFiError as exc:
        categories[name] = _probe_capability(exc)


async def detect_capabilities(client: UniFiClient) -> Capabilities:
    """Probe the gateway and return per-category availability.

    Raises ``UniFiError`` if the core connection (``GET /info``) fails.
    """
    application_info = await client.get_application_info()
    categories: dict[str, Capability] = {}

    site_id = client.site_id
    try:
        sites_page = await client.get_list("/sites", params={"limit": 1})
        categories["sites"] = Capability("ok")
        if site_id is None and sites_page.data:
            # No explicit site configured: probe site-scoped categories
            # against the first site the gateway reports.
            first = sites_page.data[0]
            if isinstance(first, dict):
                first_id = first.get("id")
                if isinstance(first_id, str):
                    site_id = first_id
    except UniFiError as exc:
        categories["sites"] = _probe_capability(exc)

    for name, template in _SITE_SCOPED_PROBES:
        if site_id is None:
            categories[name] = Capability(
                "unavailable",
                "no site available to probe (sites category failed or no sites)",
            )
            continue
        await _probe(client, categories, name, template.format(site=site_id))

    for name, cap in sorted(categories.items()):
        if cap.status == "ok":
            logger.info("capability ok", category=name)
        else:
            logger.warning(
                "capability degraded",
                category=name,
                status=cap.status,
                detail=cap.detail,
            )
    return Capabilities(application_info=application_info, categories=categories)
