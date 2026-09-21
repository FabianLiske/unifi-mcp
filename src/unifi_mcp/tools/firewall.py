"""Zone-based firewall tools (design §10.7).

Endpoints (docs/unifi-api-notes.md §5/§6, live-verified on 10.6.106):

- zones:    ``/sites/{site}/firewall/zones[/{id}]``
- policies: ``/sites/{site}/firewall/policies[/{id}]``
- ordering: ``/sites/{site}/firewall/policies/ordering`` (per zone pair)

On a gateway where the zone-based firewall is not configured, the zone and
policy endpoints answer HTTP 400 with api_code
``api.firewall.zone-based-firewall-not-configured``. The shared error mapping
in :func:`~unifi_mcp.tools.common.wrap_tool` turns that into a structured
``unsupported`` result — a gateway configuration state, not a request error
(design §3/§40).

Live-verified policy quirks (2026-09-21):

- list items may lack ``id``: system-derived policies (e.g.
  ``000-0-ALLOW-ESTABLISHED-RELATED``) are computed by the gateway and cannot
  be fetched via the detail endpoint;
- filterable (live): ``name.like``, ``metadata.origin.eq`` and the nested
  ``source.zoneId.eq`` / ``destination.zoneId.eq`` (UUID **unquoted**);
  ``action`` and ``enabled`` are NOT filterable despite the OpenAPI 10.4.57
  table;
- the ordering endpoint requires a zone pair
  (``sourceFirewallZoneId`` + ``destinationFirewallZoneId``).
"""

from __future__ import annotations

import uuid as _uuid
from typing import TYPE_CHECKING, Any, cast

from mcp_types import ToolAnnotations

from unifi_mcp.tools.common import fetch_list, resolve_site, wrap_tool
from unifi_mcp.tools.devices import clamp_params
from unifi_mcp.tools.errors import InvalidValueError, NotFoundError
from unifi_mcp.unifi.errors import UniFiNotFoundError
from unifi_mcp.unifi.normalization import normalize

if TYPE_CHECKING:
    from mcp.server.mcpserver import MCPServer

    from unifi_mcp.config import Settings
    from unifi_mcp.unifi.client import UniFiClient

LIST_FIREWALL_ZONES_DESCRIPTION = """\
Lists the zone-based firewall zones configured for the site. When the gateway
has not set up the zone-based firewall, returns a structured 'unsupported'
error (a gateway configuration state, not a request error — configure the
zone-based firewall on the gateway first). Returns at most 'limit' zones
(default 50, max 200); when next_offset is set, call again with
offset=next_offset to page. This tool is read-only.
"""

GET_FIREWALL_ZONE_DESCRIPTION = """\
Gets one zone-based firewall zone by id. When the gateway has not set up the
zone-based firewall, returns a structured 'unsupported' error; when no zone
has the given id, a structured not_found error. Obtain ids from
list_firewall_zones. This tool is read-only.
"""

LIST_FIREWALL_POLICIES_DESCRIPTION = """\
Lists the zone-based firewall policies (rules between firewall zones) of the
site: action (ALLOW/BLOCK/REJECT), enabled flag, index (lower index = higher
priority), source/destination zone, and the traffic filters (networks, IP
addresses/subnets, ports, DPI applications). Note: system-derived policies
(e.g. established/return-traffic rules) are computed by the gateway and may
lack an 'id' in the list; those cannot be fetched via get_firewall_policy.
Filters (all applied gateway-side): name (substring), origin (user | system),
source_zone_id and destination_zone_id (UUIDs from list_firewall_zones).
'action' and 'enabled' are not filterable on this endpoint. When the gateway
has not set up the zone-based firewall, returns a structured 'unsupported'
error (a gateway configuration state, not a request error). Returns at most
'limit' policies (default 50, max 200); when next_offset is set, call again
with offset=next_offset to page. This tool is read-only.
"""

