"""Async client for the official local UniFi Network API.

Design rules (design doc section 7):
- exactly one reused ``httpx.AsyncClient`` (connection pooling)
- explicit timeouts
- retries only for transient failures (5xx, 429, connect/read errors, timeouts)
- ``429`` respected via ``Retry-After``
- UniFi errors mapped to typed exceptions
- raw response size capped
- never log the API key or Authorization material
"""

from __future__ import annotations

import asyncio
import json
import ssl
import time
from collections.abc import Mapping
from typing import TYPE_CHECKING, Any, cast
from urllib.parse import urlsplit

import httpx

from unifi_mcp.config import Settings
from unifi_mcp.observability.logging import get_logger
from unifi_mcp.observability.metrics import record_unifi_request
from unifi_mcp.unifi import tls
from unifi_mcp.unifi.errors import (
    UniFiAuthenticationError,
    UniFiAuthorizationError,
    UniFiConflictError,
    UniFiError,
    UniFiNotFoundError,
    UniFiRateLimitError,
    UniFiResponseTooLargeError,
    UniFiUnavailableError,
    UniFiValidationError,
)
from unifi_mcp.unifi.models import ApplicationInfo, Page

if TYPE_CHECKING:
    from unifi_mcp.unifi.capabilities import CapabilityCache

logger = get_logger(__name__)

_API_BASE_PATH = "/proxy/network/integration/v1"
_TRANSIENT_STATUS = frozenset({500, 502, 503, 504})
_RETRYABLE_TRANSPORT_ERRORS = (
    httpx.TimeoutException,
    httpx.NetworkError,
    httpx.RemoteProtocolError,
)
_MAX_BACKOFF_SECONDS = 8.0


def _api_code(body: Any) -> str | None:
    if isinstance(body, Mapping):
        code = body.get("code")
        if isinstance(code, str) and code:
            return code
    return None


def _api_message(body: Any, status_code: int) -> str:
    if isinstance(body, Mapping):
        message = body.get("message")
        if isinstance(message, str) and message:
            return message
    return f"UniFi API error (HTTP {status_code})"


def _try_json(data: bytes) -> Any:
    if not data:
        return None
    try:
        return json.loads(data)
    except json.JSONDecodeError:
        return None


def _status_exception(status_code: int, body: Any) -> UniFiError:
    code = _api_code(body)
    message = _api_message(body, status_code)
    if status_code == 401:
        return UniFiAuthenticationError(message, status_code=status_code, api_code=code)
    if status_code == 403:
        return UniFiAuthorizationError(message, status_code=status_code, api_code=code)
    if status_code == 404:
        return UniFiNotFoundError(message, status_code=status_code, api_code=code)
    if status_code == 409:
        return UniFiConflictError(message, status_code=status_code, api_code=code)
    if status_code == 429:
        return UniFiRateLimitError(message, status_code=status_code, api_code=code)
    if status_code == 400:
        return UniFiValidationError(message, status_code=status_code, api_code=code)
    if 500 <= status_code < 600:
        return UniFiUnavailableError(message, status_code=status_code, api_code=code)
    return UniFiError(message, status_code=status_code, api_code=code)


def _parse_retry_after(headers: httpx.Headers) -> float | None:
    raw = headers.get("retry-after")
    if raw is None:
        return None
    try:
        value = float(raw)
    except ValueError:
        return None  # HTTP-date form: fall back to plain backoff
    return max(0.0, value)


def build_http_client(settings: Settings, verify: bool | ssl.SSLContext) -> httpx.AsyncClient:
    """Build the single reused AsyncClient (pooling, timeouts, auth)."""
    return httpx.AsyncClient(
        base_url=settings.unifi_base_url + _API_BASE_PATH,
        headers={"X-API-Key": settings.unifi_api_key.get_secret_value()},
        timeout=httpx.Timeout(
            connect=settings.unifi_connect_timeout_seconds,
            read=settings.unifi_timeout_seconds,
            write=settings.unifi_timeout_seconds,
            pool=settings.unifi_connect_timeout_seconds,
        ),
        limits=httpx.Limits(max_connections=20, max_keepalive_connections=10),
        verify=verify,
    )


