"""MCP server construction (design doc §26/§32).

Builds the ``MCPServer`` with server instructions, the ``/healthz`` and
``/readyz`` custom routes, and feature-flag-driven tool registration, then
exposes it as a *stateless* Streamable HTTP app under ``/mcp``.

Stateless is deliberate (design §4/§51): the server is a controlled API
adapter with no reason to hold long-lived MCP session state, which keeps it
trivially horizontally scalable and restart-friendly.
"""

from __future__ import annotations

import time
from collections.abc import Callable
from importlib.metadata import PackageNotFoundError
from importlib.metadata import version as _package_version
from typing import TYPE_CHECKING, Any

from mcp.server.mcpserver import MCPServer
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse, Response

from unifi_mcp.tools.registry import register_tools

if TYPE_CHECKING:
    from unifi_mcp.config import Settings
    from unifi_mcp.tools.registry import ToolGroup
    from unifi_mcp.unifi.client import UniFiClient


def _server_version() -> str:
    """Best-effort installed package version (avoids a circular top-level import)."""
    try:
        return _package_version("unifi-mcp")
    except PackageNotFoundError:  # pragma: no cover - dev checkouts
        return ""


SERVER_INSTRUCTIONS = """\
This MCP server manages a live UniFi Network deployment.

Prefer read-only inspection before configuration changes.
Use exact IDs returned by read tools.
Do not assume that a missing API field is false.
Write tools modify live network configuration.
Before using a write tool, read the target object and use the
returned state_hash.
Do not expose or request secrets through tool arguments unless
a tool explicitly supports them.
"""


class ReadinessProbe:
    """Caches the UniFi application-info check for ``/readyz``.

    The gateway is queried at most once per TTL (design §26: at most every 30
    seconds) so Kubernetes probes do not hammer it. If UniFi is briefly
    unreachable, readiness can be ``false`` while liveness stays ``true``.
    """

    def __init__(
        self,
        client: UniFiClient,
        *,
        ttl_seconds: float = 30.0,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._client = client
        self._ttl = ttl_seconds
        self._clock = clock
        self._last_check: float = -1.0
        self._ready: bool = False
        self._detail = "not checked yet"

    async def check(self) -> tuple[bool, str]:
        """Return ``(ready, detail)``, using the cached result within the TTL."""
        now = self._clock()
        if self._last_check >= 0 and (now - self._last_check) < self._ttl:
            return self._ready, self._detail
        try:
            info = await self._client.get_application_info()
            ready = True
            detail = f"unifi applicationVersion={info.application_version}"
        except Exception as exc:  # noqa: BLE001 - readiness must never raise
            ready = False
            detail = f"unifi check failed: {type(exc).__name__}"
        self._last_check = now
        self._ready = ready
        self._detail = detail
        return ready, detail


def build_server(
    settings: Settings,
    client: UniFiClient,
    readiness: ReadinessProbe,
    *,
    groups: tuple[ToolGroup, ...] | None = None,
) -> MCPServer:
    """Construct the ``MCPServer`` with health routes and registered tools.

    *groups* overrides the default :data:`~unifi_mcp.tools.registry.TOOL_GROUPS`
    (used by tests to exercise individual tool groups without shipping them).
    """
    server = MCPServer(
        name="unifi-mcp",
        title="UniFi Network MCP",
        description="Read-only management and diagnostics for a local UniFi Network deployment.",
        version=_server_version(),
        instructions=SERVER_INSTRUCTIONS,
    )

    @server.custom_route("/healthz", methods=["GET"])  # type: ignore[untyped-decorator]
    async def healthz(request: Request) -> Response:
        # Liveness: process only, no UniFi call (design §26).
        return JSONResponse({"status": "ok"})

    @server.custom_route("/readyz", methods=["GET"])  # type: ignore[untyped-decorator]
    async def readyz(request: Request) -> Response:
        ready, detail = await readiness.check()
        payload: dict[str, Any] = {
            "status": "ready" if ready else "not_ready",
            "detail": detail,
        }
        return JSONResponse(payload, status_code=200 if ready else 503)

    register_tools(server, client, settings, groups=groups)
    return server


def mcp_starlette(server: MCPServer, settings: Settings) -> Starlette:
    """Expose *server* as a stateless Streamable HTTP app under ``/mcp``."""
    return server.streamable_http_app(
        streamable_http_path="/mcp",
        stateless_http=True,
        host=settings.mcp_bind_host,
    )