GET_FIREWALL_POLICY_DESCRIPTION = """\
Gets one zone-based firewall policy by the id returned by
list_firewall_policies, including the full source/destination traffic
filters (network ids, IP addresses or subnets, port ranges, DPI
applications). Pass site_id to look in another site. Unknown ids return a
not_found error; when the gateway has not set up the zone-based firewall,
returns a structured 'unsupported' error. This tool is read-only.
"""

GET_FIREWALL_POLICY_ORDERING_DESCRIPTION = """\
Returns the evaluation order of the zone-based firewall policies for one zone
pair (source_zone_id, destination_zone_id — UUIDs from
list_firewall_zones): user-defined policies evaluated before the
system-defined ones ('before_system_defined') and those evaluated after
('after_system_defined'). Pass site_id to look in another site. When the
gateway has not set up the zone-based firewall, returns a structured
'unsupported' error. This tool is read-only.
"""

#: user-friendly ``origin`` value -> UniFi metadata origin enum (UPPERCASE).
_ORIGIN_API: dict[str, str] = {
    "user": "USER_DEFINED",
    "user_defined": "USER_DEFINED",
    "system": "SYSTEM_DEFINED",
    "system_defined": "SYSTEM_DEFINED",
}

ALLOWED_ORIGINS = ("user", "system")


def _origin_enum(value: str) -> str:
    key = value.strip().lower()
    if key not in _ORIGIN_API:
        raise InvalidValueError("origin", value, list(ALLOWED_ORIGINS))
    return _ORIGIN_API[key]


def _validate_zone_id(value: str, field: str) -> str:
    """Ensure *value* is a well-formed UUID (the gateway's filter rejects
    quoted or malformed UUIDs with a 400 — fail earlier with a clean error)."""
    try:
        _uuid.UUID(value)
    except (ValueError, AttributeError, TypeError):
        raise InvalidValueError(field, value, ["UUID"]) from None
    return value


def build_policy_filter(
    *,
    name: str | None = None,
    origin: str | None = None,
    source_zone_id: str | None = None,
    destination_zone_id: str | None = None,
) -> str | None:
    """Build the function-based UniFi filter expression for
    list_firewall_policies (live-verified clauses; see module docstring).

    Returns ``None`` when no clause applies. Argument values are validated
    against allowlists (no free-form filter passthrough); zone ids are sent
    unquoted, as the filter DSL requires for UUID literals.
    """
    clauses: list[str] = []
    if name is not None and name.strip():
        term = name.strip().replace("'", "")
        if term:
            clauses.append(f"name.like('*{term}*')")
    if origin is not None and origin.strip():
        clauses.append(f"metadata.origin.eq('{_origin_enum(origin)}')")
    if source_zone_id is not None and source_zone_id.strip():
        zone_id = _validate_zone_id(source_zone_id.strip(), "source_zone_id")
        clauses.append(f"source.zoneId.eq({zone_id})")
    if destination_zone_id is not None and destination_zone_id.strip():
        zone_id = _validate_zone_id(destination_zone_id.strip(), "destination_zone_id")
        clauses.append(f"destination.zoneId.eq({zone_id})")
    if not clauses:
        return None
    if len(clauses) == 1:
        return clauses[0]
    return f"and({','.join(clauses)})"