class UniFiClient:
    """Thin async wrapper around the official local UniFi Network API."""

    def __init__(
        self,
        settings: Settings,
        http: httpx.AsyncClient,
        *,
        backoff_base: float = 0.5,
    ) -> None:
        self._settings = settings
        self._http = http
        self._backoff_base = backoff_base
        self._capability_cache: CapabilityCache | None = None

    @classmethod
    async def create(cls, settings: Settings) -> UniFiClient:
        """Create a client, running the extract-once TLS bootstrap if needed."""
        parts = urlsplit(settings.unifi_base_url)
        extracted_pem: str | None = None
        if parts.scheme == "https" and settings.unifi_tls_mode == "extract-once":
            host = parts.hostname or ""
            port = parts.port or 443
            try:
                pem, fingerprint = await asyncio.to_thread(
                    tls.bootstrap_extract_once,
                    host,
                    port,
                    settings.unifi_connect_timeout_seconds,
                )
            except OSError as exc:
                raise UniFiUnavailableError(
                    "TLS extract-once bootstrap failed: "
                    f"{exc.__class__.__name__} (fix the network, or use "
                    "UNIFI_TLS_MODE=strict with UNIFI_CA_BUNDLE, or "
                    "UNIFI_TLS_MODE=insecure for local development)",
                ) from exc
            extracted_pem = pem
            logger.warning(
                "TLS extract-once: pinned gateway certificate "
                "(verify this fingerprint against the physical gateway!)",
                sha256=fingerprint,
            )
        verify = tls.build_verify(
            settings.unifi_tls_mode,
            settings.unifi_base_url,
            ca_bundle=str(settings.unifi_ca_bundle) if settings.unifi_ca_bundle else None,
            extracted_pem=extracted_pem,
        )
        return cls(settings, build_http_client(settings, verify))

    @property
    def site_id(self) -> str | None:
        return self._settings.unifi_site_id

    @property
    def capability_cache(self) -> CapabilityCache:
        """Per-client, TTL-cached capability detection (design §29).

        Created lazily; the import stays inside the property because
        :mod:`unifi_mcp.unifi.capabilities` imports this module.
        """
        if self._capability_cache is None:
            from unifi_mcp.unifi.capabilities import CapabilityCache

            self._capability_cache = CapabilityCache(self)
        return self._capability_cache

    async def aclose(self) -> None:
        await self._http.aclose()

    async def __aenter__(self) -> UniFiClient:
        return self

    async def __aexit__(self, exc_type: object, exc: object, tb: object) -> None:
        await self.aclose()

    async def get(self, path: str, *, params: dict[str, Any] | None = None) -> Any:
        """Issue a GET and return the parsed JSON payload."""
        return await self._request("GET", path, params=params)

    async def get_list(self, path: str, *, params: dict[str, Any] | None = None) -> Page:
        """Issue a GET on a list endpoint and return the paging envelope."""
        data = await self.get(path, params=params)
        if not isinstance(data, Mapping):
            msg = f"expected a list envelope from {path}, got {type(data).__name__}"
            raise UniFiError(msg)
        return Page.model_validate(cast(Mapping[str, Any], data))

    async def get_application_info(self) -> ApplicationInfo:
        """Fetch top-level application info (``GET /info``)."""
        data = await self.get("/info")
        if not isinstance(data, Mapping):
            msg = f"unexpected /info payload: {type(data).__name__}"
            raise UniFiError(msg)
        return ApplicationInfo.model_validate(cast(Mapping[str, Any], data))

    def _backoff(self, attempt: int) -> float:
        delay = self._backoff_base * 2.0 ** (attempt - 1)
        return min(delay, _MAX_BACKOFF_SECONDS)

    async def _read_body_with_cap(self, response: httpx.Response, cap: int) -> bytes:
        content_length = response.headers.get("content-length")
        if content_length is not None:
            try:
                declared = int(content_length)
            except ValueError:
                declared = -1
            if declared > cap:
                await response.aclose()
                msg = f"UniFi response declares {declared} bytes, exceeds cap of {cap}"
                raise UniFiResponseTooLargeError(msg) from None
        chunks: list[bytes] = []
        total = 0
        async for chunk in response.aiter_bytes():
            total += len(chunk)
            if total > cap:
                await response.aclose()
                msg = f"UniFi response exceeds size cap of {cap} bytes"
                raise UniFiResponseTooLargeError(msg) from None
            chunks.append(chunk)
        return b"".join(chunks)

    async def _request(
        self,
        method: str,
        path: str,
        *,
        params: dict[str, Any] | None = None,
    ) -> Any:
        settings = self._settings
        max_attempts = 1 + settings.unifi_max_retries
        endpoint_group = path.lstrip("/").split("/", 1)[0] or "root"
        for attempt in range(1, max_attempts + 1):
            started = time.monotonic()
            response: httpx.Response | None = None
            try:
                request = self._http.build_request(method, path, params=params)
                response = await self._http.send(request, stream=True)
                body = await self._read_body_with_cap(response, settings.max_tool_response_bytes)
                status = response.status_code
                retry_after = _parse_retry_after(response.headers)
                duration = time.monotonic() - started
                if 200 <= status < 300:
                    record_unifi_request(method, str(status), duration, endpoint_group)
                    logger.debug(
                        "unifi request",
                        method=method,
                        path=path,
                        status=status,
                        duration_ms=round(duration * 1000, 1),
                    )
                    parsed = _try_json(body)
                    if parsed is None:
                        msg = f"UniFi API returned invalid JSON (HTTP {status})"
                        raise UniFiError(msg)
                    return parsed
                error = _status_exception(status, _try_json(body))
                if isinstance(error, UniFiRateLimitError):
                    error.retry_after = retry_after
                record_unifi_request(method, str(status), duration, endpoint_group)
                transient = status == 429 or status in _TRANSIENT_STATUS
                if transient and attempt < max_attempts:
                    delay = retry_after if retry_after is not None else self._backoff(attempt)
                    logger.info(
                        "unifi request retrying",
                        method=method,
                        path=path,
                        status=status,
                        attempt=attempt,
                        delay_seconds=delay,
                    )
                    await asyncio.sleep(delay)
                    continue
                raise error
            except _RETRYABLE_TRANSPORT_ERRORS as exc:
                duration = time.monotonic() - started
                record_unifi_request(method, "error", duration, endpoint_group)
                if attempt < max_attempts:
                    delay = self._backoff(attempt)
                    logger.info(
                        "unifi request retrying after transport error",
                        method=method,
                        path=path,
                        error=exc.__class__.__name__,
                        attempt=attempt,
                        delay_seconds=delay,
                    )
                    await asyncio.sleep(delay)
                    continue
                raise UniFiUnavailableError(
                    f"UniFi API unreachable: {exc.__class__.__name__} ({exc})",
                ) from exc
            finally:
                if response is not None:
                    await response.aclose()
        msg = "unreachable: retry loop exited without a result"
        raise AssertionError(msg)
