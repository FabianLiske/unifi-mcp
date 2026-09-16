"""Bearer-token authentication for the MCP HTTP endpoints.

Design doc §19: a single static Bearer token shared between LiteLLM and the
MCP protects the ``/mcp`` endpoint. The token comes from ``MCP_AUTH_TOKEN``
(Kubernetes Secret) and is compared in constant time.

``/healthz`` and ``/readyz`` are deliberately left public: Kubernetes
liveness/readiness probes do not send the token, and neither endpoint returns
sensitive data (see PROGRESS.md, WP-6 decisions).
"""

from __future__ import annotations

import hmac
import json
from collections.abc import Awaitable, Callable
from typing import Any

from unifi_mcp.observability import logging as app_logging
from unifi_mcp.observability.metrics import record_auth_failure

ASGIApp = Callable[[dict[str, Any], Any, Any], Awaitable[None]]

#: Prefixes that require a Bearer token. Health endpoints stay public.
PROTECTED_PREFIXES: tuple[str, ...] = ("/mcp",)

logger = app_logging.get_logger(__name__)


def extract_bearer_token(scope: dict[str, Any]) -> str | None:
    """Return the raw Bearer token from an ASGI scope, or ``None``.

    Returns ``None`` both when no ``Authorization`` header is present and when
    the header is present but not a ``Bearer`` credential.
    """
    for key, value in scope.get("headers", []):
        if key.lower() != b"authorization":
            continue
        header = value.decode("latin-1")
        if not header.lower().startswith("bearer "):
            return None
        token = header[len("bearer ") :].strip()
        return token or None
    return None


def _is_protected(path: str, prefixes: tuple[str, ...]) -> bool:
    return any(path == p or path.startswith(p + "/") for p in prefixes)


async def _send_json(
    send: Callable[[dict[str, Any]], Awaitable[None]], status: int, payload: dict[str, Any]
) -> None:
    body = json.dumps(payload).encode("utf-8")
    await send(
        {
            "type": "http.response.start",
            "status": status,
            "headers": [
                (b"content-type", b"application/json"),
                (b"content-length", str(len(body)).encode()),
            ],
        }
    )
    await send({"type": "http.response.body", "body": body})


class BearerAuthMiddleware:
    """Pure-ASGI middleware enforcing a static Bearer token on protected paths.

    Missing/invalid ``Authorization`` on a protected path yields ``401``/
    ``403`` and an auth-failure metric. Non-HTTP scopes (notably ``lifespan``)
    and public paths are passed through untouched.
    """

    def __init__(
        self,
        app: ASGIApp,
        *,
        expected_token: str,
        protected_prefixes: tuple[str, ...] = PROTECTED_PREFIXES,
    ) -> None:
        self._app = app
        self._expected = expected_token.encode("utf-8")
        self._prefixes = protected_prefixes

    async def __call__(self, scope: dict[str, Any], receive: Any, send: Any) -> None:
        if scope["type"] == "http":
            app_logging.bind_request_id(app_logging.new_request_id())
            path: str = scope["path"]
            if _is_protected(path, self._prefixes):
                token = extract_bearer_token(scope)
                if token is None:
                    record_auth_failure()
                    logger.warning("mcp auth failed", reason="missing_token", path=path)
                    await _send_json(
                        send,
                        401,
                        {
                            "error": "unauthorized",
                            "message": "Missing Authorization: Bearer <token> header.",
                        },
                    )
                    return
                if not hmac.compare_digest(token.encode("utf-8"), self._expected):
                    record_auth_failure()
                    logger.warning("mcp auth failed", reason="invalid_token", path=path)
                    await _send_json(
                        send,
                        403,
                        {"error": "forbidden", "message": "Invalid bearer token."},
                    )
                    return
        await self._app(scope, receive, send)
