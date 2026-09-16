"""Unit tests for unifi_mcp.observability.metrics."""

from __future__ import annotations

from prometheus_client import REGISTRY

from unifi_mcp.observability import metrics


def _counter(name: str, labels: dict[str, str] | None = None) -> float:
    value = REGISTRY.get_sample_value(name, labels or {})
    return 0.0 if value is None else value


def test_metrics_registered() -> None:
    from prometheus_client import Counter, Histogram

    assert isinstance(metrics.MCP_REQUESTS_TOTAL, Counter)
    assert isinstance(metrics.MCP_REQUEST_DURATION_SECONDS, Histogram)
    assert isinstance(metrics.UNIFI_REQUESTS_TOTAL, Counter)
    assert isinstance(metrics.UNIFI_REQUEST_DURATION_SECONDS, Histogram)
    assert isinstance(metrics.WRITE_REQUESTS_TOTAL, Counter)
    assert isinstance(metrics.AUTH_FAILURES_TOTAL, Counter)


def test_record_mcp_request() -> None:
    labels = {"tool": "list_devices", "status": "success"}
    before = _counter("unifi_mcp_requests_total", labels)
    metrics.record_mcp_request("list_devices", "success", 0.123)
    after = _counter("unifi_mcp_requests_total", labels)
    assert after - before == 1.0
    dur_count = REGISTRY.get_sample_value(
        "unifi_mcp_request_duration_seconds_count", {"tool": "list_devices"}
    )
    assert dur_count is not None and dur_count >= 1.0


def test_record_unifi_request() -> None:
    labels = {"method": "GET", "status": "200"}
    before = _counter("unifi_mcp_unifi_requests_total", labels)
    metrics.record_unifi_request("GET", "200", 0.05, "devices")
    after = _counter("unifi_mcp_unifi_requests_total", labels)
    assert after - before == 1.0


def test_record_write_request() -> None:
    labels = {"tool": "update_wifi", "status": "success"}
    before = _counter("unifi_mcp_write_requests_total", labels)
    metrics.record_write_request("update_wifi", "success")
    after = _counter("unifi_mcp_write_requests_total", labels)
    assert after - before == 1.0


def test_record_auth_failure() -> None:
    before = _counter("unifi_mcp_auth_failures_total", {})
    metrics.record_auth_failure()
    after = _counter("unifi_mcp_auth_failures_total", {})
    assert after - before == 1.0
