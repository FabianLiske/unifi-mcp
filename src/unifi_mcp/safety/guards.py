"""Write guards: read-before-write, modifiability, field allowlists.

Design §14 (stale writes): a write requires the object to still hash to the
caller's ``expected_state_hash`` — checked against a *fresh* fetch, never a
cached one (design §29). Design §16 (field allowlists): only explicitly
allowed fields are mutable; everything else is a validation error. The
``metadata.origin`` guard rejects system-defined and derived objects, which
the gateway does not allow the API to modify (live-verified: only
``USER_DEFINED`` objects are modifiable; design §42 layer 6).
"""

from __future__ import annotations

import hmac
from typing import TYPE_CHECKING, Any, cast

from unifi_mcp.safety.state_hash import compute_state_hash
from unifi_mcp.tools.errors import InvalidValueError, StateMismatchError
from unifi_mcp.unifi.normalization import normalize

if TYPE_CHECKING:
    from unifi_mcp.unifi.client import UniFiClient


async def verify_expected_state(
    client: UniFiClient,
    path: str,
    expected_state_hash: str,
    *,
    resource: str,
) -> dict[str, Any]:
    """Fetch the current object and check it against *expected_state_hash*.

    Returns the *raw* (pre-normalization) current object: the write path
    must build its PUT body from the raw form, because the UniFi update
    DTOs are full-replace and can round-trip secret fields (e.g. the WiFi
    PSK) — those stay inside this process and are never shown to the LLM.

    Raises :class:`StateMismatchError` (carrying the current hash) when the
    object changed since the caller read it.
    """
    raw = await client.get(path)
    normalized = cast("dict[str, Any]", normalize(raw, level="detail"))
    current = compute_state_hash(normalized)
    if not hmac.compare_digest(current, expected_state_hash):
        raise StateMismatchError(resource=resource, current_state_hash=current)
    return cast(dict[str, Any], raw)


def assert_user_defined(raw: Any, *, resource: str) -> None:
    """Reject objects that are not ``USER_DEFINED`` (not modifiable).

    Fails closed: a missing or malformed ``metadata.origin`` is rejected.
    """
    metadata = raw.get("metadata") if isinstance(raw, dict) else None
    origin = metadata.get("origin") if isinstance(metadata, dict) else None
    if origin != "USER_DEFINED":
        raise InvalidValueError(
            "metadata.origin",
            str(origin) if origin is not None else "<missing>",
            ["USER_DEFINED"],
            message=f"The {resource} is not user-defined and cannot be modified.",
        )


def apply_patch(
    current: dict[str, Any],
    changes: dict[str, Any],
    *,
    allowed_fields: frozenset[str],
) -> tuple[dict[str, Any], dict[str, dict[str, Any]]]:
    """Validate *changes* against the allowlist and merge them into *current*.

    Shallow-merge semantics (design §15: patch outside, full object
    inside): each allowed key in *changes* replaces the corresponding
    top-level field. *current* is never mutated.

    Returns ``(merged, diff)`` where ``diff`` maps every requested field to
    ``{"old": ..., "new": ...}`` for the audit log (design §27).

    Raises :class:`InvalidValueError` (code ``validation``) for the first
    field that is not on the allowlist — no partial application.
    """
    allowed_sorted = sorted(allowed_fields)
    for field in changes:
        if field not in allowed_fields:
            raise InvalidValueError("changes", str(field), allowed_sorted)
    diff: dict[str, dict[str, Any]] = {}
    merged = dict(current)
    for field, new_value in changes.items():
        diff[field] = {"old": merged.get(field), "new": new_value}
        merged[field] = new_value
    return merged, diff
