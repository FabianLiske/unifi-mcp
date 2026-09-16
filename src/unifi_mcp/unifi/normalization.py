"""Normalization of raw UniFi payloads for LLM consumption.

Design doc §8/§12: raw UniFi objects are large and full of internal
revision-tracking noise. Tools expose two levels:

* ``detail``  — internal fields stripped, secrets redacted, otherwise
  complete (used by ``get_*`` tools).
* ``summary`` — like ``detail`` plus depth pruning: containers nested deeper
  than ``max_depth`` are replaced by :data:`ELLIPSIS`, keeping only shallow,
  LLM-relevant data (used by ``list_*`` tools).

Both levels *always* redact secrets — there is no opt-out (see
:mod:`unifi_mcp.safety.redaction`).
"""

from __future__ import annotations

from typing import Any, Literal

from unifi_mcp.safety.redaction import normalize_key, redact
from unifi_mcp.unifi.models import Page

#: Internal UniFi fields with no LLM value (revision tracking etc.).
INTERNAL_FIELDS: frozenset[str] = frozenset({"metadata", "etag", "revision"})

_INTERNAL_NORM: frozenset[str] = frozenset(normalize_key(name) for name in INTERNAL_FIELDS)

#: Marker replacing containers pruned at the summary depth limit.
ELLIPSIS = "…"

#: Default ``limit`` for list tools (design §12).
DEFAULT_LIMIT = 50

#: Hard maximum ``limit`` for list tools (design §12; mirrors
#: ``Settings.max_list_items``).
MAX_LIMIT = 200


def _strip_internal(value: Any) -> Any:
    """Recursively remove :data:`INTERNAL_FIELDS` from dicts and lists."""
    if isinstance(value, dict):
        out: dict[Any, Any] = {}
        for key, item in value.items():
            if isinstance(key, str) and normalize_key(key) in _INTERNAL_NORM:
                continue
            out[key] = _strip_internal(item)
        return out
    if isinstance(value, list):
        return [_strip_internal(item) for item in value]
    return value


def _prune_depth(value: Any, depth: int, max_depth: int) -> Any:
    """Collapse containers nested deeper than *max_depth* into :data:`ELLIPSIS`.

    Scalars are always kept (they are cheap, even when nested); only dicts and
    lists past the depth limit are collapsed. This keeps shallow data such as
    ``features: ["switch", "ap"]`` intact while reducing large nested objects
    like per-port interface details to an ellipsis.
    """
    if isinstance(value, (dict, list)) and depth > max_depth:
        return ELLIPSIS
    if isinstance(value, dict):
        return {key: _prune_depth(item, depth + 1, max_depth) for key, item in value.items()}
    if isinstance(value, list):
        return [_prune_depth(item, depth + 1, max_depth) for item in value]
    return value


def normalize(
    value: Any,
    *,
    level: Literal["summary", "detail"] = "detail",
    max_depth: int = 1,
) -> Any:
    """Shape a raw UniFi object for LLM output.

    Always strips internal fields and redacts secrets. For ``level="summary"``
    additionally prunes containers nested deeper than *max_depth*.
    """
    value = _strip_internal(value)
    value = redact(value)
    if level == "summary":
        value = _prune_depth(value, 0, max_depth)
    return value


def clamp_limit(requested: int | None, *, max_limit: int = MAX_LIMIT) -> int:
    """Clamp a caller-supplied list ``limit`` to sane bounds.

    ``None`` or ``<= 0`` falls back to :data:`DEFAULT_LIMIT`; values above
    *max_limit* are capped at *max_limit*.
    """
    if requested is None or requested <= 0:
        return DEFAULT_LIMIT
    if requested > max_limit:
        return max_limit
    return requested


def page_to_mcp(
    page: Page,
    *,
    level: Literal["summary", "detail"] = "detail",
    max_depth: int = 1,
) -> dict[str, Any]:
    """Convert a UniFi ``Page`` envelope into the MCP list-tool format.

    Returns ``{"items": [...], "count": n, "total_count": n, "next_offset":
    n | None}`` (design §12). ``next_offset`` is the offset of the next page,
    or ``None`` when the last page was returned.
    """
    items = [normalize(item, level=level, max_depth=max_depth) for item in page.data]
    count = len(items)
    next_offset: int | None = None
    if page.offset + count < page.total_count:
        next_offset = page.offset + count
    return {
        "items": items,
        "count": count,
        "total_count": page.total_count,
        "next_offset": next_offset,
    }
