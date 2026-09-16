"""Redaction of secret values from UniFi payloads before LLM output.

Design doc §8: the MCP must never hand passwords, WiFi PSKs, tokens, API
keys, etc. to the LLM. This module implements case-insensitive, recursive,
field-name-based redaction.

Matching is done on the *normalized* key (lower-cased, with ``_``/``-``
stripped), so ``apiKey``, ``api_key`` and ``API-Key`` all match. A key is
redacted when:

* its normalized name equals a forbidden name (e.g. ``password``,
  ``passphrase``, ``psk``, ``secret``, ``token``, ``apikey``,
  ``privatekey``, ``credential``, ``authorization``, ``presharedkey``), or
* its normalized name *properly ends* in a forbidden core (e.g. ``wpaPsk`` /
  ``wpa_psk`` end in ``psk``, ``x_passphrase`` ends in ``passphrase``,
  ``clientSecret`` ends in ``secret``, ``accessToken`` ends in ``token``).

Over-redaction is intentional and safe: a field whose name looks like a
secret is hidden even if, in some context, it held a non-secret value.
"""

from __future__ import annotations

from typing import Any

#: Value substituted for every redacted secret.
REDACTED = "[REDACTED]"

#: Forbidden normalized field names (exact match).
_FORBIDDEN_EXACT: frozenset[str] = frozenset(
    {
        "password",
        "passphrase",
        "psk",
        "secret",
        "token",
        "apikey",
        "privatekey",
        "credential",
        "authorization",
        "presharedkey",
    }
)

#: Forbidden cores used for the "properly ends with" rule.
_SUFFIX_CORES: tuple[str, ...] = (
    "password",
    "passphrase",
    "psk",
    "secret",
    "token",
    "apikey",
    "privatekey",
    "credential",
    "authorization",
)

#: Public, normalized forbidden names (exact set) for tests/docs.
FORBIDDEN_NAMES: frozenset[str] = frozenset(_FORBIDDEN_EXACT)


def normalize_key(key: str) -> str:
    """Normalize a field name for matching: lower-case, separators stripped.

    ``apiKey`` / ``api_key`` / ``API-Key`` all normalize to ``apikey``.
    """
    return "".join(ch for ch in key.lower() if ch.isalnum())


def is_secret_key(key: object) -> bool:
    """Return ``True`` when *key* names a field that must be redacted."""
    if not isinstance(key, str):
        return False
    norm = normalize_key(key)
    if not norm:
        return False
    if norm in _FORBIDDEN_EXACT:
        return True
    return any(len(norm) > len(core) and norm.endswith(core) for core in _SUFFIX_CORES)


def redact(value: Any) -> Any:
    """Return a copy of *value* with all secret values replaced by ``REDACTED``.

    Dicts and lists are traversed recursively. Only dict *keys* are
    inspected; a matching key's value is replaced (the key itself is kept so
    the LLM sees that a redacted field exists). The input is never mutated.
    Non-container values are returned unchanged.
    """
    if isinstance(value, dict):
        out: dict[Any, Any] = {}
        for key, item in value.items():
            if is_secret_key(key):
                out[key] = REDACTED
            else:
                out[key] = redact(item)
        return out
    if isinstance(value, list):
        return [redact(item) for item in value]
    return value
