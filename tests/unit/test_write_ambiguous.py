"""Unit tests for the ambiguous-write outcome resolution (design §44 / F05).

A PUT that fails in the *transport* layer may have executed server-side. The
write path must never blindly retry; instead :func:`guarded_update` re-reads
the object and resolves the outcome:

* re-read hash == expected *post-write* hash  -> the write executed  -> OK;
* re-read hash == pre-write ``expected_state_hash`` -> provably a no-op
  -> the original transport error is re-raised (a clean failure);
* re-read hash == some other state            -> cannot be proven
  -> :class:`AmbiguousWriteOutcomeError` with the current hash.

The PUT is made to fail in the transport layer (respx) while the re-read GET
succeeds, so the three resolution branches are exercised deterministically.
"""

from __future__ import annotations

from typing import Any

import httpx
import pytest

from unifi_mcp.safety.state_hash import compute_state_hash
from unifi_mcp.tools.errors import AmbiguousWriteOutcomeError
from unifi_mcp.tools.writes import guarded_update
from unifi_mcp.unifi.errors import UniFiTransportTimeoutError

BASE = "http://gateway.test/proxy/network/integration/v1"
SITE = "site-default"
WIDGET_PATH = f"/sites/{SITE}/widgets/widget-1"

#: The mutable object as the gateway returns it (server-managed + internal).
WIDGET_RAW: dict[str, Any] = {
    "id": "widget-1",
    "name": "Test Widget",
    "type": "DASHBOARD",
    "description": "old description",
    "metadata": {"origin": "USER_DEFINED", "creator": "user", "creationTime": 1},
    "revision": 3,
    "etag": "0x3",
}

ALLOWED = frozenset({"description"})
CHANGES = {"description": "new description"}


def _updated_raw() -> dict[str, Any]:
    """The object after the guarded write (description changed)."""
    out = dict(WIDGET_RAW)
    out["description"] = CHANGES["description"]
    return out


def _pre_write_hash() -> str:
    return compute_state_hash(WIDGET_RAW)


def _post_write_hash() -> str:
    return compute_state_hash(_updated_raw())


async def _run(respx_mock, client, re_read_payload: dict[str, Any]) -> dict[str, Any]:
    """Drive guarded_update; the PUT times out in the transport. The GET
    serves two sequential responses: the *pre-write* object for the
    read-before-write guard, then *re_read_payload* for the ambiguous-outcome
    re-read."""
    respx_mock.get(f"{BASE}{WIDGET_PATH}").mock(
        side_effect=[
            httpx.Response(200, json=WIDGET_RAW),
            httpx.Response(200, json=re_read_payload),
        ]
    )
    respx_mock.put(f"{BASE}{WIDGET_PATH}").mock(side_effect=httpx.ReadTimeout("response timed out"))
    return await guarded_update(
        client,
        path=WIDGET_PATH,
        object_id="widget-1",
        resource="widget",
        expected_state_hash=_pre_write_hash(),
        changes=CHANGES,
        allowed_fields=ALLOWED,
        tool="update_widget",
        site_id=SITE,
    )


async def test_ambiguous_put_resolved_executed(respx_mock, make_client) -> None:
    """Re-read shows the post-write state -> the write executed -> success,
    reported with a fresh state hash (no blind re-PUT)."""
    client = await make_client()
    result = await _run(respx_mock, client, _updated_raw())
    # exactly one PUT (the timed-out one); the resolution is a GET, not a 2nd PUT
    assert respx_mock.put(f"{BASE}{WIDGET_PATH}").call_count == 1
    assert result["description"] == "new description"
    assert result["state_hash"] == _post_write_hash()


async def test_ambiguous_put_resolved_noop(respx_mock, make_client) -> None:
    """Re-read shows the pre-write state -> provably a no-op -> the original
    transport error is re-raised as a clean (non-ambiguous) failure."""
    client = await make_client()
    with pytest.raises(UniFiTransportTimeoutError):
        await _run(respx_mock, client, WIDGET_RAW)
    assert respx_mock.put(f"{BASE}{WIDGET_PATH}").call_count == 1


async def test_ambiguous_put_unresolved_third_state(respx_mock, make_client) -> None:
    """Re-read shows a state that is neither pre-write nor post-write -> this
    call cannot be proven responsible -> AmbiguousWriteOutcomeError with the
    current hash."""
    client = await make_client()
    third = dict(WIDGET_RAW)
    third["name"] = "changed by someone else"
    with pytest.raises(AmbiguousWriteOutcomeError) as excinfo:
        await _run(respx_mock, client, third)
    err = excinfo.value
    assert err.to_dict()["error"] == "write_outcome_unknown"
    assert err.to_dict()["current_state_hash"] == compute_state_hash(third)
