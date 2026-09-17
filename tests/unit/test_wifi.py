"""Unit tests for unifi_mcp.tools.wifi (design §10.6).

The central safety property (§10.6 "Nie PSK zurückgeben"): the raw WiFi
passphrase stored in the fixtures (``securityConfiguration.passphrase``) must
never appear in any tool output — at summary *and* detail level.
"""

from __future__ import annotations

import json

import httpx
from mcp_types import ToolAnnotations

from unifi_mcp.safety.redaction import REDACTED
from unifi_mcp.tools.wifi import register_wifi_tools

BASE = "http://gateway.test/proxy/network/integration/v1"
SITE = "site-test"

RAW_PSKS = ("fixture-wifi-psk-12345678", "fixture-wifi-psk-87654321")


class _RecordingServer:
    """Captures the (wrapped) tool callables registered on the server."""

    def __init__(self) -> None:
        self.tools: dict[str, dict] = {}

    def add_tool(
        self,
        fn,
        name: str | None = None,
        title: str | None = None,
        description: str | None = None,
        annotations: ToolAnnotations | None = None,
        **kwargs: object,
    ) -> None:
        self.tools[name or fn.__name__] = {
            "fn": fn,
            "title": title,
            "description": description,
            "annotations": annotations,
        }


async def _tools(make_client, make_settings) -> _RecordingServer:
    settings = make_settings(unifi_site_id=SITE)
    client = await make_client(settings)
    server = _RecordingServer()
    register_wifi_tools(server, client, settings)
    return server


def _assert_psk_absent(payload: object) -> str:
    raw = json.dumps(payload, ensure_ascii=False)
    for psk in RAW_PSKS:
        assert psk not in raw
    return raw


# --- registration -------------------------------------------------------------


async def test_register_wifi_tools(make_settings) -> None:
    settings = make_settings()
    server = _RecordingServer()
    register_wifi_tools(server, object(), settings)  # type: ignore[arg-type]
    assert list(server.tools) == ["list_wifi", "get_wifi"]
    for entry in server.tools.values():
        assert entry["annotations"] is not None
        assert entry["annotations"].read_only_hint is True
        assert entry["title"]
        assert "read-only" in entry["description"]


async def test_wifi_schema_params(make_client, make_settings) -> None:
    import inspect

    server = await _tools(make_client, make_settings)
    params = inspect.signature(server.tools["list_wifi"]["fn"]).parameters
    assert set(params) == {"site_id", "limit", "offset"}
    params = inspect.signature(server.tools["get_wifi"]["fn"]).parameters
    assert set(params) == {"broadcast_id", "site_id"}


# --- list_wifi ------------------------------------------------------------------


async def test_list_wifi_envelope_and_redaction(
    respx_mock, make_client, make_settings, load_fixture
) -> None:
    server = await _tools(make_client, make_settings)
    respx_mock.get(f"{BASE}/sites/{SITE}/wifi/broadcasts").mock(
        return_value=httpx.Response(200, json=load_fixture("wifi.json"))
    )
    result = await server.tools["list_wifi"]["fn"](limit=5, offset=0)
    raw = _assert_psk_absent(result)
    assert result["count"] == 2
    assert result["total_count"] == 2
    assert result["next_offset"] is None
    first = result["items"][0]
    assert first["id"] == "bc-iot"
    assert first["name"] == "ID-IoT"
    assert first["network"] == {"type": "SPECIFIC", "networkId": "net-iot"}
    # the PSK field exists but is redacted, never raw
    assert first["securityConfiguration"]["passphrase"] == REDACTED
    assert REDACTED in raw
    assert "metadata" not in first
    params = dict(respx_mock.calls.last.request.url.params)
    assert params["limit"] == "5"
    assert params["offset"] == "0"


async def test_list_wifi_redacts_psk_even_without_other_fields(
    respx_mock, make_client, make_settings
) -> None:
    """A minimal broadcast object carrying a raw PSK is still redacted."""
    server = await _tools(make_client, make_settings)
    page = {
        "offset": 0,
        "limit": 50,
        "count": 1,
        "totalCount": 1,
        "data": [
            {
                "id": "bc-x",
                "name": "Hidden",
                "securityConfiguration": {
                    "type": "WPA2_PERSONAL",
                    "passphrase": "fixture-wifi-psk-12345678",
                },
            }
        ],
    }
    respx_mock.get(f"{BASE}/sites/{SITE}/wifi/broadcasts").mock(
        return_value=httpx.Response(200, json=page)
    )
    result = await server.tools["list_wifi"]["fn"]()
    raw = _assert_psk_absent(result)
    assert result["items"][0]["securityConfiguration"]["passphrase"] == REDACTED
    assert REDACTED in raw


# --- get_wifi -------------------------------------------------------------------


async def test_get_wifi_detail_redacts_psk(
    respx_mock, make_client, make_settings, load_fixture
) -> None:
    server = await _tools(make_client, make_settings)
    fixture = load_fixture("wifi.json")
    detail = fixture["data"][0]
    assert detail["securityConfiguration"]["passphrase"] == "fixture-wifi-psk-12345678"
    respx_mock.get(f"{BASE}/sites/{SITE}/wifi/broadcasts/bc-iot").mock(
        return_value=httpx.Response(200, json=detail)
    )
    result = await server.tools["get_wifi"]["fn"](broadcast_id="bc-iot")
    raw = _assert_psk_absent(result)
    # detail keeps the security block, with the PSK redacted
    assert result["securityConfiguration"]["type"] == "WPA2_PERSONAL"
    assert result["securityConfiguration"]["passphrase"] == REDACTED
    assert REDACTED in raw
    # non-secret fields are intact
    assert result["name"] == "ID-IoT"
    assert result["type"] == "IOT_OPTIMIZED"
    assert result["network"] == {"type": "SPECIFIC", "networkId": "net-iot"}
    assert "metadata" not in result


async def test_get_wifi_not_found(respx_mock, make_client, make_settings) -> None:
    server = await _tools(make_client, make_settings)
    respx_mock.get(f"{BASE}/sites/{SITE}/wifi/broadcasts/nope").mock(
        return_value=httpx.Response(404, json={"code": "not-found", "message": "Not found."})
    )
    result = await server.tools["get_wifi"]["fn"](broadcast_id="nope")
    assert result["error"] == "not_found"
    assert result["resource"] == "wifi_broadcast"
    assert result["query"] == "nope"
