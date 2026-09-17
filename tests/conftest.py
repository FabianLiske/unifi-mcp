"""Shared pytest fixtures."""

from __future__ import annotations

import asyncio
import contextlib
import io
import json
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

import anyio
import httpx
import pytest

from unifi_mcp.app import create_app
from unifi_mcp.config import Settings
from unifi_mcp.observability import logging as app_logging
from unifi_mcp.unifi.client import UniFiClient, build_http_client

FIXTURES_DIR = Path(__file__).parent / "fixtures"

#: Paging envelope for list endpoints that return no items (capability probes).
EMPTY_PAGE: dict[str, Any] = {"offset": 0, "limit": 1, "count": 0, "totalCount": 0, "data": []}

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


@pytest.fixture
def load_fixture():
    """Load a JSON fixture from tests/fixtures/."""

    def _load(name: str) -> Any:
        return json.loads((FIXTURES_DIR / name).read_text(encoding="utf-8"))

    return _load


@pytest.fixture(autouse=True)
def _quiet_logging() -> pytest.Iterator[None]:
    """Keep structlog silent (and secret-free) unless a test opts in."""
    app_logging.setup_logging("CRITICAL", "json", stream=io.StringIO())
    yield
    app_logging.setup_logging("INFO", "json")


@pytest.fixture
def make_settings(clean_env):
    """Factory for deterministic Settings (no .env file, no env vars)."""

    def _make(**overrides: object) -> Settings:
        base: dict[str, object] = {
            "unifi_base_url": "http://gateway.test",
            "unifi_api_key": "test-api-key",
            "unifi_site_id": None,
            "unifi_tls_mode": "extract-once",
            "unifi_ca_bundle": None,
            "unifi_timeout_seconds": 5.0,
            "unifi_connect_timeout_seconds": 1.0,
            "unifi_max_retries": 2,
            "mcp_auth_token": "test-mcp-token",
        }
        base.update(overrides)
        return Settings(_env_file=None, **base)

    return _make


@pytest.fixture
async def make_client(make_settings):
    """Factory for UniFiClient instances (respx intercepts the transport)."""
    created: list[UniFiClient] = []

    async def _make(settings: Settings | None = None) -> UniFiClient:
        resolved = settings or make_settings()
        client = UniFiClient(
            resolved,
            build_http_client(resolved, True),
            backoff_base=0.0,
        )
        created.append(client)
        return client

    yield _make
    for client in created:
        await client.aclose()


@asynccontextmanager
async def asgi_lifespan(app: Any) -> AsyncIterator[None]:
    """Run an ASGI app's lifespan protocol in-process.

    ``httpx.ASGITransport`` only drives HTTP scopes, so the Starlette lifespan
    (which starts the MCP session manager) would never run otherwise.

    The app is driven from a single background task so that the session
    manager's internal task group is entered and exited within one task
    (pytest-asyncio runs fixture setup/teardown in different tasks, which a
    spanning anyio task group cannot survive).
    """
    to_app_send, to_app_recv = anyio.create_memory_object_stream(max_buffer_size=16)
    from_app_send, from_app_recv = anyio.create_memory_object_stream(max_buffer_size=16)

    async def receive() -> dict[str, Any]:
        return await to_app_recv.receive()

    async def send(message: dict[str, Any]) -> None:
        await from_app_send.send(message)

    async def run_app() -> None:
        await app({"type": "lifespan"}, receive, send)

    loop = asyncio.get_running_loop()
    app_task = loop.create_task(run_app())
    try:
        await to_app_send.send({"type": "lifespan.startup"})
        message = await from_app_recv.receive()
        if message["type"] != "lifespan.startup.complete":
            raise RuntimeError(f"ASGI lifespan startup failed: {message}")
        try:
            yield
        finally:
            await to_app_send.send({"type": "lifespan.shutdown"})
    finally:
        if not app_task.done():
            try:
                await asyncio.wait_for(app_task, timeout=5)
            except BaseException:
                app_task.cancel()
        with contextlib.suppress(BaseException):
            await app_task


class FakeUniFi:
    """State of the mocked UniFi API: the site id plus every devices request."""

    def __init__(self, site_id: str) -> None:
        self.site_id = site_id
        self.devices_requests: list[httpx.Request] = []


@pytest.fixture
def fake_unifi(respx_mock, load_fixture) -> FakeUniFi:
    """Mock the fake UniFi API surface used by the WP-7 tools.

    Returns a :class:`FakeUniFi` with the site id used for the site-scoped
    routes and the captured ``/devices`` requests. ``GET /info`` may be
    mocked additionally by :func:`mcp_app` — both mocks serve the same
    application-info payload.
    """
    fake = FakeUniFi("site-default")
    base = "http://gateway.test/proxy/network/integration/v1"
    respx_mock.get(f"{base}/info").mock(
        return_value=httpx.Response(200, json=load_fixture("application_info.json"))
    )
    respx_mock.get(f"{base}/sites").mock(
        return_value=httpx.Response(200, json=load_fixture("sites.json"))
    )

    def _devices(request: httpx.Request) -> httpx.Response:
        fake.devices_requests.append(request)
        return httpx.Response(200, json=load_fixture("devices.json"))

    respx_mock.get(f"{base}/sites/{fake.site_id}/devices").mock(side_effect=_devices)
    for resource in (
        "clients",
        "networks",
        "wifi/broadcasts",
        "acl-rules",
        "traffic-matching-lists",
    ):
        respx_mock.get(f"{base}/sites/{fake.site_id}/{resource}").mock(
            return_value=httpx.Response(200, json=EMPTY_PAGE)
        )
    respx_mock.get(f"{base}/sites/{fake.site_id}/firewall/zones").mock(
        return_value=httpx.Response(
            400,
            json={
                "code": "api.firewall.zone-based-firewall-not-configured",
                "message": "Zone-based firewall is not configured.",
            },
        )
    )
    return fake


@pytest.fixture
async def mcp_app(respx_mock, make_client, make_settings):
    """The full ASGI app (auth + MCP + health) with a pre-built client, lifespan running.

    ``GET /info`` is mocked so ``/readyz`` resolves deterministically without a
    real gateway.
    """
    settings = make_settings()
    client = await make_client(settings)
    respx_mock.get("http://gateway.test/proxy/network/integration/v1/info").mock(
        return_value=httpx.Response(200, json={"applicationVersion": "10.6.101"})
    )
    app = create_app(settings, client=client)
    async with asgi_lifespan(app):
        yield app, settings
