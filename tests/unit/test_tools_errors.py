"""Unit tests for unifi_mcp.tools.errors."""

from __future__ import annotations

import pytest

from unifi_mcp.tools.errors import (
    AmbiguousMatchError,
    NotFoundError,
    ResponseTooLargeError,
    ToolError,
    check_response_size,
)


def test_not_found_shape_and_key_order() -> None:
    err = NotFoundError("client", "wk-5")
    data = err.to_dict()
    assert data == {
        "error": "not_found",
        "resource": "client",
        "query": "wk-5",
        "message": "No client matched the supplied identifier.",
    }
    # Code first, message last (design §30).
    assert list(data.keys()) == ["error", "resource", "query", "message"]


def test_not_found_custom_message() -> None:
    err = NotFoundError("device", "USG", message="No device with that name.")
    assert err.to_dict()["message"] == "No device with that name."


def test_ambiguous_match_shape() -> None:
    matches = [
        {"id": "c1", "hostname": "wk-5", "ip": "172.26.10.5"},
        {"id": "c2", "hostname": "wk-5-old", "ip": "172.26.10.9"},
    ]
    err = AmbiguousMatchError(matches)
    data = err.to_dict()
    assert data["error"] == "ambiguous_match"
    assert data["matches"] == matches
    assert data["message"] == "Multiple resources matched; use an exact identifier."
    assert list(data.keys()) == ["error", "matches", "message"]


def test_response_too_large_shape() -> None:
    err = ResponseTooLargeError()
    assert err.to_dict() == {
        "error": "response_too_large",
        "message": "Narrow the query or use pagination.",
    }


def test_tool_error_is_catchable_exception() -> None:
    with pytest.raises(ToolError):
        raise NotFoundError("site", "default")
    with pytest.raises(ToolError):
        raise ResponseTooLargeError()


def test_check_response_size_returns_bytes() -> None:
    assert check_response_size({"a": 1}, max_bytes=100) == 7


def test_check_response_size_counts_utf8_bytes() -> None:
    # `{"s":"` + é (2 UTF-8 bytes) + `"}` = 10 bytes.
    assert check_response_size({"s": "é"}, max_bytes=100) == 10


def test_check_response_size_raises_when_exceeded() -> None:
    with pytest.raises(ResponseTooLargeError):
        check_response_size({"a": 1}, max_bytes=6)


def test_check_response_size_large_payload_raises() -> None:
    payload = {"items": [{"id": i, "pad": "x" * 50} for i in range(1000)]}
    with pytest.raises(ResponseTooLargeError):
        check_response_size(payload, max_bytes=1000)


def test_check_response_size_at_limit_is_ok() -> None:
    # Exactly at the limit is allowed (only strictly larger fails).
    assert check_response_size({"a": 1}, max_bytes=7) == 7
