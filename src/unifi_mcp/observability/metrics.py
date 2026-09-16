"""Prometheus metrics for the MCP server (skeleton).

Label cardinality is intentionally bounded: only ``tool`` / ``status`` /
``method`` / ``endpoint_group``. Never put MACs, client names, object IDs, or
URLs containing object IDs into labels.
"""

from __future__ import annotations

from prometheus_client import Counter, Histogram

MCP_REQUESTS_TOTAL = Counter(
    "unifi_mcp_requests_total",
    "MCP tool calls, by tool and status.",
    labelnames=("tool", "status"),
)
MCP_REQUEST_DURATION_SECONDS = Histogram(
    "unifi_mcp_request_duration_seconds",
    "MCP tool call duration in seconds, by tool.",
    labelnames=("tool",),
)
UNIFI_REQUESTS_TOTAL = Counter(
    "unifi_mcp_unifi_requests_total",
    "Requests to the UniFi API, by method and status.",
    labelnames=("method", "status"),
)
UNIFI_REQUEST_DURATION_SECONDS = Histogram(
    "unifi_mcp_unifi_request_duration_seconds",
    "UniFi API request duration in seconds, by endpoint group.",
    labelnames=("endpoint_group",),
)
WRITE_REQUESTS_TOTAL = Counter(
    "unifi_mcp_write_requests_total",
    "Write (mutating) MCP tool calls, by tool and status.",
    labelnames=("tool", "status"),
)
AUTH_FAILURES_TOTAL = Counter(
    "unifi_mcp_auth_failures_total",
    "Failed MCP authentication attempts.",
)


def record_mcp_request(tool: str, status: str, duration_seconds: float) -> None:
    """Record one MCP tool call and its duration."""
    MCP_REQUESTS_TOTAL.labels(tool=tool, status=status).inc()
    MCP_REQUEST_DURATION_SECONDS.labels(tool=tool).observe(duration_seconds)


def record_unifi_request(
    method: str, status: str, duration_seconds: float, endpoint_group: str
) -> None:
    """Record one outbound UniFi API request and its duration."""
    UNIFI_REQUESTS_TOTAL.labels(method=method, status=status).inc()
    UNIFI_REQUEST_DURATION_SECONDS.labels(endpoint_group=endpoint_group).observe(duration_seconds)


def record_write_request(tool: str, status: str) -> None:
    """Record one write (mutating) MCP tool call."""
    WRITE_REQUESTS_TOTAL.labels(tool=tool, status=status).inc()


def record_auth_failure() -> None:
    """Record one failed MCP authentication attempt."""
    AUTH_FAILURES_TOTAL.inc()
