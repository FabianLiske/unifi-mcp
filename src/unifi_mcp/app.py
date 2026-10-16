"""ASGI assembly with local readiness and lifespan-owned diagnostics.

The caller owns the UniFi client; the SDK owns MCP sessions. This module
starts/stops only the diagnostic task inside the SDK's existing lifespan.
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Awaitable, Callable, Mapping
from contextlib import asynccontextmanager
from typing import TYPE_CHECKING, Any, cast

from unifi_mcp.auth.middleware import BearerAuthMiddleware
from unifi_mcp.config import Settings, get_settings
from unifi_mcp.health import UpstreamMonitor
from unifi_mcp.observability import logging as app_logging
from unifi_mcp.safety.audit import get_audit_log
from unifi_mcp.server import ReadinessProbe, build_server, mcp_starlette
from unifi_mcp.unifi.client import UniFiClient
from unifi_mcp.unifi.models import ApplicationInfo

if TYPE_CHECKING:
    from starlette.applications import Starlette
    from starlette.types import Lifespan

    from unifi_mcp.tools.registry import ToolGroup

ASGIApp = Callable[[dict[str, Any], Any, Any], Awaitable[None]]


def create_app(
    settings: Settings,
    *,
    client: UniFiClient,
    groups: tuple[ToolGroup, ...] | None = None,
) -> ASGIApp:
    """Assemble the app; validated config and audit initialization precede ready."""
    readiness = ReadinessProbe(lambda: not client._http.is_closed)
    # A file sink failure deliberately falls back to stdout, per audit policy.
    get_audit_log(settings.audit_log_path or None)
    server = build_server(settings, client, readiness, groups=groups)
    starlette_app = mcp_starlette(server, settings)

    async def check_upstream() -> ApplicationInfo:
        # Adapter only: retain body cap/error mapping/metrics, but no retries
        # for diagnostic traffic. Client-core policy belongs to F05.
        data = await client._request("GET", "/info", retry=False)
        return ApplicationInfo.model_validate(data)

    monitor = UpstreamMonitor(
        check_upstream,
        interval_seconds=settings.upstream_check_interval_seconds,
        timeout_seconds=settings.upstream_check_timeout_seconds,
    )
    starlette_app.state.upstream_monitor = monitor
    sdk_lifespan = starlette_app.router.lifespan_context

    @asynccontextmanager
    async def lifespan(app: Starlette) -> AsyncIterator[Mapping[str, Any] | None]:
        # Preserve the SDK session-manager lifecycle and its yielded state.
        async with sdk_lifespan(app) as state:
            monitor.start()
            readiness.started = True
            try:
                yield state
            finally:
                readiness.started = False
                await monitor.stop()

    # Starlette types this as a union of two callable return types; the
    # wrapper preserves either value at runtime (including None, not {}).
    starlette_app.router.lifespan_context = cast("Lifespan[Starlette]", lifespan)
    return BearerAuthMiddleware(
        starlette_app, expected_token=settings.mcp_auth_token.get_secret_value()
    )


async def main() -> None:
    """Load config, create the client, serve, and clean up."""
    import uvicorn

    settings = get_settings()
    app_logging.setup_logging(settings.log_level, settings.log_format)
    client = await UniFiClient.create(settings)
    try:
        # Capabilities are lazy in read tools: no gateway wait before serving.
        app = create_app(settings, client=client)
        config = uvicorn.Config(
            app,
            host=settings.mcp_bind_host,
            port=settings.mcp_bind_port,
            log_level=settings.log_level.lower(),
            access_log=False,
        )
        await uvicorn.Server(config).serve()
    finally:
        await client.aclose()


if __name__ == "__main__":
    import asyncio

    asyncio.run(main())
