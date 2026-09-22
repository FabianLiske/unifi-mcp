"""DNS policy tools (WP-13b, live-verified on 10.6.106, api-notes §5f).

Endpoints (docs/unifi-api-notes.md §5f):

- list:   ``/sites/{site}/dns/policies`` — filterable (live):
  ``type.eq`` , ``domain.like``, ``enabled.eq``; **not** filterable:
  ``metadata.origin`` (400 ``api.request.invalid-filter``)
- detail: ``/sites/{site}/dns/policies/{id}``

Policies are a discriminated union over ``type`` (``A_RECORD``,
``AAAA_RECORD``, ``CNAME_RECORD``, ``MX_RECORD``, ``TXT_RECORD``,
``SRV_RECORD``, ``FORWARD_DOMAIN``); the record-specific fields
(``ipv4Address``, ``targetDomain``, forwarder ``ipAddress``, ...) vary by
type.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, cast

from mcp_types import ToolAnnotations

from unifi_mcp.tools.common import (
    fetch_list,
    resolve_site,
    with_state_hash,
    wrap_tool,
)
from unifi_mcp.tools.devices import clamp_params
from unifi_mcp.tools.errors import InvalidValueError, NotFoundError
from unifi_mcp.unifi.errors import UniFiNotFoundError
from unifi_mcp.unifi.normalization import normalize

if TYPE_CHECKING:
    from mcp.server.mcpserver import MCPServer

    from unifi_mcp.config import Settings
    from unifi_mcp.unifi.client import UniFiClient

DESCRIPTION_LIST = """\
Lists DNS policies (custom DNS records and forward domains) configured
for the site. Each item carries its type (a_record | aaaa_record |
cname_record | mx_record | txt_record | srv_record | forward_domain),
the domain, the record-specific value (e.g. ipv4Address, targetDomain,
or the forwarder address), and the enabled flag. Filters: site_id,
record_type (the policy type), domain (substring match), enabled (true |
false); all filters are applied gateway-side. Returns at most 'limit'
policies (default 50, max 200); when next_offset is set, call again with
offset=next_offset to page. This tool is read-only.
"""

DESCRIPTION_GET = """\
Returns one DNS policy by the id returned by list_dns_policies, including
the record-specific fields for its type (ipv4Address for A records,
targetDomain for CNAME, the forwarder address for forward domains, ...).
Unknown ids return a not_found error. The result includes a state_hash of
the policy; write tools (if enabled) require it as expected_state_hash to
prevent stale writes. This tool is read-only.
"""

#: user-friendly ``record_type`` value -> UniFi policy ``type`` enum (live-verified).
_RECORD_TYPE_API: dict[str, str] = {
    "a": "A_RECORD",
    "a_record": "A_RECORD",
    "aaaa": "AAAA_RECORD",
    "aaaa_record": "AAAA_RECORD",
    "cname": "CNAME_RECORD",
    "cname_record": "CNAME_RECORD",
    "mx": "MX_RECORD",
    "mx_record": "MX_RECORD",
    "txt": "TXT_RECORD",
    "txt_record": "TXT_RECORD",
    "srv": "SRV_RECORD",
    "srv_record": "SRV_RECORD",
    "forward": "FORWARD_DOMAIN",
    "forward_domain": "FORWARD_DOMAIN",
}

ALLOWED_RECORD_TYPES = ("a", "aaaa", "cname", "mx", "txt", "srv", "forward")


def _record_type_enum(value: str) -> str:
    key = value.strip().lower()
    if key not in _RECORD_TYPE_API:
        raise InvalidValueError("record_type", value, list(ALLOWED_RECORD_TYPES))
    return _RECORD_TYPE_API[key]


def build_dns_filter(
    *,
    record_type: str | None = None,
    domain: str | None = None,
    enabled: bool | None = None,
) -> str | None:
    """Build the function-based UniFi filter expression for list_dns_policies.

    Returns ``None`` when no clause applies. Argument values are validated
    against allowlists (no free-form filter passthrough); the domain term is
    embedded into ``domain.like('*…*')`` (single quotes stripped, the DSL has
    no quote escaping) and ignored when it is empty.
    """
    clauses: list[str] = []
    if record_type is not None and record_type.strip():
        clauses.append(f"type.eq('{_record_type_enum(record_type)}')")
    if domain is not None:
        term = domain.strip().replace("'", "")
        if term:
            clauses.append(f"domain.like('*{term}*')")
    if enabled is not None:
        clauses.append(f"enabled.eq({'true' if enabled else 'false'})")
    if not clauses:
        return None
    if len(clauses) == 1:
        return clauses[0]
    return f"and({','.join(clauses)})"


def register_dns_tools(server: MCPServer, client: UniFiClient, settings: Settings) -> None:
    async def list_dns_policies(
        site_id: str | None = None,
        record_type: str | None = None,
        domain: str | None = None,
        enabled: bool | None = None,
        limit: int | None = None,
        offset: int | None = None,
    ) -> dict[str, Any]:
        site = await resolve_site(client, settings, site_id)
        params = clamp_params(limit, offset, max_limit=settings.max_list_items)
        filter_expr = build_dns_filter(record_type=record_type, domain=domain, enabled=enabled)
        if filter_expr is not None:
            params["filter"] = filter_expr
        return await fetch_list(client, f"/sites/{site}/dns/policies", params=params)

    async def get_dns_policy(policy_id: str, site_id: str | None = None) -> dict[str, Any]:
        site = await resolve_site(client, settings, site_id)
        try:
            data = await client.get(f"/sites/{site}/dns/policies/{policy_id}")
        except UniFiNotFoundError as exc:
            raise NotFoundError(resource="dns_policy", query=policy_id) from exc
        return with_state_hash(cast("dict[str, Any]", normalize(data, level="detail")))

    server.add_tool(
        wrap_tool("list_dns_policies", settings.max_tool_response_bytes, list_dns_policies),
        name="list_dns_policies",
        title="List DNS Policies",
        description=DESCRIPTION_LIST,
        annotations=ToolAnnotations(read_only_hint=True),
    )
    server.add_tool(
        wrap_tool("get_dns_policy", settings.max_tool_response_bytes, get_dns_policy),
        name="get_dns_policy",
        title="Get DNS Policy",
        description=DESCRIPTION_GET,
        annotations=ToolAnnotations(read_only_hint=True),
    )
