"""Unit tests for the per-tool-call wall-clock budget (design §44)."""

from __future__ import annotations

import asyncio
import time

import pytest

from unifi_mcp import budget
from unifi_mcp.unifi.errors import UniFiToolBudgetError


def test_no_budget_outside_tool_call() -> None:
    assert budget.remaining_budget() is None


async def test_budget_deadline_enforced_inside_tool_call() -> None:
    with budget.tool_call_budget(10.0):
        remaining = budget.remaining_budget()
        assert remaining is not None
        assert 9.0 <= remaining <= 10.0
    # the caller's (absent) budget is restored on exit
    assert budget.remaining_budget() is None


async def test_ensure_budget_passes_when_headroom_available() -> None:
    with budget.tool_call_budget(10.0):
        await budget.ensure_budget(5.0)  # must not raise


async def test_ensure_budget_raises_when_headroom_exceeded() -> None:
    with budget.tool_call_budget(10.0), pytest.raises(UniFiToolBudgetError) as excinfo:
        await budget.ensure_budget(99.0)
    assert excinfo.value.headroom_seconds == 99.0


async def test_ensure_budget_noop_without_budget() -> None:
    await budget.ensure_budget(999.0)  # no budget in effect: must not raise


async def test_wait_backoff_clipped_and_exhausted_raises() -> None:
    with budget.tool_call_budget(0.2):
        started = time.monotonic()
        with pytest.raises(UniFiToolBudgetError):
            await budget.wait_backoff(5.0)  # budget expires mid-sleep
        elapsed = time.monotonic() - started
        # slept only ~the remaining budget, not the full 5 s
        assert elapsed < 2.0


async def test_wait_backoff_short_sleep_completes() -> None:
    with budget.tool_call_budget(5.0):
        await budget.wait_backoff(0.01)  # fits: no raise


async def test_budget_nested_restores_outer_deadline() -> None:
    with budget.tool_call_budget(10.0):
        outer = budget.remaining_budget()
        with budget.tool_call_budget(2.0):
            inner = budget.remaining_budget()
            assert inner is not None and inner <= 2.0
        restored = budget.remaining_budget()
        assert restored is not None
        # outer deadline untouched by the inner budget
        assert abs(restored - outer) < 0.05


async def test_budget_exhausted_mid_call_is_structured_error() -> None:
    """The core F05 guarantee: a call that would overrun the external 60 s
    deadline reports a structured budget error instead of hanging past it."""
    with budget.tool_call_budget(0.05), pytest.raises(UniFiToolBudgetError):
        await asyncio.sleep(0.06)  # burn the budget
        await budget.ensure_budget(25.0)
