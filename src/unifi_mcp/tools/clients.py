"""Client tools (design §10.4, §11).

The local UniFi API exposes a minimal client object (live-verified in WP-8):
``id, name, type, ipAddress, macAddress, uplinkDeviceId, connectedAt,
access`` — no ``hostname``, no per-client ``networkId``, no state, no SSID
or port information. The client filter DSL only accepts ``type``, ``id``
(raw UUID), ``ipAddress``/``macAddress`` (``eq`` only — ``like`` is
rejected), ``access.type`` and ``connectedAt``; ``name``, ``networkId`` and
``uplinkDeviceId`` are **not** filter properties. Consequently exact IP/MAC
searches are pushed to the gateway, while name-substring searches and the
``network_id``/``connected_to_device_id`` filters are applied by this module
to the fetched page.
"""

from __future__ import annotations

import ipaddress
import re
from typing import TYPE_CHECKING, Any, cast

from mcp_types import ToolAnnotations

from unifi_mcp.tools.common import fetch_list, resolve_site, wrap_tool
from unifi_mcp.tools.errors import AmbiguousMatchError, InvalidValueError, NotFoundError
from unifi_mcp.unifi.errors import UniFiNotFoundError
from unifi_mcp.unifi.normalization import MAX_LIMIT, clamp_limit, normalize

if TYPE_CHECKING:
    from mcp.server.mcpserver import MCPServer

    from unifi_mcp.config import Settings
    from unifi_mcp.unifi.client import UniFiClient

LIST_CLIENTS_DESCRIPTION = """\
Lists network clients (wired and wireless) with their name, IP and MAC
address, connection type and time, and the id of the device they are
attached to (uplinkDeviceId). Combinable filters: site_id (defaults to the
configured site, else the first site), network_id (keeps clients whose IP
address falls into that network's IPv4 subnet; the gateway exposes no
per-client network filter, and a network without an IPv4 subnet matches
nothing), connected_to_device_id (keeps clients attached to that device),
search (an exact IPv4 address or exact MAC address is matched by the
gateway; any other term is a case-insensitive substring of the client name,
IP, or MAC). Note: the local API reports no SSID, no port, and no per-client
networkId field, and name is not a filter property — substring and
attachment filters are therefore applied to the fetched page (up to 200
clients). Returns at most 'limit' clients (default 50, max 200); when
next_offset is set, call again with offset=next_offset to page. Errors:
not_found (unknown site or network), validation (negative offset). This tool
is read-only.
"""

GET_CLIENT_DESCRIPTION = """\
Fetches one client's detail object. Provide EXACTLY ONE identifier:
client_id (fetched directly), mac (matched by the gateway), ip (matched by
the gateway), or hostname (matched case-insensitively against the client
name field — the gateway has no hostname filter, so up to 200 clients are
scanned). site_id defaults to the configured site, else the first site.
Errors: validation (zero or several identifiers, malformed mac/ip),
not_found (no match or unknown client), ambiguous_match (several matches —
inspect the 'matches' list and retry with client_id). This tool is read-only.
"""

INSPECT_CLIENT_PATH_DESCRIPTION = """\
Composite diagnostics for one client: resolves it (exactly one of
client_id, mac, ip, hostname — see get_client) and aggregates the facts the
gateway reports: the client's detail object, its network (via the client's
networkId field, when present), the device it is attached to (via
uplinkDeviceId), and plain facts as 'observations' (e.g. 'wireless',
'wired', 'client_connected', 'network:<name>', 'uplink_device:<name>').
Facts only — no root-cause analysis. Missing parts are null plus an
observation, never an error: the local API reports no SSID, no port, and no
client-to-network mapping, so 'network' is null whenever the client does
not carry a networkId. Errors: validation, not_found, ambiguous_match as in
get_client. This tool is read-only.
"""

_MAC_RE = re.compile(r"([0-9a-f]{2}:){5}[0-9a-f]{2}")

#: Identifier fields accepted by get_client / inspect_client_path (design
#: §10.4: exactly one of them is required).
IDENTIFIER_FIELDS: tuple[str, ...] = ("client_id", "mac", "ip", "hostname")

_SEARCH_FIELDS: tuple[str, ...] = ("name", "ipAddress", "macAddress")


def _normalized_mac(value: str) -> str | None:
    key = value.strip().lower()
    return key if _MAC_RE.fullmatch(key) else None


def _is_ipv4(value: str) -> bool:
    try:
        ipaddress.IPv4Address(value.strip())
    except ValueError:
        return False
    return True


