"""Unit tests for unifi_mcp.safety.state_hash (design §14)."""

from __future__ import annotations

import re

import pytest

from unifi_mcp.safety.state_hash import (
    VOLATILE_FIELDS,
    canonical_json,
    compute_state_hash,
)

_HASH_FORMAT = re.compile(r"^sha256:[0-9a-f]{64}$")


def _widget(**overrides: object) -> dict[str, object]:
    base: dict[str, object] = {
        "id": "widget-1",
        "name": "Test Widget",
        "type": "DASHBOARD",
        "description": "a widget",
        "metadata": {"origin": "USER_DEFINED", "creationTime": 1},
        "revision": 3,
        "etag": "0x3",
    }
    base.update(overrides)
    return base


def test_hash_format() -> None:
    assert _HASH_FORMAT.match(compute_state_hash(_widget()))


def test_hash_is_stable() -> None:
    assert compute_state_hash(_widget()) == compute_state_hash(_widget())


def test_hash_is_key_order_independent() -> None:
    a = {"a": 1, "b": {"x": 1, "y": [1, 2]}, "c": "z"}
    b = {"c": "z", "b": {"y": [1, 2], "x": 1}, "a": 1}
    assert compute_state_hash(a) == compute_state_hash(b)


def test_config_change_changes_hash() -> None:
    assert compute_state_hash(_widget()) != compute_state_hash(_widget(description="a new widget"))


def test_id_is_not_a_config_field_but_still_hashes() -> None:
    # The id is stable identity; a change to it is a different object.
    assert compute_state_hash(_widget()) != compute_state_hash(_widget(id="widget-2"))


def test_internal_fields_are_excluded() -> None:
    # metadata / etag / revision are stripped by normalization, so they must
    # not affect the hash.
    base = compute_state_hash(_widget())
    assert base == compute_state_hash(_widget(revision=99, etag="0x99"))
    assert base == compute_state_hash(
        _widget(metadata={"origin": "SYSTEM", "creationTime": 999})
    )


def test_secret_value_change_does_not_change_hash() -> None:
    # Secrets are redacted before hashing: the *value* of a secret field is
    # invisible to the LLM's view, so a re-issued PSK must not cause a
    # spurious conflict. (The *presence* of the field still matters.)
    base = compute_state_hash(_widget(**{"wpaPsk": "hunter2"}))
    assert base == compute_state_hash(_widget(**{"wpaPsk": "hunter3"}))


def test_secret_field_presence_changes_hash() -> None:
    # Adding/removing a redacted field changes the object's shape, so the
    # hash changes — only the secret *value* is hidden.
    assert compute_state_hash(_widget()) != compute_state_hash(_widget(**{"wpaPsk": "x"}))


def test_volatile_field_change_does_not_change_hash() -> None:
    base = compute_state_hash(_widget())
    assert base == compute_state_hash(_widget(lastSeen=1700000000))
    assert base == compute_state_hash(_widget(lastSeen=1799999999, clientCount=42))


def test_non_volatile_runtime_field_changes_hash() -> None:
    # A field that is not on the volatile list still matters.
    assert compute_state_hash(_widget()) != compute_state_hash(_widget(name="Renamed"))


def test_volatile_fields_are_normalized() -> None:
    # The set stores normalized names, matching is case/separator insensitive.
    assert "lastseen" in VOLATILE_FIELDS
    assert "clientcount" in VOLATILE_FIELDS
    # A differently-cased volatile key is still stripped.
    a = compute_state_hash({"LastSeen": 1, "name": "x"})
    b = compute_state_hash({"lastseen": 2, "name": "x"})
    assert a == b


def test_canonical_json_is_sorted_and_compact() -> None:
    assert canonical_json({"b": 1, "a": {"y": 2, "x": 1}}) == '{"a":{"x":1,"y":2},"b":1}'


def test_raw_and_normalized_yield_same_hash() -> None:
    # compute_state_hash normalizes internally; feeding it an already
    # normalized value must give the identical hash (idempotency).
    raw = _widget()
    from unifi_mcp.unifi.normalization import normalize

    normalized = normalize(raw, level="detail")
    assert compute_state_hash(raw) == compute_state_hash(normalized)


def test_volatile_fields_are_stripped_recursively() -> None:
    # Volatile fields nested inside lists/dicts are removed too.
    a = {"clients": [{"lastSeen": 1, "ip": "1.2.3.4"}], "name": "x"}
    b = {"clients": [{"lastSeen": 99, "ip": "1.2.3.4"}], "name": "x"}
    assert compute_state_hash(a) == compute_state_hash(b)


def test_empty_and_scalar_values() -> None:
    assert compute_state_hash({}) == compute_state_hash({})
    assert _HASH_FORMAT.match(compute_state_hash({"name": "x"}))


def test_distinct_values_distinct_hashes() -> None:
    hashes = {compute_state_hash(_widget(name=f"n{i}")) for i in range(20)}
    assert len(hashes) == 20


@pytest.mark.parametrize("value", [None, 42, "plain", True])
def test_non_dict_values_hash_cleanly(value: object) -> None:
    assert _HASH_FORMAT.match(compute_state_hash(value))
