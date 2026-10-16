"""Per-call wall-clock budget (deadline) for UniFi tool calls (design §44).

A single MCP tool call may fan out into several *sequential* UniFi gateway
requests (read-before-write ``GET`` then ``PUT``, capability re-detection with
up to eleven probes, site resolution, ...). Each request has its own retry
budget, but the **total** wall-clock time for the whole tool call must stay
strictly below the *external* MCP / LiteLLM tool-call deadline
(:data:`EXTERNAL_MCP_TOOL_TIMEOUT_SECONDS`). Otherwise the LiteLLM client
aborts the call while unifi-mcp is still mid-retry — the classic
retry-amplification and unknown-write-outcome failure this module exists to
prevent.

The budget is carried in a :class:`contextvars.ContextVar` so that
:func:`unifi_mcp.tools.common.wrap_tool` (which wraps every tool call) sets it
once per call, and :class:`unifi_mcp.unifi.client.UniFiClient` (which may issue
several gateway requests within that same call) reads and enforces it — without
threading a deadline through every call signature. Each incoming MCP tool call
runs in its own task, so the ``ContextVar`` is naturally isolated per call and
no two concurrent tool calls can bleed into each other's budget.

Call paths that are *not* behind the external tool-call deadline (the startup
capability check, the ``/readyz`` readiness probe) leave the ``ContextVar``
unset (``None``); for those the client enforces only its per-attempt timeouts.

This module deliberately lives at the *package* level (not under
``unifi_mcp.unifi``): it imports only the leaf settings constant from
``unifi_mcp.config`` (which imports nothing from this module), never the
client — placing it under ``unifi_mcp.unifi`` would create an import cycle
(``unifi/__init__`` eagerly imports the client).
"""

from __future__ import annotations

import asyncio
import contextvars
import time
from collections.abc import Iterator
from contextlib import contextmanager

from unifi_mcp.unifi.errors import UniFiToolBudgetError

#: External MCP / LiteLLM tool-call deadline (seconds) — imported from
#: :mod:`unifi_mcp.config` (the single source of truth) so the internal
#: per-tool-call budget validated there and the deadline enforced here can
#: never drift apart. See :data:`unifi_mcp.config.EXTERNAL_MCP_TOOL_TIMEOUT_SECONDS`.

#: Monotonic deadline (``time.monotonic()``) by which the current MCP tool call
#: must complete, or ``None`` outside a tool-call context (startup checks,
#: readiness probes).
current_tool_deadline: contextvars.ContextVar[float | None] = contextvars.ContextVar(
    "unifi_tool_call_deadline", default=None
)


def remaining_budget() -> float | None:
    """Return seconds remaining in the current tool-call budget.

    Returns ``None`` when no tool-call budget is in effect (call paths not
    behind the external deadline), or ``>= 0.0`` when one is (``0.0`` means the
    budget is fully consumed).
    """
    deadline = current_tool_deadline.get()
    if deadline is None:
        return None
    return max(0.0, deadline - time.monotonic())


async def ensure_budget(headroom: float) -> None:
    """Raise :class:`UniFiToolBudgetError` if *headroom* seconds are not left.

    Call before a gateway request (or a backoff) whose worst case needs
    *headroom* seconds to finish inside the budget. A request that cannot
    finish before the deadline is worse than an early, structured error, so
    this raises instead of letting the request run past the deadline. Without
    a budget in effect this is a no-op.
    """
    remaining = remaining_budget()
    if remaining is not None and remaining < headroom:
        raise UniFiToolBudgetError(
            "per-tool-call UniFi budget exhausted before this request could "
            f"complete ({remaining:.1f}s left, {headroom:.1f}s needed)",
            headroom_seconds=headroom,
        )


async def wait_backoff(delay: float) -> None:
    """Sleep *delay* seconds, honoring the tool-call budget.

    The sleep is clipped to the remaining budget; if the budget expires during
    the sleep, raise :class:`UniFiToolBudgetError` so the caller reports a
    structured error instead of retrying past the deadline. Without a budget
    this is a plain sleep.
    """
    remaining = remaining_budget()
    if remaining is None:
        await asyncio.sleep(delay)
        return
    if delay <= remaining:
        await asyncio.sleep(delay)
        return
    await asyncio.sleep(max(0.0, remaining))
    raise UniFiToolBudgetError(
        f"per-tool-call UniFi budget exhausted during retry backoff ({delay:.1f}s)",
        headroom_seconds=None,
    )


@contextmanager
def tool_call_budget(seconds: float) -> Iterator[None]:
    """Run a block with a per-tool-call deadline of *seconds* from now.

    Used by :func:`unifi_mcp.tools.common.wrap_tool` and by tests. The
    deadline is restored on exit so a caller's budget (if any) is unaffected.
    """
    token = current_tool_deadline.set(time.monotonic() + seconds)
    try:
        yield
    finally:
        current_tool_deadline.reset(token)
