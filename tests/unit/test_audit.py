"""Unit tests for unifi_mcp.safety.audit (design §27)."""

from __future__ import annotations

import json
from typing import Any

import pytest

from unifi_mcp.safety.audit import AuditLog, get_audit_log, reset_audit_log


@pytest.fixture(autouse=True)
def _isolate_audit_singleton():
    reset_audit_log()
    yield
    reset_audit_log()


def _event_fields() -> dict[str, Any]:
    return {
        "tool": "update_widget",
        "object_id": "widget-1",
        "object_name": "Test Widget",
        "changes": {"description": {"old": "a", "new": "b"}},
        "result": "ok",
        "site_id": "site-default",
    }


def test_record_returns_full_event() -> None:
    audit = AuditLog()
    event = audit.record(**_event_fields())
    assert event["event"] == "unifi_write"
    assert event["tool"] == "update_widget"
    assert event["site_id"] == "site-default"
    assert event["object_id"] == "widget-1"
    assert event["object_name"] == "Test Widget"
    assert event["changes"] == {"description": {"old": "a", "new": "b"}}
    assert event["result"] == "ok"
    assert "timestamp" in event
    assert "request_id" in event


def test_record_writes_jsonl_line(tmp_path) -> None:
    path = tmp_path / "audit.jsonl"
    audit = AuditLog(str(path))
    audit.record(**_event_fields())
    audit.record(
        tool="update_widget",
        object_id="widget-1",
        object_name="Test Widget",
        changes={},
        result="state_changed",
        site_id="site-default",
    )
    lines = path.read_text(encoding="utf-8").splitlines()
    assert len(lines) == 2
    first = json.loads(lines[0])
    second = json.loads(lines[1])
    assert first["result"] == "ok"
    assert second["result"] == "state_changed"
    # each line is independently valid JSON with a stable key order.
    assert lines[0] == json.dumps(first, ensure_ascii=False, sort_keys=True)


def test_record_appends(tmp_path) -> None:
    path = tmp_path / "audit.jsonl"
    path.write_text(json.dumps({"event": "unifi_write", "tool": "earlier"}) + "\n")
    audit = AuditLog(str(path))
    audit.record(**_event_fields())
    lines = path.read_text(encoding="utf-8").splitlines()
    assert len(lines) == 2
    assert json.loads(lines[0])["tool"] == "earlier"
    assert json.loads(lines[1])["tool"] == "update_widget"


def test_secrets_are_redacted_in_event(tmp_path) -> None:
    path = tmp_path / "audit.jsonl"
    audit = AuditLog(str(path))
    event = audit.record(
        tool="update_wifi",
        object_id="bc-1",
        object_name="SSID",
        changes={"wpaPsk": {"old": "hunter2", "new": "hunter3"}},
        result="ok",
    )
    # The whole value of a secret-named field is redacted (over-redaction is
    # intentional and safe, design §8): the returned event and the written
    # line must both hide the PSK, and no plaintext may reach the file.
    assert event["changes"]["wpaPsk"] == "[REDACTED]"
    text = path.read_text(encoding="utf-8")
    written = json.loads(text.splitlines()[0])
    assert written["changes"]["wpaPsk"] == "[REDACTED]"
    assert "hunter2" not in text
    assert "hunter3" not in text


def test_unopenable_path_falls_back_to_stdout(tmp_path) -> None:
    # Parent directory does not exist -> open() raises -> fallback, no crash.
    audit = AuditLog(str(tmp_path / "does-not-exist" / "audit.jsonl"))
    assert audit.file_path is None
    event = audit.record(**_event_fields())  # must not raise
    assert event["result"] == "ok"


def test_file_write_failure_falls_back_to_stdout(tmp_path) -> None:
    path = tmp_path / "audit.jsonl"
    audit = AuditLog(str(path))
    assert audit.file_path is not None

    class _BrokenFile:
        name = str(path)

        def write(self, _data: str) -> None:
            raise OSError("disk full")

        def flush(self) -> None:
            raise OSError("disk full")

        def close(self) -> None:
            return None

    audit._file = _BrokenFile()  # type: ignore[assignment]
    event = audit.record(**_event_fields())  # must not raise
    assert event["result"] == "ok"
    # after a write failure the file sink is dropped (stdout fallback).
    assert audit.file_path is None


def test_close_is_idempotent(tmp_path) -> None:
    audit = AuditLog(str(tmp_path / "audit.jsonl"))
    audit.close()
    audit.close()  # no error on double close


def test_get_audit_log_is_singleton(tmp_path) -> None:
    first = get_audit_log(str(tmp_path / "a.jsonl"))
    second = get_audit_log()
    assert first is second
    reset_audit_log()
    third = get_audit_log(str(tmp_path / "b.jsonl"))
    assert third is not first


def test_get_audit_log_path_is_process_static(tmp_path) -> None:
    # A later call with a different path must NOT re-route an existing sink.
    first = get_audit_log(str(tmp_path / "a.jsonl"))
    second = get_audit_log(str(tmp_path / "b.jsonl"))
    assert second is first
    assert second.file_path == str(tmp_path / "a.jsonl")
