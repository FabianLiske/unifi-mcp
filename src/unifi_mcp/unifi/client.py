"""Async client for the official local UniFi Network API.

Design rules (design doc §7, budget discipline §44):
- exactly one reused ``httpx.AsyncClient`` (connection pooling)
- explicit, *configured* timeouts (connect / read / pool — a saturated
  pool surfaces as a bounded error instead of a silent multi-minute stall)
- retries only for transient failures, and only where safe (design §44):
  * ``GET`` — retried on transient status (5xx/429) *and* on transport
    errors, with exponential backoff + full jitter
  * ``PUT`` — retried on transient *status* only (the gateway answered,
    so the request provably did not execute); **never** on a transport
    error, where the outcome is ambiguous (no blind write retries)
  * ``POST`` — never retried (a doubled create is worse than a failure)
- every retry is bounded by a per-attempt *and* a per-tool-call wall-clock
  budget, so a single call always completes and reports before the
  external MCP/LiteLLM client deadline (60 s) — no retry amplification
- ``429`` respected via ``Retry-After``
- bounded concurrency: at most ``unifi_max_concurrent_requests`` in-flight
  gateway requests per client
- UniFi errors mapped to typed exceptions
- raw response size capped
- never log the API key or Authorization material
"""

from __future__ import annotations

import asyncio
import json
import random
import ssl
import time
from collections.abc import Mapping
from typing import TYPE_CHECKING, Any, cast
from urllib.parse import urlsplit

import httpx

from unifi_mcp import budget as _budget
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
    UniFiPoolTimeoutError,
    UniFiRateLimitError,
    UniFiResponseTooLargeError,
    UniFiTransportTimeoutError,
    UniFiUnavailableError,
    UniFiValidationError,
)
from unifi_mcp.unifi.models import ApplicationInfo, Page

if TYPE_CHECKING:
    from unifi_mcp.unifi.capabilities import CapabilityCache

logger = get_logger(__name__)

