"""Shared pytest fixtures."""

from __future__ import annotations

import pytest

ENV_KEYS = (
    "UNIFI_BASE_URL",
    "UNIFI_API_KEY",
    "UNIFI_SITE_ID",
    "UNIFI_TLS_MODE",
    "UNIFI_CA_BUNDLE",
    "UNIFI_TIMEOUT_SECONDS",
    "UNIFI_CONNECT_TIMEOUT_SECONDS",
    "UNIFI_MAX_RETRIES",
    "MCP_AUTH_TOKEN",
    "MCP_BIND_HOST",
    "MCP_BIND_PORT",
    "LOG_LEVEL",
    "LOG_FORMAT",
    "ENABLE_WRITE_TOOLS",
    "ENABLE_ACTION_TOOLS",
    "ENABLE_DELETE_TOOLS",
    "MAX_LIST_ITEMS",
    "MAX_TOOL_RESPONSE_BYTES",
)


@pytest.fixture
def clean_env(monkeypatch: pytest.MonkeyPatch) -> pytest.MonkeyPatch:
    """Remove all unifi-mcp env vars so tests are deterministic."""
    for key in ENV_KEYS:
        monkeypatch.delenv(key, raising=False)
    return monkeypatch
