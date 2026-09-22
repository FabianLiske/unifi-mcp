"""Unit tests for unifi_mcp.safety.guards (design §14/§16/§42)."""

from __future__ import annotations

from typing import Any

import pytest

from unifi_mcp.safety.guards import (
    apply_patch,
    assert_user_defined,
    verify_expected_state,
)
from unifi_mcp.safety.state_hash import compute_state_hash
from unifi_mcp.tools.errors import InvalidValueError, StateMismatchError


class _StubClient:
    """Minimal async client: returns a fixed payload for every GET."""

    def __init__(self, payload: Any) -> None:
        self._payload = payload
        self.get_calls: list[str] = []

    async def get(self, path: str, *, params: dict[str, Any] | None = None) -> Any:
        self.get_calls.append(path)
        return self._payload


def _widget(**overrides: object) -> dict[str, Any]:
    base: dict[str, Any] = {
        "id": "widget-1",
        "name": "Test Widget",
        "description": "a widget",
        "metadata": {"origin": "USER_DEFINED"},
    }
    base.update(overrides)
    return base


async def test_verify_expected_state_ok_returns_raw() -> None:
    raw = _widget()
    stub = _StubClient(raw)
    result = await verify_expected_state(
        stub, "/sites/s/widgets/widget-1", compute_state_hash(raw), resource="widget"
    )
    # returns the RAW (pre-normalization) object: it still carries metadata.
    assert result == raw
    assert "metadata" in result
    assert stub.get_calls == ["/sites/s/widgets/widget-1"]


async def test_verify_expected_state_mismatch_raises_with_current_hash() -> None:
    raw = _widget()
    stub = _StubClient(raw)
    stale = "sha256:" + "0" * 64
    with pytest.raises(StateMismatchError) as excinfo:
        await verify_expected_state(stub, "/sites/s/widgets/widget-1", stale, resource="widget")
    err = excinfo.value
    assert err.to_dict()["error"] == "state_changed"
    assert err.to_dict()["resource"] == "widget"
    # carries the CURRENT hash so the LLM can retry immediately.
    assert err.to_dict()["current_state_hash"] == compute_state_hash(raw)


async def test_verify_expected_state_always_fetches_fresh() -> None:
    raw = _widget()
    stub = _StubClient(raw)
    h = compute_state_hash(raw)
    await verify_expected_state(stub, "/p", h, resource="w")
    await verify_expected_state(stub, "/p", h, resource="w")
    assert stub.get_calls == ["/p", "/p"]  # two fresh reads, never cached


def test_assert_user_defined_ok() -> None:
    assert_user_defined(_widget(), resource="widget")  # no raise


@pytest.mark.parametrize(
    "raw",
    [
        _widget(metadata={"origin": "SYSTEM"}),
        _widget(metadata={"origin": "DERIVED"}),
        _widget(metadata={}),  # missing origin -> fails closed
        {"id": "w", "name": "no metadata"},  # metadata missing -> fails closed
        {"name": "no metadata"},
        "not-a-dict",
    ],
)
def test_assert_user_defined_rejects_non_user_defined(raw: Any) -> None:
    with pytest.raises(InvalidValueError) as excinfo:
        assert_user_defined(raw, resource="widget")
    data = excinfo.value.to_dict()
    assert data["error"] == "validation"
    assert data["field"] == "metadata.origin"
    assert data["allowed"] == ["USER_DEFINED"]


def test_apply_patch_merges_allowed_fields() -> None:
    current = _widget(description="old", name="old-name")
    merged, diff = apply_patch(
        current,
        {"description": "new", "name": "new-name"},
        allowed_fields=frozenset({"description", "name"}),
    )
    assert merged["description"] == "new"
    assert merged["name"] == "new-name"
    # untouched fields are preserved from the current object.
    assert merged["id"] == "widget-1"
    assert diff == {
        "description": {"old": "old", "new": "new"},
        "name": {"old": "old-name", "new": "new-name"},
    }


def test_apply_patch_does_not_mutate_current() -> None:
    current = _widget(description="old")
    _ = apply_patch(current, {"description": "new"}, allowed_fields=frozenset({"description"}))
    assert current["description"] == "old"


def test_apply_patch_adds_missing_fields() -> None:
    current = {"id": "w"}
    merged, diff = apply_patch(
        current, {"description": "x"}, allowed_fields=frozenset({"description"})
    )
    assert merged["description"] == "x"
    assert diff["description"] == {"old": None, "new": "x"}


def test_apply_patch_empty_changes_is_noop() -> None:
    current = _widget()
    merged, diff = apply_patch(current, {}, allowed_fields=frozenset({"description"}))
    assert merged == current
    assert diff == {}


def test_apply_patch_rejects_forbidden_field() -> None:
    current = _widget()
    with pytest.raises(InvalidValueError) as excinfo:
        apply_patch(
            current,
            {"description": "ok", "name": "hijack"},
            allowed_fields=frozenset({"description"}),
        )
    data = excinfo.value.to_dict()
    assert data["error"] == "validation"
    assert data["field"] == "changes"
    # the offending field is named, and the allowlist is offered.
    assert data["value"] == "name"
    assert data["allowed"] == ["description"]


def test_apply_patch_rejects_before_any_application() -> None:
    # No partial application: a forbidden field means nothing is merged.
    current = _widget(description="old")
    try:
        apply_patch(
            current,
            {"description": "new", "name": "hijack"},
            allowed_fields=frozenset({"description"}),
        )
    except InvalidValueError:
        pass
    else:
        pytest.fail("expected InvalidValueError")
