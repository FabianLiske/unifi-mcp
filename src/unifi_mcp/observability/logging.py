"""Structured logging (structlog) with request_id and secret redaction."""

from __future__ import annotations

import sys
import uuid
from collections.abc import Mapping, MutableMapping
from contextvars import ContextVar
from typing import Any, cast

import structlog

REDACTED = "[REDACTED]"

# Substrings (lowercased) that mark a log-event key as sensitive.
_SENSITIVE_KEY_PARTS = (
    "api_key",
    "apikey",
    "api-key",
    "authorization",
    "credential",
    "passwd",
    "password",
    "passphrase",
    "private_key",
    "privatekey",
    "psk",
    "secret",
    "token",
)

_MAX_REDACT_DEPTH = 6

# Numeric log levels (match stdlib and structlog).
_LEVELS = {"debug": 10, "info": 20, "warning": 30, "error": 40, "critical": 50}

_request_id: ContextVar[str | None] = ContextVar("request_id", default=None)


def new_request_id() -> str:
    """Generate a new request identifier."""
    return uuid.uuid4().hex


def bind_request_id(request_id: str | None) -> None:
    """Bind a request id to the current context (``None`` clears it)."""
    _request_id.set(request_id)


def get_request_id() -> str | None:
    """Return the request id bound to the current context, if any."""
    return _request_id.get()


def _is_sensitive_key(key: object) -> bool:
    if not isinstance(key, str):
        return False
    lowered = key.lower()
    return any(part in lowered for part in _SENSITIVE_KEY_PARTS)


def _redact_structure(value: Any, depth: int = 0) -> Any:
    if depth > _MAX_REDACT_DEPTH:
        return value
    if isinstance(value, dict):
        return {
            k: (REDACTED if _is_sensitive_key(k) else _redact_structure(v, depth + 1))
            for k, v in value.items()
        }
    if isinstance(value, (list, tuple)):
        return [_redact_structure(item, depth + 1) for item in value]
    return value


def _add_request_id(
    _logger: Any, _method_name: str, event_dict: MutableMapping[str, Any]
) -> Mapping[str, Any]:
    rid = _request_id.get()
    if rid is not None:
        event_dict.setdefault("request_id", rid)
    return event_dict


def _redact(
    _logger: Any, _method_name: str, event_dict: MutableMapping[str, Any]
) -> Mapping[str, Any]:
    return cast(Mapping[str, Any], _redact_structure(event_dict))


def _to_level(level: str) -> int:
    return _LEVELS.get(level.lower(), 20)


def setup_logging(level: str, log_format: str, stream: Any = None) -> None:
    """Configure structlog for the given level/format, writing to ``stream``."""
    out = stream if stream is not None else sys.stdout
    if log_format == "json":
        renderer: structlog.types.Processor = structlog.processors.JSONRenderer()
    else:
        renderer = structlog.dev.ConsoleRenderer()
    structlog.configure(
        processors=[
            structlog.stdlib.add_log_level,
            structlog.processors.TimeStamper(fmt="iso", utc=True),
            _add_request_id,
            structlog.processors.StackInfoRenderer(),
            structlog.processors.format_exc_info,
            _redact,
            renderer,
        ],
        logger_factory=structlog.PrintLoggerFactory(file=out),
        wrapper_class=structlog.make_filtering_bound_logger(_to_level(level)),
        cache_logger_on_first_use=False,
    )


def get_logger(name: str | None = None) -> structlog.BoundLogger:
    """Return a configured structlog logger."""
    return cast(structlog.BoundLogger, structlog.get_logger(name))
