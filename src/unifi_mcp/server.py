"""MCP server construction (design doc §26/§32).

Builds the ``MCPServer`` with server instructions, the ``/healthz`` and
``/readyz`` custom routes, and feature-flag-driven tool registration, then
exposes it as a *stateless* Streamable HTTP app under ``/mcp``.

Stateless is deliberate (design §4/§51): the server is a controlled API
adapter with no reason to hold long-lived MCP session state, which keeps it
trivially horizontally scalable and restart-friendly.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import asdict
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


def _server_instructions(label: str) -> str:
    """Server instructions prefixed with the instance label.

    When a client is connected to several instances of this server (e.g. a
    production deployment plus a local dev instance), the label is the
    client-side signal that disambiguates them.
    """
    return f'Instance: "{label}".\n' + SERVER_INSTRUCTIONS


class ReadinessProbe:
    """Purely local readiness: lifecycle complete and HTTP client usable.

    Config validation, audit initialization (including stdout fallback), and
    tool registration complete before the lifespan marks this ready.
    """

    def __init__(self, client_open: Callable[[], bool]) -> None:
        self._client_open = client_open
        self.started = False

    async def check(self) -> tuple[bool, str]:
        """Never perform network or filesystem I/O on the probe path."""
        if not self.started:
            return False, "local application not started or shutting down"
        try:
            if not self._client_open():
                return False, "local HTTP client closed"
        except Exception:  # noqa: BLE001 - local defects must return 503, not 500
            return False, "local HTTP client unavailable"
        return True, "local application ready"


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
    label = settings.mcp_instance_label
    server = MCPServer(
        name="unifi-mcp",
        title=f"UniFi Network MCP ({label})",
        description="Read-only management and diagnostics for a local UniFi Network deployment.",
        version=_server_version(),
        instructions=_server_instructions(label),
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
        payload["upstream"] = asdict(request.app.state.upstream_monitor.snapshot())
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
