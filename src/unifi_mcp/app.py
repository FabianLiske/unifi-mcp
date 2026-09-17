"""ASGI application assembly and the production entry point.

``create_app`` wires the layers together (outermost first):

1. :class:`~unifi_mcp.auth.middleware.BearerAuthMiddleware` — protects
   ``/mcp``, leaves ``/healthz``/``/readyz`` public.
2. The Starlette MCP app — stateless Streamable HTTP under ``/mcp`` plus the
   two health routes.

The :class:`~unifi_mcp.unifi.client.UniFiClient` is created by the caller
(:func:`main`) and closed by the caller, so tests can inject a pre-built
client without a real TLS handshake.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import TYPE_CHECKING, Any

from unifi_mcp.auth.middleware import BearerAuthMiddleware
from unifi_mcp.config import Settings, get_settings
from unifi_mcp.observability import logging as app_logging
from unifi_mcp.server import ReadinessProbe, build_server, mcp_starlette
from unifi_mcp.unifi.client import UniFiClient

if TYPE_CHECKING:
    from unifi_mcp.tools.registry import ToolGroup

ASGIApp = Callable[[dict[str, Any], Any, Any], Awaitable[None]]


def create_app(
    settings: Settings,
    *,
    client: UniFiClient,
    groups: tuple[ToolGroup, ...] | None = None,
) -> ASGIApp:
    """Assemble the final ASGI application around a ready *client*.

    *groups* overrides the default tool groups (see :func:`build_server`).
    """
    readiness = ReadinessProbe(client)
    server = build_server(settings, client, readiness, groups=groups)
    starlette_app = mcp_starlette(server, settings)
    return BearerAuthMiddleware(
        starlette_app, expected_token=settings.mcp_auth_token.get_secret_value()
    )


async def main() -> None:
    """Production entry point: load config, create the client, serve, clean up."""
    import uvicorn

    settings = get_settings()
    app_logging.setup_logging(settings.log_level, settings.log_format)
    client = await UniFiClient.create(settings)
    try:
        # Startup capability check (design §3): best-effort. A failure only
        # logs a warning — the server still starts and reports the degraded
        # state via /readyz and get_system_info.
        try:
            await client.capability_cache.get()
        except Exception as exc:  # noqa: BLE001 - startup detection is best-effort
            app_logging.get_logger(__name__).warning(
                "startup capability detection failed (server starts anyway)",
                error=f"{type(exc).__name__}: {exc}",
            )
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
