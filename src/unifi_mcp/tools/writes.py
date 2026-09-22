"""The guarded, audited write path (design §14/§15/§16/§27).

Every future write tool builds on :func:`guarded_update`, which enforces the
safety guarantees of the safe-writes foundation in one place:

1. **Read-before-write against *fresh* state** (§14/§29): the object is
   re-fetched and must still hash to the caller's ``expected_state_hash``.
   Never a cached value — a stale write is the exact failure this prevents.
2. **Modifiability guard** (§42 layer 6): only ``metadata.origin ==
   USER_DEFINED`` objects are writable; system/derived objects are rejected
   (fails closed on a missing/malformed origin).
3. **Field allowlist** (§15/§16): only the explicitly allowed top-level
   fields may change; anything else is a validation error, no partial
   application.
4. **Audit log** (§27): *every* attempt — successful or rejected — is
   recorded, redacted, to stdout and (when configured) a JSONL file.

The write itself is a **full-replace PUT** of the merged object, because the
UniFi v1 update DTOs are complete objects (live-verified: each ``PUT .../{id}``
takes a "create or update" DTO and returns the updated detail object). The
server-managed fields ``id`` and ``metadata`` are present in the detail read
but absent from every update DTO, so they are stripped from the body before
the PUT — they must never be echoed back to the gateway.
"""

from __future__ import annotations

import contextlib
from typing import TYPE_CHECKING, Any, cast

from unifi_mcp.safety.audit import get_audit_log
from unifi_mcp.safety.guards import (
    apply_patch,
    assert_user_defined,
    verify_expected_state,
)
from unifi_mcp.tools.common import unifi_error_to_dict, with_state_hash
from unifi_mcp.tools.errors import InvalidValueError, StateMismatchError
from unifi_mcp.unifi.errors import UniFiError
from unifi_mcp.unifi.normalization import INTERNAL_FIELDS, normalize

if TYPE_CHECKING:
    from unifi_mcp.unifi.client import UniFiClient

#: Fields that exist in a detail read but are server-managed (``id`` plus the
#: internal ``metadata``/``etag``/``revision`` revision-tracking fields) and
#: are absent from every update DTO. They are stripped from the PUT body so
#: they are never echoed back to the gateway (design §42).
_SERVER_MANAGED_FIELDS: frozenset[str] = frozenset({"id"}) | INTERNAL_FIELDS


def _strip_server_managed(body: dict[str, Any]) -> dict[str, Any]:
    """Return *body* without the server-managed fields (top-level only)."""
    return {k: v for k, v in body.items() if k not in _SERVER_MANAGED_FIELDS}


def _record(
    tool: str,
    site_id: str | None,
    object_id: str,
    object_name: str | None,
    changes: dict[str, dict[str, Any]],
    result: str,
) -> None:
    """Emit one audited write attempt; an audit failure must never mask the
    write's own outcome (design §27), so this swallows its own errors."""
    with contextlib.suppress(Exception):  # noqa: S110 - the audit log is best-effort
        get_audit_log().record(
            tool=tool,
            site_id=site_id,
            object_id=object_id,
            object_name=object_name,
            changes=changes,
            result=result,
        )


async def guarded_update(
    client: UniFiClient,
    *,
    path: str,
    object_id: str,
    resource: str,
    expected_state_hash: str,
    changes: dict[str, Any],
    allowed_fields: frozenset[str],
    tool: str,
    site_id: str | None = None,
    object_name: str | None = None,
) -> dict[str, Any]:
    """Perform one guarded, audited full-replace PUT of a mutable object.

    Returns the normalized detail representation of the *updated* object,
    including a fresh ``state_hash`` for the next read-before-write cycle.

    Raises (each audited as a rejected attempt, design §27):

    - :class:`StateMismatchError` — the object changed since the read (§14);
    - :class:`InvalidValueError` — not user-defined, or a field outside the
      allowlist (§16);
    - :class:`UniFiError` — the gateway rejected the write.
    """
    # 1. read-before-write against fresh state (§14/§29).
    try:
        raw = await verify_expected_state(
            client, path, expected_state_hash, resource=resource
        )
    except StateMismatchError:
        _record(tool, site_id, object_id, object_name, {}, "state_changed")
        raise

    # 2. modifiability guard: only USER_DEFINED objects are writable.
    try:
        assert_user_defined(raw, resource=resource)
    except InvalidValueError:
        _record(tool, site_id, object_id, object_name, {}, "validation")
        raise

    # 3. field allowlist + shallow merge into the full object (§15/§16).
    try:
        merged, diff = apply_patch(raw, changes, allowed_fields=allowed_fields)
    except InvalidValueError:
        _record(tool, site_id, object_id, object_name, {}, "validation")
        raise

    body = _strip_server_managed(merged)

    # 4. the write itself (full-replace PUT).
    try:
        updated = await client.put(path, json=body)
    except UniFiError as exc:
        _record(tool, site_id, object_id, object_name, diff, unifi_error_to_dict(exc)["error"])
        raise

    # 5. audited success; report the updated object with a fresh state hash.
    _record(tool, site_id, object_id, object_name, diff, "ok")
    return with_state_hash(cast("dict[str, Any]", normalize(updated, level="detail")))
