"""Non-blocking upstream diagnostics, deliberately independent of readiness."""

from __future__ import annotations

import asyncio
import time
from collections.abc import Awaitable, Callable
from contextlib import suppress
from dataclasses import dataclass, replace
from typing import TYPE_CHECKING, Literal

if TYPE_CHECKING:
    from unifi_mcp.unifi.models import ApplicationInfo


@dataclass(frozen=True)
class UpstreamState:
    """Immutable snapshot; timestamps and ages use a monotonic clock."""

    status: Literal["ok", "degraded", "down", "stale"] = "stale"
    application_version: str | None = None
    last_latency_ms: float | None = None
    last_success_at: float | None = None
    last_failure_at: float | None = None
    last_error: str | None = None
    consecutive_failures: int = 0
    last_check_age_seconds: float | None = None


class UpstreamMonitor:
    """One bounded, retry-free check at a time, never on a request path.

    The injected check must perform a single request and honor cancellation.
    Sleep *after* completion avoids catch-up bursts and overlapping checks.
    Only exception class names are published: messages may contain credentials.
    """

    def __init__(
        self,
        check: Callable[[], Awaitable[ApplicationInfo]],
        *,
        interval_seconds: float = 30.0,
        timeout_seconds: float = 3.0,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        if interval_seconds <= 0 or timeout_seconds <= 0:
            raise ValueError("monitor interval and timeout must be positive")
        self._check = check
        self._interval = interval_seconds
        self._timeout = timeout_seconds
        self._clock = clock
        self._state = UpstreamState()
        self._last_check: float | None = None
        self._task: asyncio.Task[None] | None = None

    def snapshot(self) -> UpstreamState:
        """Return cached information without I/O, locks, or task scheduling."""
        age = None if self._last_check is None else max(0.0, self._clock() - self._last_check)
        stale = age is None or age > self._interval + self._timeout
        return replace(
            self._state,
            status="stale" if stale else self._state.status,
            last_check_age_seconds=age,
        )

    def start(self) -> None:
        """Start once inside the application's running lifespan."""
        if self._task is None or self._task.done():
            self._task = asyncio.create_task(self._run(), name="unifi-upstream-monitor")

    async def stop(self) -> None:
        """Cancel and join the active request before SDK/client shutdown."""
        if self._task is not None:
            self._task.cancel()
            with suppress(asyncio.CancelledError):
                await self._task
            self._task = None

    async def _run(self) -> None:
        while True:
            await self._check_once()
            await asyncio.sleep(self._interval)

    async def _check_once(self) -> None:
        started = self._clock()
        try:
            async with asyncio.timeout(self._timeout):
                info = await self._check()
        except Exception as exc:  # noqa: BLE001 - diagnostics must survive gateway failures
            finished = self._clock()
            self._state = replace(
                self._state,
                status="down" if self._state.last_success_at is None else "degraded",
                last_latency_ms=(finished - started) * 1000,
                last_failure_at=finished,
                last_error=type(exc).__name__,
                consecutive_failures=self._state.consecutive_failures + 1,
            )
        else:
            finished = self._clock()
            self._state = replace(
                self._state,
                status="ok",
                application_version=info.application_version,
                last_latency_ms=(finished - started) * 1000,
                last_success_at=finished,
                last_error=None,
                consecutive_failures=0,
            )
        self._last_check = finished