def build_client_filter(*, search: str | None = None) -> str | None:
    """Build the server-side UniFi filter expression for list_clients.

    Live-verified (WP-8): the client filter DSL only accepts ``type``,
    ``id`` (raw UUID), ``ipAddress``/``macAddress`` (``eq`` only, ``like``
    is rejected), ``access.type`` and ``connectedAt`` — no ``name``/
    ``hostname``, no ``networkId``, no ``uplinkDeviceId``. A *search* term
    therefore becomes a server-side clause only when it is an exact IPv4
    address (``ipAddress.eq``) or an exact MAC address (``macAddress.eq``);
    anything else returns ``None`` and is applied client-side by
    :func:`filter_client_items`.
    """
    if search is None:
        return None
    term = search.strip().replace("'", "")
    if not term:
        return None
    if _is_ipv4(term):
        return f"ipAddress.eq('{term.strip()}')"
    mac = _normalized_mac(term)
    if mac is not None:
        return f"macAddress.eq('{mac}')"
    return None


def filter_client_items(
    items: list[Any],
    *,
    connected_to_device_id: str | None = None,
    search: str | None = None,
    subnet: ipaddress.IPv4Network | None = None,
) -> list[dict[str, Any]]:
    """Apply the filters the gateway cannot express server-side.

    ``connected_to_device_id`` matches the client's ``uplinkDeviceId``;
    ``subnet`` (a ``network_id`` filter) keeps clients whose ``ipAddress``
    falls into it; ``search`` is a case-insensitive substring of the client
    name, IP, or MAC (the DSL has no name property). Non-dict entries are
    dropped.
    """
    term = search.strip().lower() if search is not None else ""
    device_id = connected_to_device_id.strip() if connected_to_device_id else ""
    out: list[dict[str, Any]] = []
    for item in items:
        if isinstance(item, dict) and _item_matches(
            item, device_id=device_id, subnet=subnet, term=term
        ):
            out.append(item)
    return out


def _item_matches(
    item: dict[str, Any],
    *,
    device_id: str,
    subnet: ipaddress.IPv4Network | None,
    term: str,
) -> bool:
    if device_id and str(item.get("uplinkDeviceId") or "") != device_id:
        return False
    if subnet is not None:
        ip = item.get("ipAddress")
        try:
            contained = isinstance(ip, str) and ipaddress.ip_address(ip) in subnet
        except ValueError:
            contained = False
        if not contained:
            return False
    if term:
        haystack = " ".join(str(item.get(field) or "") for field in _SEARCH_FIELDS).lower()
        if term not in haystack:
            return False
    return True


def exactly_one_identifier(
    *,
    client_id: str | None = None,
    mac: str | None = None,
    ip: str | None = None,
    hostname: str | None = None,
) -> str:
    """Return the name of the single provided client identifier.

    Raises :class:`InvalidValueError` when none or more than one of the
    identifier fields is set (design §10.4: exactly one is required).
    """
    values: dict[str, str | None] = {
        "client_id": client_id,
        "mac": mac,
        "ip": ip,
        "hostname": hostname,
    }
    provided = [name for name, value in values.items() if value is not None and value.strip()]
    if len(provided) != 1:
        raise InvalidValueError(
            "identifier",
            ",".join(provided) if provided else "none",
            list(IDENTIFIER_FIELDS),
            message="Provide exactly one client identifier: client_id, mac, ip or hostname.",
        )
    return provided[0]


def _validate_identifier_value(field: str, value: str) -> str:
    key = value.strip()
    if field == "mac":
        mac = _normalized_mac(key)
        if mac is None:
            raise InvalidValueError("mac", value, ["MAC address, e.g. aa:bb:cc:dd:ee:ff"])
        return mac
    if not _is_ipv4(key):
        raise InvalidValueError("ip", value, ["IPv4 address, e.g. 192.168.1.10"])
    return key


def _compact_match(item: dict[str, Any]) -> dict[str, Any]:
    """Compact disambiguation view: only id, name, ip and mac."""
    return {key: item[key] for key in ("id", "name", "ipAddress", "macAddress") if key in item}


async def _client_detail(client: UniFiClient, site: str, client_id: str) -> dict[str, Any]:
    try:
        raw = await client.get(f"/sites/{site}/clients/{client_id}")
    except UniFiNotFoundError:
        raise NotFoundError(resource="client", query=client_id) from None
    return cast("dict[str, Any]", normalize(raw, level="detail"))