def register_firewall_tools(server: MCPServer, client: UniFiClient, settings: Settings) -> None:
    async def list_firewall_zones(
        site_id: str | None = None,
        limit: int | None = None,
        offset: int | None = None,
    ) -> dict[str, Any]:
        site = await resolve_site(client, settings, site_id)
        params = clamp_params(limit, offset, max_limit=settings.max_list_items)
        return await fetch_list(client, f"/sites/{site}/firewall/zones", params=params)

    async def get_firewall_zone(zone_id: str, site_id: str | None = None) -> dict[str, Any]:
        site = await resolve_site(client, settings, site_id)
        try:
            data = await client.get(f"/sites/{site}/firewall/zones/{zone_id}")
        except UniFiNotFoundError:
            raise NotFoundError(resource="firewall_zone", query=zone_id) from None
        normalized: dict[str, Any] = normalize(data, level="detail")
        return normalized

    async def list_firewall_policies(
        site_id: str | None = None,
        name: str | None = None,
        origin: str | None = None,
        source_zone_id: str | None = None,
        destination_zone_id: str | None = None,
        limit: int | None = None,
        offset: int | None = None,
    ) -> dict[str, Any]:
        site = await resolve_site(client, settings, site_id)
        params = clamp_params(limit, offset, max_limit=settings.max_list_items)
        filter_expr = build_policy_filter(
            name=name,
            origin=origin,
            source_zone_id=source_zone_id,
            destination_zone_id=destination_zone_id,
        )
        if filter_expr is not None:
            params["filter"] = filter_expr
        return await fetch_list(client, f"/sites/{site}/firewall/policies", params=params)

    async def get_firewall_policy(policy_id: str, site_id: str | None = None) -> dict[str, Any]:
        site = await resolve_site(client, settings, site_id)
        try:
            data = await client.get(f"/sites/{site}/firewall/policies/{policy_id}")
        except UniFiNotFoundError:
            raise NotFoundError(resource="firewall_policy", query=policy_id) from None
        normalized: dict[str, Any] = normalize(data, level="detail")
        return normalized

    async def get_firewall_policy_ordering(
        source_zone_id: str,
        destination_zone_id: str,
        site_id: str | None = None,
    ) -> dict[str, Any]:
        site = await resolve_site(client, settings, site_id)
        source = _validate_zone_id(source_zone_id.strip(), "source_zone_id")
        destination = _validate_zone_id(destination_zone_id.strip(), "destination_zone_id")
        data = await client.get(
            f"/sites/{site}/firewall/policies/ordering",
            params={
                "sourceFirewallZoneId": source,
                "destinationFirewallZoneId": destination,
            },
        )
        ordered = cast("dict[str, Any]", data).get("orderedFirewallPolicyIds") or {}
        return {
            "source_zone_id": source,
            "destination_zone_id": destination,
            "before_system_defined": list(ordered.get("beforeSystemDefined") or []),
            "after_system_defined": list(ordered.get("afterSystemDefined") or []),
        }

    server.add_tool(
        wrap_tool("list_firewall_zones", settings.max_tool_response_bytes, list_firewall_zones),
        name="list_firewall_zones",
        title="List Firewall Zones",
        description=LIST_FIREWALL_ZONES_DESCRIPTION,
        annotations=ToolAnnotations(read_only_hint=True),
    )
    server.add_tool(
        wrap_tool("get_firewall_zone", settings.max_tool_response_bytes, get_firewall_zone),
        name="get_firewall_zone",
        title="Get Firewall Zone",
        description=GET_FIREWALL_ZONE_DESCRIPTION,
        annotations=ToolAnnotations(read_only_hint=True),
    )
    server.add_tool(
        wrap_tool(
            "list_firewall_policies", settings.max_tool_response_bytes, list_firewall_policies
        ),
        name="list_firewall_policies",
        title="List Firewall Policies",
        description=LIST_FIREWALL_POLICIES_DESCRIPTION,
        annotations=ToolAnnotations(read_only_hint=True),
    )
    server.add_tool(
        wrap_tool("get_firewall_policy", settings.max_tool_response_bytes, get_firewall_policy),
        name="get_firewall_policy",
        title="Get Firewall Policy",
        description=GET_FIREWALL_POLICY_DESCRIPTION,
        annotations=ToolAnnotations(read_only_hint=True),
    )
    server.add_tool(
        wrap_tool(
            "get_firewall_policy_ordering",
            settings.max_tool_response_bytes,
            get_firewall_policy_ordering,
        ),
        name="get_firewall_policy_ordering",
        title="Get Firewall Policy Ordering",
        description=GET_FIREWALL_POLICY_ORDERING_DESCRIPTION,
        annotations=ToolAnnotations(read_only_hint=True),
    )
