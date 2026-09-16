"""Unit tests for unifi_mcp.server.ReadinessProbe."""

from __future__ import annotations

from unifi_mcp.server import ReadinessProbe
from unifi_mcp.unifi.models import ApplicationInfo


class FakeClient:
    def __init__(self, ok: bool = True, version: str = "10.6.101") -> None:
        self.ok = ok
        self.version = version
        self.calls = 0

    async def get_application_info(self) -> ApplicationInfo:
        self.calls += 1
        if not self.ok:
            raise RuntimeError("gateway unreachable")
        return ApplicationInfo(applicationVersion=self.version)


class FakeClock:
    def __init__(self) -> None:
        self.t = 0.0

    def __call__(self) -> float:
        return self.t


async def test_ready_when_reachable() -> None:
    probe = ReadinessProbe(FakeClient(ok=True))
    ready, detail = await probe.check()
    assert ready is True
    assert "10.6.101" in detail


async def test_not_ready_when_unreachable() -> None:
    probe = ReadinessProbe(FakeClient(ok=False))
    ready, detail = await probe.check()
    assert ready is False
    assert "RuntimeError" in detail


async def test_caches_within_ttl() -> None:
    client = FakeClient(ok=True)
    clock = FakeClock()
    probe = ReadinessProbe(client, ttl_seconds=30.0, clock=clock)

    await probe.check()
    assert client.calls == 1

    clock.t += 10  # still within the 30s TTL
    await probe.check()
    assert client.calls == 1  # served from cache


async def test_rechecks_after_ttl() -> None:
    client = FakeClient(ok=True)
    clock = FakeClock()
    probe = ReadinessProbe(client, ttl_seconds=30.0, clock=clock)

    await probe.check()
    assert client.calls == 1

    clock.t += 30  # at/over the TTL boundary
    await probe.check()
    assert client.calls == 2  # re-queried