async def resolve_client(
    client: UniFiClient,
    site: str,
    *,
    client_id: str | None = None,
    mac: str | None = None,
    ip: str | None = None,
    hostname: str | None = None,
) -> dict[str, Any]:
    """Resolve a client by exactly one identifier and return its detail object.

    ``client_id`` is fetched directly; ``mac``/``ip`` use the gateway filter
    (``macAddress.eq`` / ``ipAddress.eq``, limit 2); ``hostname`` matches the
    client ``name`` field (exact, case-insensitive) over the largest page
    the gateway allows, because ``name`` is not a filter property. Zero
    matches raise :class:`NotFoundError`, more than one
    :class:`AmbiguousMatchError` with a compact match list.
    """
    field = exactly_one_identifier(client_id=client_id, mac=mac, ip=ip, hostname=hostname)
    if field == "client_id":
        return await _client_detail(client, site, cast(str, client_id).strip())
    if field in ("mac", "ip"):
        value = _validate_identifier_value(field, cast(str, mac if field == "mac" else ip))
        filter_expr = build_client_filter(search=value)
        assert filter_expr is not None
        page = await client.get_list(
            f"/sites/{site}/clients", params={"filter": filter_expr, "limit": 2}
        )
        matches = [item for item in page.data if isinstance(item, dict)]
        total = page.total_count
    else:
        term = cast(str, hostname).strip().lower()
        page = await client.get_list(f"/sites/{site}/clients", params={"limit": MAX_LIMIT})
        matches = [
            item
            for item in page.data
            if isinstance(item, dict)
            and isinstance(item.get("name"), str)
            and item["name"].strip().lower() == term
        ]
        total = len(matches)
    raw_value: str | None
    if field == "client_id":
        raw_value = client_id
    elif field == "mac":
        raw_value = mac
    elif field == "ip":
        raw_value = ip
    else:
        raw_value = hostname
    query = f"{field}={raw_value}"
    if total == 0:
        raise NotFoundError(resource="client", query=query)
    if total > 1:
        raise AmbiguousMatchError(
            matches=[_compact_match(item) for item in matches[:2]],
            message=f"Multiple clients match {query}; retry with client_id.",
        )
    return await _client_detail(client, site, str(matches[0]["id"]))


def build_client_path_observations(
    client_obj: dict[str, Any],
    *,
    network: dict[str, Any] | None,
    attachment_device: dict[str, Any] | None,
) -> list[str]:
    """Plain facts about the client's path (design §11: no speculation)."""
    observations: list[str] = []
    ctype = client_obj.get("type")
    if ctype == "WIRELESS":
        observations.append("wireless")
        observations.append("ssid_not_reported_by_api")
    elif ctype == "WIRED":
        observations.append("wired")
        observations.append("port_not_reported_by_api")
    if client_obj.get("connectedAt"):
        observations.append("client_connected")
    if client_obj.get("networkId"):
        name = network.get("name") if network else None
        observations.append(f"network:{name}" if name else "network_not_found")
    else:
        observations.append("no_network_assignment")
    if client_obj.get("uplinkDeviceId"):
        name = attachment_device.get("name") if attachment_device else None
        observations.append(f"uplink_device:{name}" if name else "uplink_device_not_found")
    else:
        observations.append("no_uplink_device_reported")
    return observations


async def _network_subnet(
    client: UniFiClient, site: str, network_id: str
) -> ipaddress.IPv4Network | None:
    try:
        raw = await client.get(f"/sites/{site}/networks/{network_id}")
    except UniFiNotFoundError:
        raise NotFoundError(resource="network", query=network_id) from None
    if not isinstance(raw, dict):
        return None
    cfg = raw.get("ipv4Configuration")
    if not isinstance(cfg, dict):
        return None
    host, plen = cfg.get("hostIpAddress"), cfg.get("prefixLength")
    if not isinstance(host, str) or not isinstance(plen, int):
        return None
    try:
        return ipaddress.IPv4Network(f"{host}/{plen}", strict=False)
    except ValueError:
        return None


def _clamp_pagination(
    limit: int | None,
    offset: int | None,
    *,
    max_limit: int,
) -> tuple[int, int]:
    """Clamp pagination parameters (design §12); negative offset is an error."""
    if offset is not None and offset < 0:
        raise InvalidValueError("offset", str(offset), ["offset >= 0"])
    return clamp_limit(limit, max_limit=max_limit), (offset or 0)


async def list_clients_data(
    client: UniFiClient,
    site: str,
    *,
    network_id: str | None = None,
    connected_to_device_id: str | None = None,
    search: str | None = None,
    limit: int | None = None,
    offset: int | None = None,
) -> dict[str, Any]:
    """List clients as the normalized MCP envelope (design §12).

    When only the gateway applies the filters (plain list or exact IP/MAC
    search), the gateway's pagination is preserved. When a client-side
    filter is active (network_id, connected_to_device_id, or a name
    substring), the largest page is fetched and the caller's limit/offset
    are applied to the filtered result.
    """
    window_limit, window_offset = _clamp_pagination(limit, offset, max_limit=MAX_LIMIT)
    filter_expr = build_client_filter(search=search)
    client_side = bool(
        (network_id is not None and network_id.strip())
        or (connected_to_device_id is not None and connected_to_device_id.strip())
        or (search is not None and search.strip() and filter_expr is None)
    )
    if not client_side:
        params: dict[str, Any] = {"limit": window_limit, "offset": window_offset}
        if filter_expr is not None:
            params["filter"] = filter_expr
        return await fetch_list(client, f"/sites/{site}/clients", params=params)

    if network_id is not None and network_id.strip():
        subnet = await _network_subnet(client, site, network_id.strip())
        if subnet is None:
            return {"items": [], "count": 0, "total_count": 0, "next_offset": None}
    else:
        subnet = None
    page = await client.get_list(f"/sites/{site}/clients", params={"limit": MAX_LIMIT, "offset": 0})
    items = filter_client_items(
        page.data,
        connected_to_device_id=connected_to_device_id,
        # For exact IP/MAC terms the substring check keeps exactly the
        # gateway-matched client, so it is safe to always apply it here.
        search=search,
        subnet=subnet,
    )
    normalized = [normalize(item, level="summary") for item in items]
    total = len(normalized)
    window = normalized[window_offset : window_offset + window_limit]
    next_offset = window_offset + len(window) if window_offset + len(window) < total else None
    return {
        "items": window,
        "count": len(window),
        "total_count": total,
        "next_offset": next_offset,
    }


