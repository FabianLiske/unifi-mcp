"""ACL rule tools (design §10.8).

The filter DSL is function-based (docs/unifi-api-notes.md §3); for ACL rules
the following clauses are live-verified against the gateway:
``action.eq('ALLOW'|'BLOCK')``, ``enabled.eq(true|false)`` and the nested
``sourceFilter.type.eq(...)`` / ``destinationFilter.type.eq(...)``. Enum
values are UPPERCASE on the wire and the action enum is ``ALLOW``/``BLOCK``
(there is no ``DENY``); ``deny`` is accepted as a user-friendly alias.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, cast

from mcp_types import ToolAnnotations

from unifi_mcp.tools.common import fetch_list, resolve_site, wrap_tool
from unifi_mcp.tools.errors import InvalidValueError, NotFoundError
from unifi_mcp.unifi.errors import UniFiNotFoundError
from unifi_mcp.unifi.normalization import clamp_limit, normalize

if TYPE_CHECKING:
    from mcp.server.mcpserver import MCPServer

    from unifi_mcp.config import Settings
    from unifi_mcp.unifi.client import UniFiClient

DESCRIPTION_LIST = """\
Lists ACL rules defined on a UniFi Network site (e.g. allow the K8s network
to reach the NAS network), with action, enabled flag, rule index (lower
index = higher priority) and source/destination endpoint filters. Filters:
site_id, action (allow | deny — the gateway's BLOCK action, so 'deny'
matches it), enabled (true | false), source and destination (the kind of
endpoint filter the rule uses: networks | ip_addresses | ports |
mac_addresses, matched against sourceFilter.type / destinationFilter.type).
All filters are applied gateway-side. Returns at most 'limit' rules
(default 50, max 200); when next_offset is set, call again with
offset=next_offset to page. Note: list items summarize the endpoint
filters (ids are elided); use get_acl_rule for the full rule. This tool is
read-only.
"""

DESCRIPTION_GET = """\
Returns one ACL rule by the id returned by list_acl_rules, including the
full source/destination endpoint filters (network ids, IP addresses or
subnets, port ranges). Pass site_id to look in another site. Unknown ids
return a not_found error. This tool is read-only.
"""

#: user-friendly ``action`` value -> UniFi action enum (UPPERCASE, live-verified).
_ACTION_API: dict[str, str] = {"allow": "ALLOW", "deny": "BLOCK", "block": "BLOCK"}

#: user-friendly source/destination endpoint-filter kind -> UniFi enum.
_ENDPOINT_TYPE_API: dict[str, str] = {
    "networks": "NETWORKS",
    "network": "NETWORKS",
    "ip_addresses": "IP_ADDRESSES_OR_SUBNETS",
    "ips": "IP_ADDRESSES_OR_SUBNETS",
    "subnets": "IP_ADDRESSES_OR_SUBNETS",
    "ports": "PORTS",
    "port": "PORTS",
    "mac_addresses": "MAC_ADDRESSES",
    "macs": "MAC_ADDRESSES",
}

ALLOWED_ACTIONS = ("allow", "deny")
ALLOWED_ENDPOINT_TYPES = ("networks", "ip_addresses", "ports", "mac_addresses")


def _action_enum(value: str) -> str:
    key = value.strip().lower()
    if key not in _ACTION_API:
        raise InvalidValueError("action", value, list(ALLOWED_ACTIONS))
    return _ACTION_API[key]


def _endpoint_type_enum(value: str, field: str) -> str:
    key = value.strip().lower()
    if key not in _ENDPOINT_TYPE_API:
        raise InvalidValueError(field, value, list(ALLOWED_ENDPOINT_TYPES))
    return _ENDPOINT_TYPE_API[key]


def build_acl_filter(
    *,
    action: str | None = None,
    enabled: bool | None = None,
    source: str | None = None,
    destination: str | None = None,
) -> str | None:
    """Build the function-based UniFi filter expression for list_acl_rules.

    Returns ``None`` when no clause applies. Argument values are validated
    against allowlists (no free-form filter passthrough); ``enabled`` maps
    to an unquoted boolean literal, and source/destination match on the
    rule's ``sourceFilter.type`` / ``destinationFilter.type`` (live-verified
    nested paths).
    """
    clauses: list[str] = []
    if enabled is not None:
        clauses.append(f"enabled.eq({'true' if enabled else 'false'})")
    if source is not None and source.strip():
        source_type = _endpoint_type_enum(source, "source")
        clauses.append(f"sourceFilter.type.eq('{source_type}')")
    if destination is not None and destination.strip():
        destination_type = _endpoint_type_enum(destination, "destination")
        clauses.append(f"destinationFilter.type.eq('{destination_type}')")
    if action is not None and action.strip():
        clauses.append(f"action.eq('{_action_enum(action)}')")
    if not clauses:
        return None
    if len(clauses) == 1:
        return clauses[0]
    return f"and({','.join(clauses)})"


def clamp_params(
    limit: int | None,
    offset: int | None,
    *,
    max_limit: int,
) -> dict[str, Any]:
    """Clamp pagination parameters into a ready-to-send params dict.

    ``limit`` follows design §12 (default 50, hard max *max_limit*); a
    negative ``offset`` is a caller error.
    """
    if offset is not None and offset < 0:
        raise InvalidValueError("offset", str(offset), ["offset >= 0"])
    params: dict[str, Any] = {
        "limit": min(clamp_limit(limit, max_limit=max_limit), max_limit),
        "offset": offset or 0,
    }
    return params


async def _get_acl_rule_detail(client: UniFiClient, path: str, rule_id: str) -> dict[str, Any]:
    """Detail-GET one ACL rule; 404 becomes a structured NotFoundError (§30)."""
    try:
        data = await client.get(path)
    except UniFiNotFoundError as exc:
        raise NotFoundError(resource="acl_rule", query=rule_id) from exc
    return cast("dict[str, Any]", normalize(data, level="detail"))


def register_acl_tools(server: MCPServer, client: UniFiClient, settings: Settings) -> None:
    async def list_acl_rules(
        site_id: str | None = None,
        action: str | None = None,
        enabled: bool | None = None,
        source: str | None = None,
        destination: str | None = None,
        limit: int | None = None,
        offset: int | None = None,
    ) -> dict[str, Any]:
        site = await resolve_site(client, settings, site_id)
        params = clamp_params(limit, offset, max_limit=settings.max_list_items)
        filter_expr = build_acl_filter(
            action=action, enabled=enabled, source=source, destination=destination
        )
        if filter_expr is not None:
            params["filter"] = filter_expr
        return await fetch_list(client, f"/sites/{site}/acl-rules", params=params)

    async def get_acl_rule(rule_id: str, site_id: str | None = None) -> dict[str, Any]:
        site = await resolve_site(client, settings, site_id)
        return await _get_acl_rule_detail(client, f"/sites/{site}/acl-rules/{rule_id}", rule_id)

    server.add_tool(
        wrap_tool("list_acl_rules", settings.max_tool_response_bytes, list_acl_rules),
        name="list_acl_rules",
        title="List ACL Rules",
        description=DESCRIPTION_LIST,
        annotations=ToolAnnotations(read_only_hint=True),
    )
    server.add_tool(
        wrap_tool("get_acl_rule", settings.max_tool_response_bytes, get_acl_rule),
        name="get_acl_rule",
        title="Get ACL Rule",
        description=DESCRIPTION_GET,
        annotations=ToolAnnotations(read_only_hint=True),
    )
