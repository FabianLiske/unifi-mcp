"""Exception hierarchy for the UniFi Network API client.

MCP tools must never surface raw Python tracebacks; these exceptions carry
structured attributes (HTTP status + UniFi error code) that the tool layer
maps to LLM-friendly errors (design doc section 30).
"""

from __future__ import annotations


class UniFiError(Exception):
    """Base class for all UniFi API errors."""

    def __init__(
        self,
        message: str,
        *,
        status_code: int | None = None,
        api_code: str | None = None,
    ) -> None:
        super().__init__(message)
        self.message = message
        self.status_code = status_code
        self.api_code = api_code

    def __str__(self) -> str:
        parts = [self.message]
        if self.api_code is not None:
            parts.append(f"code={self.api_code}")
        if self.status_code is not None:
            parts.append(f"status={self.status_code}")
        return " ".join(parts)


class UniFiAuthenticationError(UniFiError):
    """HTTP 401: the API key is missing, invalid, or revoked."""


class UniFiAuthorizationError(UniFiError):
    """HTTP 403: the API key is valid but not allowed for this operation."""


class UniFiNotFoundError(UniFiError):
    """HTTP 404: the resource or endpoint does not exist."""


class UniFiConflictError(UniFiError):
    """HTTP 409: the request conflicts with the current server state."""


class UniFiRateLimitError(UniFiError):
    """HTTP 429: the request was rate limited (retries exhausted)."""

    def __init__(
        self,
        message: str,
        *,
        status_code: int | None = None,
        api_code: str | None = None,
        retry_after: float | None = None,
    ) -> None:
        super().__init__(message, status_code=status_code, api_code=api_code)
        self.retry_after = retry_after


class UniFiValidationError(UniFiError):
    """HTTP 400: the request was rejected (invalid parameters/filter)."""


class UniFiUnavailableError(UniFiError):
    """5xx, connection failure, or timeout: the gateway is unreachable."""


class UniFiResponseTooLargeError(UniFiError):
    """The raw API response exceeded the configured size cap."""
