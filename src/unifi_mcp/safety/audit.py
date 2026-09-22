"""Write audit log (design §27 "Write Audit Log", §41 layer 8).

Every write *attempt* — successful or rejected — is recorded as one event:

``{"event": "unifi_write", "tool", "site_id", "object_id", "object_name",
"changes": {field: {"old", "new"}}, "result", "request_id"}``

The default sink is stdout (the structured logger, on to the cluster
logging stack). When ``AUDIT_LOG_PATH`` is set, events are additionally
written as JSONL (one event per line, flushed per event) to that file —
e.g. a host mount at ``/var/log/unifi-mcp/audit.jsonl``. The parent
directory must exist; if the file cannot be opened or a write fails, the
audit falls back to stdout with a prominent warning: a broken audit sink
must never take the server down.

All events pass through the shared secret redaction before they are
written (design §27: "Sensitive Values redigieren", §33).
"""

from __future__ import annotations

import contextlib
import json
from datetime import UTC, datetime
from typing import Any, TextIO

from unifi_mcp.observability.logging import get_logger, get_request_id
from unifi_mcp.safety.redaction import redact

logger = get_logger(__name__)


class AuditLog:
    """Appends redacted write-audit events as JSONL to a file and/or stdout."""

    def __init__(self, path: str | None = None) -> None:
        self._file: TextIO | None = None
        if path:
            try:
                # The handle stays open for the process lifetime (a long-lived
                # sink), so it cannot be a context manager (SIM115).
                self._file = open(path, "a", encoding="utf-8")  # noqa: SIM115
            except OSError as exc:
                logger.warning(
                    "audit log: cannot open %s; falling back to stdout",
                    path=path,
                    error=exc.__class__.__name__,
                )
                self._file = None

    @property
    def file_path(self) -> str | None:
        """The configured file path (``None`` when stdout-only or fell back)."""
        return None if self._file is None else getattr(self._file, "name", None)

    def record(
        self,
        *,
        tool: str,
        object_id: str,
        object_name: str | None,
        changes: dict[str, dict[str, Any]],
        result: str,
        site_id: str | None = None,
    ) -> dict[str, Any]:
        """Record one write attempt and return the redacted event dict."""
        event: dict[str, Any] = {
            "event": "unifi_write",
            "timestamp": datetime.now(UTC).isoformat(),
            "tool": tool,
            "site_id": site_id,
            "object_id": object_id,
            "object_name": object_name,
            "changes": changes,
            "result": result,
            "request_id": get_request_id(),
        }
        event = redact(event)
        self._emit(event)
        return event

    def _emit(self, event: dict[str, Any]) -> None:
        if self._file is not None:
            try:
                self._file.write(
                    json.dumps(event, ensure_ascii=False, sort_keys=True) + "\n"
                )
                self._file.flush()
                return
            except OSError as exc:
                logger.warning(
                    "audit log: file write failed; falling back to stdout",
                    error=exc.__class__.__name__,
                )
                with contextlib.suppress(OSError):
                    self._file.close()
                self._file = None
        fields = {key: value for key, value in event.items() if key != "event"}
        logger.info("unifi_write", **fields)

    def close(self) -> None:
        if self._file is not None:
            with contextlib.suppress(OSError):
                self._file.close()
            self._file = None


_audit_log: AuditLog | None = None
_audit_log_path: str | None = None


def get_audit_log(path: str | None = None) -> AuditLog:
    """Return the process-wide :class:`AuditLog` (created on first use).

    The path is only used when the instance is created; later calls with a
    different path return the existing instance (the configuration is
    process-static, like the settings).
    """
    global _audit_log, _audit_log_path
    if _audit_log is None:
        _audit_log_path = path
        _audit_log = AuditLog(path)
    return _audit_log


def reset_audit_log() -> None:
    """Close and drop the process-wide audit log (used by tests)."""
    global _audit_log, _audit_log_path
    if _audit_log is not None:
        _audit_log.close()
    _audit_log = None
    _audit_log_path = None