async def inspect_client_path_data(
    client: UniFiClient,
    site: str,
    *,
    client_id: str | None = None,
    mac: str | None = None,
    ip: str | None = None,
    hostname: str | None = None,
) -> dict[str, Any]:
    """Aggregate the facts of a client's path (design §11: facts only).

    Missing parts (offline client without uplink, unresolvable network)
    become ``null`` plus an observation — never an error.
    """
    client_obj = await resolve_client(
        client, site, client_id=client_id, mac=mac, ip=ip, hostname=hostname
    )

    network: dict[str, Any] | None = None
    network_id = client_obj.get("networkId")
    if isinstance(network_id, str) and network_id:
        try:
            raw = await client.get(f"/sites/{site}/networks/{network_id}")
        except UniFiNotFoundError:
            raw = None
        if isinstance(raw, dict):
            network = cast("dict[str, Any]", normalize(raw, level="detail"))

    device: dict[str, Any] | None = None
    attachment: dict[str, Any] | None = None
    uplink_device_id = client_obj.get("uplinkDeviceId")
    if isinstance(uplink_device_id, str) and uplink_device_id:
        try:
            raw = await client.get(f"/sites/{site}/devices/{uplink_device_id}")
        except UniFiNotFoundError:
            raw = None
        if isinstance(raw, dict):
            device = cast("dict[str, Any]", normalize(raw, level="detail"))
        attachment = {"device": device, "port": None}

    return {
        "client": client_obj,
        "network": network,
        "attachment": attachment,
        "observations": build_client_path_observations(
            client_obj, network=network, attachment_device=device
        ),
    }


def register_client_tools(server: MCPServer, client: UniFiClient, settings: Settings) -> None:
    async def list_clients(
        site_id: str | None = None,
        network_id: str | None = None,
        connected_to_device_id: str | None = None,
        search: str | None = None,
        limit: int | None = None,
        offset: int | None = None,
    ) -> dict[str, Any]:
        site = await resolve_site(client, settings, site_id)
        return await list_clients_data(
            client,
            site,
            network_id=network_id,
            connected_to_device_id=connected_to_device_id,
            search=search,
            limit=limit,
            offset=offset,
        )

    async def get_client(
        client_id: str | None = None,
        mac: str | None = None,
        ip: str | None = None,
        hostname: str | None = None,
        site_id: str | None = None,
    ) -> dict[str, Any]:
        site = await resolve_site(client, settings, site_id)
        return await resolve_client(
            client, site, client_id=client_id, mac=mac, ip=ip, hostname=hostname
        )

    async def inspect_client_path(
        client_id: str | None = None,
        mac: str | None = None,
        ip: str | None = None,
        hostname: str | None = None,
        site_id: str | None = None,
    ) -> dict[str, Any]:
        site = await resolve_site(client, settings, site_id)
        return await inspect_client_path_data(
            client, site, client_id=client_id, mac=mac, ip=ip, hostname=hostname
        )

    server.add_tool(
        wrap_tool("list_clients", settings.max_tool_response_bytes, list_clients),
        name="list_clients",
        title="List Clients",
        description=LIST_CLIENTS_DESCRIPTION,
        annotations=ToolAnnotations(read_only_hint=True),
    )
    server.add_tool(
        wrap_tool("get_client", settings.max_tool_response_bytes, get_client),
        name="get_client",
        title="Get Client",
        description=GET_CLIENT_DESCRIPTION,
        annotations=ToolAnnotations(read_only_hint=True),
    )
    server.add_tool(
        wrap_tool("inspect_client_path", settings.max_tool_response_bytes, inspect_client_path),
        name="inspect_client_path",
        title="Inspect Client Path",
        description=INSPECT_CLIENT_PATH_DESCRIPTION,
        annotations=ToolAnnotations(read_only_hint=True),
    )
