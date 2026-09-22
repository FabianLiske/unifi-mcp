"""Shared helpers for MCP tools (design doc §9/§12/§28/§30).

Every tool implementation goes through :func:`wrap_tool`, which guarantees:

- the call is timed and recorded via ``record_mcp_request`` (§28),
- the response respects the hard size cap (§12),
- every failure becomes a short structured JSON error dict (§30) — tools
  never surface raw HTTP errors or tracebacks to the LLM.

List tools share :func:`fetch_list` (UniFi page envelope → MCP envelope),
site-scoped tools share :func:`resolve_site` / :func:`site_overview`.
"""

from __future__ import annotations

import functools
import time
from collections.abc import Awaitable, Callable
from typing import TYPE_CHECKING, Any

from unifi_mcp.observability.logging import get_logger
from unifi_mcp.observability.metrics import record_mcp_request, record_write_request
from unifi_mcp.safety.state_hash import compute_state_hash
from unifi_mcp.tools.errors import NotFoundError, ToolError, check_response_size
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
from unifi_mcp.unifi.normalization import clamp_limit, page_to_mcp

if TYPE_CHECKING:
    from unifi_mcp.config import Settings
    from unifi_mcp.unifi.client import UniFiClient

logger = get_logger(__name__)

#: A tool implementation: async, typed parameters, returns a JSON-safe dict.
ToolFn = Callable[..., Awaitable[dict[str, Any]]]

_INTERNAL_ERROR_PAYLOAD: dict[str, Any] = {
    "error": "internal_error",
    "message": (
        "Internal server error: the tool call could not be completed. "
        "Retry, or check the server logs."
    ),
}

#: Exception type -> structured error code (checked in order; the most
#: specific types are subtypes of :class:`UniFiError`, not of each other).
_ERROR_CODE_BY_TYPE: tuple[tuple[type[UniFiError], str], ...] = (
    (UniFiNotFoundError, "not_found"),
    (UniFiAuthenticationError, "authentication"),
    (UniFiAuthorizationError, "authorization"),
    (UniFiValidationError, "validation"),
    (UniFiRateLimitError, "rate_limited"),
    (UniFiConflictError, "conflict"),
    (UniFiResponseTooLargeError, "response_too_large"),
    (UniFiUnavailableError, "unavailable"),
)


def wrap_tool(
    name: str, max_response_bytes: int, fn: ToolFn, *, write: bool = False
) -> ToolFn:
    """Wrap a tool implementation with metrics, size cap, and error mapping.

    ``functools.wraps`` keeps *fn*'s signature so the MCP SDK still derives
    the tool's input schema from its typed parameters. Pass ``write=True``
    for mutating tools so they are additionally recorded via
    ``record_write_request`` (design §28 ``unifi_mcp_write_requests_total``).
    """

    @functools.wraps(fn)
    async def wrapper(*args: Any, **kwargs: Any) -> dict[str, Any]:
        started = time.monotonic()
        status = "ok"
        try:
            payload = await fn(*args, **kwargs)
            check_response_size(payload, max_bytes=max_response_bytes)
        except ToolError as exc:
            payload = exc.to_dict()
            status = exc.code
        except UniFiError as exc:
            payload = unifi_error_to_dict(exc)
            status = str(payload["error"])
        except Exception:
            logger.exception("tool crashed", tool=name)
            payload = dict(_INTERNAL_ERROR_PAYLOAD)
            status = "internal_error"
        record_mcp_request(name, status, time.monotonic() - started)
        if write:
            record_write_request(name, status)
        return payload

    return wrapper


def unifi_error_to_dict(exc: UniFiError) -> dict[str, Any]:
    """Map a client exception to a structured LLM error dict (design §30).

    The code first, the message last. ``unsupported`` is reserved for
    ``not-configured`` API responses (a gateway configuration state, not a
    request error — design §3/§40).
    """
    code = "unifi_error"
    for exc_type, mapped in _ERROR_CODE_BY_TYPE:
        if isinstance(exc, exc_type):
            code = mapped
            break
    if code == "validation" and _is_not_configured(exc):
        code = "unsupported"
    payload: dict[str, Any] = {"error": code}
    if exc.api_code is not None:
        payload["api_code"] = exc.api_code
    if isinstance(exc, UniFiRateLimitError) and exc.retry_after is not None:
        payload["retry_after_seconds"] = exc.retry_after
    payload["message"] = exc.message
    return payload


def _is_not_configured(exc: UniFiError) -> bool:
    return isinstance(exc, UniFiValidationError) and (
        exc.api_code is not None and "not-configured" in exc.api_code
    )


async def resolve_site(
    client: UniFiClient, settings: Settings, requested: str | None = None
) -> str:
    """Resolve the site id a site-scoped call should use.

    Precedence: explicit *requested* → configured ``UNIFI_SITE_ID`` → first
    site the gateway reports. Raises :class:`NotFoundError` when no site can
    be determined at all.
    """
    if requested is not None and requested.strip():
        return requested.strip()
    if client.site_id is not None:
        return client.site_id
    page = await client.get_list("/sites", params={"limit": 1})
    for item in page.data:
        if isinstance(item, dict) and isinstance(item.get("id"), str):
            return str(item["id"])
    raise NotFoundError(
        resource="site",
        query="configured",
        message="The gateway reports no sites and UNIFI_SITE_ID is not set.",
    )


async def site_overview(client: UniFiClient, settings: Settings) -> dict[str, Any]:
    """Return the active site as ``{"id": ..., "name": ...}``.

    The name is best-effort: an explicitly configured site that the API key
    cannot see in ``/sites`` is still reported (with ``name: null``).
    """
    wanted = settings.unifi_site_id
    limit = clamp_limit(None, max_limit=settings.max_list_items)
    page = await client.get_list("/sites", params={"limit": limit})
    for item in page.data:
        if not isinstance(item, dict) or not isinstance(item.get("id"), str):
            continue
        if wanted is not None and item["id"] != wanted:
            continue
        name = item.get("name")
        return {"id": str(item["id"]), "name": name if isinstance(name, str) else None}
    if wanted is not None:
        return {"id": wanted, "name": None}
    raise NotFoundError(
        resource="site",
        query="gateway",
        message="The gateway reports no sites and UNIFI_SITE_ID is not set.",
    )


async def fetch_list(
    client: UniFiClient,
    path: str,
    *,
    params: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Fetch a list endpoint and return the normalized MCP envelope (§12).

    Items are normalized at the ``summary`` level (internal fields stripped,
    secrets redacted, deep containers pruned).
    """
    page = await client.get_list(path, params=params)
    return page_to_mcp(page, level="summary")


def with_state_hash(detail: dict[str, Any]) -> dict[str, Any]:
    """Return *detail* with a ``state_hash`` key appended (design §14).

    *detail* is the normalized detail representation; the hash is computed
    over exactly what the LLM sees, so a write guarded against this value
    cannot be fooled by normalization or key-order differences. The input
    dict is not mutated.
    """
    out = dict(detail)
    out["state_hash"] = compute_state_hash(detail)
    return out