_API_BASE_PATH = "/proxy/network/integration/v1"
_TRANSIENT_STATUS = frozenset({500, 502, 503, 504})
#: Transport errors safe to retry for *reads* (the request almost certainly
#: never reached the gateway). Writes never retry transport errors (F05).
_RETRYABLE_TRANSPORT_ERRORS = (
    httpx.TimeoutException,
    httpx.NetworkError,
    httpx.RemoteProtocolError,
)


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
    """Build the single reused AsyncClient (pooling, timeouts, auth).

    All four timeout phases are configured: ``pool`` is its own, bounded
    value (a saturated pool surfaces as a :class:`UniFiPoolTimeoutError`
    after ``unifi_pool_timeout_seconds`` instead of a silent multi-minute
    stall). Pool size is derived from the bounded concurrency so
    ``max_connections`` can never be smaller than the in-flight requests the
    client allows (which would force every extra request to block on the
    pool and turn a burst into a wall of pool timeouts).
    """
    max_concurrent = settings.unifi_max_concurrent_requests
    return httpx.AsyncClient(
        base_url=settings.unifi_base_url + _API_BASE_PATH,
        headers={"X-API-Key": settings.unifi_api_key.get_secret_value()},
        timeout=httpx.Timeout(
            connect=settings.unifi_connect_timeout_seconds,
            read=settings.unifi_timeout_seconds,
            write=settings.unifi_timeout_seconds,
            pool=settings.unifi_pool_timeout_seconds,
        ),
        limits=httpx.Limits(
            max_connections=2 * max_concurrent,
            max_keepalive_connections=max_concurrent,
        ),
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
        backoff_cap: float = 2.0,
    ) -> None:
        self._settings = settings
        self._http = http
        self._backoff_base = backoff_base
        self._backoff_cap = backoff_cap
        self._capability_cache: CapabilityCache | None = None
        # Bounded concurrency: at most N in-flight gateway requests, so a
        # single tool call (or a burst of concurrent calls) cannot saturate
        # the pool and amplify a degraded gateway (F01/F05).
        self._concurrency = asyncio.Semaphore(settings.unifi_max_concurrent_requests)

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
        return cls(
            settings,
            build_http_client(settings, verify),
            backoff_base=settings.unifi_retry_backoff_base_seconds,
            backoff_cap=settings.unifi_retry_backoff_cap_seconds,
        )

    @property
    def site_id(self) -> str | None:
        return self._settings.unifi_site_id

    @property
    def attempt_headroom(self) -> float:
        """Worst-case wall-clock of one gateway request *attempt*.

        ``connect + read + pool``: the budget guard refuses an attempt whose
        worst case cannot finish inside the current tool-call budget (design
        §44), so a request never runs past the external deadline.
        """
        s = self._settings
        return (
            s.unifi_connect_timeout_seconds + s.unifi_timeout_seconds + s.unifi_pool_timeout_seconds
        )

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

    async def put(self, path: str, *, json: Any = None) -> Any:
        """Issue a PUT with an optional JSON body (full-replace semantics).

        Retry policy (design §44):

        - a transient *status* response (5xx/429) is retried: the gateway
          answered, so the request provably did not execute and retrying is
          safe;
        - a *transport* failure (timeout, connection error) is **never**
          retried here: the request may have executed server-side and timed
          out on the response, so the outcome is ambiguous. The write layer
          (:mod:`unifi_mcp.tools.writes`) re-reads the object to resolve the
          outcome instead of blindly re-issuing the write.
        """
        return await self._request("PUT", path, json_body=json)

    async def post(self, path: str, *, json: Any = None) -> Any:
        """Issue a POST (create) with an optional JSON body.

        POST is *not* idempotent: a transport failure may have executed the
        request server-side, so the call is never retried — a doubled create
        is worse than a reported failure.
        """
        return await self._request("POST", path, json_body=json, retry=False)

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
        """Exponential backoff with *full jitter*.

        ``delay = uniform(0, min(base * 2**(attempt-1), cap))``. Full jitter
        decorrelates concurrent clients and bursts so they do not retry on the
        same tick (retry amplification against a degraded gateway).
        """
        ceiling = min(self._backoff_base * 2.0 ** (attempt - 1), self._backoff_cap)
        return random.uniform(0.0, ceiling)

    def _should_retry_status(
        self, method: str, retry: bool, transient: bool, attempt: int, max_attempts: int
    ) -> bool:
        """Whether a transient *status* response is retried.

        Retried for GET and PUT (the gateway answered, so the write provably
        did not execute and retrying is safe); never for POST (``retry`` is
        already ``False``) or a non-transient 4xx.
        """
        return transient and retry and method in ("GET", "PUT") and attempt < max_attempts

    def _retry_delay_pool(
        self,
        method: str,
        retry: bool,
        attempt: int,
        max_attempts: int,
        started: float,
        endpoint_group: str,
        exc: httpx.PoolTimeout,
    ) -> float:
        """Pool-saturation handler: return the backoff delay to wait before a
        retry (reads), or raise the bounded structured error (writes)."""
        duration = time.monotonic() - started
        record_unifi_request(method, "pool_timeout", duration, endpoint_group)
        if method == "GET" and retry and attempt < max_attempts:
            delay = self._backoff(attempt)
            logger.info(
                "unifi request retrying after pool timeout",
                method=method,
                attempt=attempt,
                delay_seconds=delay,
            )
            return delay
        msg = (
            "UniFi API connection pool saturated "
            f"({self._settings.unifi_pool_timeout_seconds:.0f}s wait); "
            "too many in-flight requests"
        )
        raise UniFiPoolTimeoutError(msg) from exc

    def _retry_delay_transport(
        self,
        method: str,
        retry: bool,
        attempt: int,
        max_attempts: int,
        started: float,
        endpoint_group: str,
        exc: BaseException,
    ) -> float:
        """Transport-error handler: return the backoff delay to wait before a
        retry (reads), or raise. A *mutating* transport failure is surfaced as
        :class:`UniFiTransportTimeoutError` (the request may have executed —
        the write layer re-reads to resolve it, never a blind retry); any
        other non-read is :class:`UniFiUnavailableError`."""
        duration = time.monotonic() - started
        record_unifi_request(method, "error", duration, endpoint_group)
        if method == "GET" and retry and attempt < max_attempts:
            delay = self._backoff(attempt)
            logger.info(
                "unifi request retrying after transport error",
                method=method,
                error=exc.__class__.__name__,
                attempt=attempt,
                delay_seconds=delay,
            )
            return delay
        if method in ("PUT", "POST"):
            raise UniFiTransportTimeoutError(
                f"UniFi write failed in the transport layer and may "
                f"have executed: {exc.__class__.__name__} ({exc})",
            ) from exc
        raise UniFiUnavailableError(
            f"UniFi API unreachable: {exc.__class__.__name__} ({exc})",
        ) from exc

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
        json_body: Any = None,
        retry: bool = True,
    ) -> Any:
        """Issue one gateway request with the budgeted retry policy (design §44).

        - ``retry`` (status retries) is off for POST; transport-error retries
          are *additionally* off for PUT/POST (the outcome of a write that
          timed out in the transport is ambiguous — resolved by re-reading,
          never by a blind re-issue).
        - every attempt first passes the per-tool-call budget guard
          (:func:`unifi_mcp.budget.ensure_budget`), and every backoff honors
          the budget (:func:`unifi_mcp.budget.wait_backoff`).
        - in-flight requests are bounded by the concurrency semaphore.
        """
        settings = self._settings
        max_attempts = 1 + settings.unifi_max_retries if retry else 1
        endpoint_group = path.lstrip("/").split("/", 1)[0] or "root"
        headroom = self.attempt_headroom
        for attempt in range(1, max_attempts + 1):
            await _budget.ensure_budget(headroom)
            started = time.monotonic()
            response: httpx.Response | None = None
            try:
                async with self._concurrency:
                    request = self._http.build_request(method, path, params=params, json=json_body)
                    response = await self._http.send(request, stream=True)
                    body = await self._read_body_with_cap(
                        response, settings.max_tool_response_bytes
                    )
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
                # A transient *status* is retried for GET and PUT (the gateway
                # answered, so the write provably did not execute); never for
                # POST or a non-transient 4xx.
                if self._should_retry_status(method, retry, transient, attempt, max_attempts):
                    delay = retry_after if retry_after is not None else self._backoff(attempt)
                    logger.info(
                        "unifi request retrying",
                        method=method,
                        path=path,
                        status=status,
                        attempt=attempt,
                        delay_seconds=delay,
                    )
                    await _budget.wait_backoff(delay)
                    continue
                raise error
            except httpx.PoolTimeout as exc:
                # Pool saturation (bounded wait): retried for reads only,
                # surfaced as a structured error otherwise (see the helper).
                delay = self._retry_delay_pool(
                    method, retry, attempt, max_attempts, started, endpoint_group, exc
                )
                await _budget.wait_backoff(delay)
                continue
            except _RETRYABLE_TRANSPORT_ERRORS as exc:
                # Transport errors are retried for *reads* only; a *mutating*
                # failure is surfaced for the write layer to resolve (the
                # helper), never a blind retry (design §44).
                delay = self._retry_delay_transport(
                    method, retry, attempt, max_attempts, started, endpoint_group, exc
                )
                await _budget.wait_backoff(delay)
                continue
            finally:
                if response is not None:
                    await response.aclose()
        msg = "unreachable: retry loop exited without a result"
        raise AssertionError(msg)
