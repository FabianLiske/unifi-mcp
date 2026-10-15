"""Readiness stays local during gateway latency, failure, and recovery."""

from __future__ import annotations

import asyncio
import time
from dataclasses import FrozenInstanceError

import httpx
import pytest
from pydantic import ValidationError

from tests.conftest import asgi_lifespan, mcp_client_session
from unifi_mcp.app import create_app
from unifi_mcp.health import UpstreamMonitor
from unifi_mcp.server import ReadinessProbe
from unifi_mcp.unifi.models import ApplicationInfo

BASE = "http://gateway.test/proxy/network/integration/v1"


async def wait_until(predicate):
    async with asyncio.timeout(1):
        while not predicate():
            await asyncio.sleep(0.001)


async def test_local_lifecycle_and_client_defects() -> None:
    opened = True
    probe = ReadinessProbe(lambda: opened)
    assert not (await probe.check())[0]
    probe.started = True
    assert (await probe.check())[0]
    opened = False
    assert not (await probe.check())[0]

    def broken():
        raise RuntimeError("local defect")

    probe = ReadinessProbe(broken)
    probe.started = True
    assert await probe.check() == (False, "local HTTP client unavailable")


async def test_monitor_failure_recovery_age_and_immutable_snapshot() -> None:
    clock = [0.0]
    ok = False

    async def check():
        if not ok:
            raise RuntimeError("secret-not-for-public-probes")
        return ApplicationInfo(applicationVersion="10.6.106")

    monitor = UpstreamMonitor(check, clock=lambda: clock[0])
    assert monitor.snapshot().status == "stale"
    assert monitor.snapshot().last_check_age_seconds is None
    await monitor._check_once()
    first = monitor.snapshot()
    assert first.status == "down"
    assert first.consecutive_failures == 1
    assert first.last_error == "RuntimeError"
    with pytest.raises(FrozenInstanceError):
        first.status = "ok"
    await monitor._check_once()
    assert monitor.snapshot().consecutive_failures == 2
    ok = True
    clock[0] = 1.0
    await monitor._check_once()
    recovered = monitor.snapshot()
    assert recovered.status == "ok"
    assert recovered.consecutive_failures == 0
    assert recovered.last_error is None
    assert recovered.last_success_at == 1.0
    assert recovered.last_failure_at == 0.0
    ok = False
    await monitor._check_once()
    assert monitor.snapshot().status == "degraded"
    assert monitor.snapshot().application_version == "10.6.106"
    clock[0] = 35.0
    assert monitor.snapshot().status == "stale"
    assert monitor.snapshot().last_check_age_seconds == 34.0
    assert first.consecutive_failures == 1


async def test_monitor_timeout_serial_checks_idempotent_start_and_stop() -> None:
    active = 0
    maximum = 0
    calls = 0

    async def slow():
        nonlocal active, maximum, calls
        active += 1
        maximum = max(maximum, active)
        calls += 1
        try:
            await asyncio.sleep(8)
        finally:
            active -= 1
        return ApplicationInfo(applicationVersion="unused")

    monitor = UpstreamMonitor(slow, interval_seconds=0.01, timeout_seconds=0.01)
    monitor.start()
    task = monitor._task
    monitor.start()
    assert monitor._task is task
    try:
        await wait_until(lambda: monitor.snapshot().consecutive_failures >= 2)
        assert monitor.snapshot().last_error == "TimeoutError"
        assert maximum == 1
    finally:
        await monitor.stop()
    assert active == 0
    stopped_calls = calls
    await asyncio.sleep(0.03)
    assert calls == stopped_calls
    await monitor.stop()
    monitor.start()
    await wait_until(lambda: calls > stopped_calls)
    await monitor.stop()
    assert active == 0


@pytest.mark.parametrize("mode", ["slow", "down", "recovery"])
async def test_asgi_probes_during_gateway_problems(
    mode,
    make_settings,
    make_client,
    respx_mock,
) -> None:
    calls = 0
    active = 0
    entered = asyncio.Event()
    cancelled = asyncio.Event()

    async def upstream(request):
        nonlocal calls, active
        calls += 1
        active += 1
        entered.set()
        try:
            if mode == "slow":
                try:
                    await asyncio.sleep(8)
                finally:
                    cancelled.set()
            if mode == "down" or (mode == "recovery" and calls == 1):
                return httpx.Response(503, json={"message": "unavailable"})
            return httpx.Response(200, json={"applicationVersion": "10.6.106"})
        finally:
            active -= 1

    respx_mock.get(f"{BASE}/info").mock(side_effect=upstream)
    settings = make_settings(
        upstream_check_interval_seconds=0.04 if mode == "recovery" else 60,
        upstream_check_timeout_seconds=0.03,
    )
    client = await make_client(settings)
    app = create_app(settings, client=client, groups=())
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://testserver"
    ) as http:
        assert (await http.get("/readyz")).status_code == 503
        async with asgi_lifespan(app):
            await asyncio.wait_for(entered.wait(), 1)
            # A burst of probes while /info is actively stuck must do no I/O.
            started = time.monotonic()
            responses = await asyncio.wait_for(
                asyncio.gather(*(http.get("/readyz") for _ in range(20))),
                1,
            )
            elapsed = time.monotonic() - started
            print(f"{mode}: 20 /readyz responses in {elapsed:.4f}s")
            assert all(r.status_code == 200 for r in responses)
            assert (await http.get("/healthz")).status_code == 200
            if mode == "slow":
                await asyncio.wait_for(cancelled.wait(), 1)
            elif mode == "down":
                # No internal retry even though UNIFI_MAX_RETRIES defaults to 2.
                assert calls == 1
            else:

                async def recovered():
                    while True:
                        r = await http.get("/readyz")
                        if r.json()["upstream"]["status"] == "ok":
                            return r
                        await asyncio.sleep(0.001)

                r = await asyncio.wait_for(recovered(), 1)
                assert r.json()["upstream"]["consecutive_failures"] == 0
            # The SDK's session manager must still run with the wrapper lifespan.
            async with mcp_client_session(
                app, settings.mcp_auth_token.get_secret_value()
            ) as session:
                result = await session.initialize()
                assert result.server_info.name == "unifi-mcp"
            await client.aclose()
            assert (await http.get("/readyz")).status_code == 503
            assert (await http.get("/healthz")).status_code == 200
        assert (await http.get("/readyz")).status_code == 503
        assert active == 0


@pytest.mark.parametrize(
    "key", ["upstream_check_interval_seconds", "upstream_check_timeout_seconds"]
)
@pytest.mark.parametrize("value", [0, -1, float("inf"), float("nan")])
def test_invalid_monitor_config_rejected(make_settings, key, value):
    with pytest.raises(ValidationError):
        make_settings(**{key: value})
