"""Stable state hashes of mutable UniFi objects (design §14).

The UniFi v1 API offers no server-side versioning (the OpenAPI spec has no
revision/ETag/If-Match), so stale writes are guarded client-side: detail
reads of mutable objects carry a ``state_hash``, and a write only proceeds
when the object still hashes to the caller's ``expected_state_hash``
(read-before-write, design §14/§29: always fetch fresh state before writing).

The hash is SHA-256 over a canonical JSON serialization of the *normalized
detail* representation (internal fields stripped, secrets redacted — see
:func:`unifi_mcp.unifi.normalization.normalize`), with:

* keys sorted at every level (key-order independence),
* volatile runtime-statistics fields excluded (they change on their own and
  would otherwise make every write conflict),
* compact separators, UTF-8 encoded.

Because normalization redacts secrets first, a change that only touches a
secret value (e.g. a re-issued PSK) does not change the hash — which is the
intended behavior: the LLM can never observe secret values, so they cannot
be part of its "view" of the object.
"""

from __future__ import annotations

import hashlib
import json
from typing import Any

from unifi_mcp.safety.redaction import normalize_key
from unifi_mcp.unifi.normalization import normalize

#: Volatile, runtime-only fields that must not affect the state hash.
#: Stored as *normalized* names (lower-cased, separators stripped) so that
#: matching is case-insensitive like the rest of the safety layer.
VOLATILE_FIELDS: frozenset[str] = frozenset(
    normalize_key(name)
    for name in (
        "lastSeen",
        "connectedAt",
        "disconnectedAt",
        "uptimeSec",
        "clientCount",
        "stationCount",
        "associatedClients",
        "cpuUtilizationPct",
        "memoryUtilizationPct",
        "txRateBps",
        "rxRateBps",
        "txBytes",
        "rxBytes",
    )
)


def _strip_volatile(value: Any) -> Any:
    """Recursively remove :data:`VOLATILE_FIELDS` from dicts and lists."""
    if isinstance(value, dict):
        return {
            key: _strip_volatile(item)
            for key, item in value.items()
            if not (isinstance(key, str) and normalize_key(key) in VOLATILE_FIELDS)
        }
    if isinstance(value, list):
        return [_strip_volatile(item) for item in value]
    return value


def canonical_json(value: Any) -> str:
    """Canonical JSON: sorted keys, compact separators, no volatile fields."""
    return json.dumps(
        _strip_volatile(value),
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
    )


def compute_state_hash(value: Any) -> str:
    """Return the ``sha256:<hex>`` state hash of a UniFi object.

    *value* may be the raw API payload or an already normalized detail
    representation; normalization is idempotent, so both yield the same
    hash. The hash covers the config-relevant, secret-free representation
    only.
    """
    normalized = normalize(value, level="detail")
    digest = hashlib.sha256(canonical_json(normalized).encode("utf-8")).hexdigest()
    return f"sha256:{digest}"
