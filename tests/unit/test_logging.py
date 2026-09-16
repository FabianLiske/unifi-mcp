"""Unit tests for unifi_mcp.observability.logging."""

from __future__ import annotations

import io
import json

from unifi_mcp.observability import logging as app_logging


def _logger(fmt: str = "json") -> tuple[io.StringIO, object]:
    app_logging.bind_request_id(None)
    buf = io.StringIO()
    app_logging.setup_logging("INFO", fmt, stream=buf)
    return buf, app_logging.get_logger("test")


def _last_json(buf: io.StringIO) -> dict:
    return json.loads(buf.getvalue().strip().splitlines()[-1])


def test_json_output_fields() -> None:
    buf, logger = _logger("json")
    app_logging.bind_request_id("req-123")
    logger.info("hello world")
    data = _last_json(buf)
    assert data["event"] == "hello world"
    assert data["request_id"] == "req-123"
    assert data["level"] == "info"
    assert "timestamp" in data


def test_no_request_id_when_unbound() -> None:
    buf, logger = _logger("json")
    logger.info("no rid")
    data = _last_json(buf)
    assert "request_id" not in data


def test_redacts_sensitive_keys() -> None:
    buf, logger = _logger("json")
    logger.info("login", api_key="supersecret", password="hunter2", safe="ok")
    line = buf.getvalue().strip().splitlines()[-1]
    data = json.loads(line)
    assert data["api_key"] == app_logging.REDACTED
    assert data["password"] == app_logging.REDACTED
    assert data["safe"] == "ok"
    assert "supersecret" not in line
    assert "hunter2" not in line


def test_nested_redaction() -> None:
    buf, logger = _logger("json")
    logger.info("evt", client={"mac": "aa:bb", "psk": "secret123", "name": "pc"})
    line = buf.getvalue().strip().splitlines()[-1]
    data = json.loads(line)
    assert data["client"]["psk"] == app_logging.REDACTED
    assert data["client"]["name"] == "pc"
    assert "secret123" not in line


def test_text_format() -> None:
    buf, logger = _logger("text")
    logger.info("visible message")
    assert "visible message" in buf.getvalue()


def test_new_request_id_unique() -> None:
    assert app_logging.new_request_id() != app_logging.new_request_id()
