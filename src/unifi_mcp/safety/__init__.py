"""Safety guards: secret redaction, state hashing, write guards, audit log."""

from unifi_mcp.safety.audit import AuditLog, get_audit_log, reset_audit_log
from unifi_mcp.safety.guards import (
    apply_patch,
    assert_user_defined,
    verify_expected_state,
)
from unifi_mcp.safety.redaction import (
    FORBIDDEN_NAMES,
    REDACTED,
    is_secret_key,
    normalize_key,
    redact,
)
from unifi_mcp.safety.state_hash import (
    VOLATILE_FIELDS,
    canonical_json,
    compute_state_hash,
)

__all__ = [
    "REDACTED",
    "FORBIDDEN_NAMES",
    "is_secret_key",
    "normalize_key",
    "redact",
    "VOLATILE_FIELDS",
    "canonical_json",
    "compute_state_hash",
    "apply_patch",
    "assert_user_defined",
    "verify_expected_state",
    "AuditLog",
    "get_audit_log",
    "reset_audit_log",
]
