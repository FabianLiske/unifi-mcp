"""Unit tests for unifi_mcp.safety.redaction."""

from __future__ import annotations

import copy

from unifi_mcp.safety.redaction import (
    REDACTED,
    is_secret_key,
    normalize_key,
    redact,
)

# (field name, expected) covering the design §8 list case-insensitively.
_EXACT_CASES: dict[str, bool] = {
    "password": True,
    "Password": True,
    "PASSWORD": True,
    "passphrase": True,
    "PSK": True,
    "psk": True,
    "Psk": True,
    "secret": True,
    "token": True,
    "apiKey": True,
    "api_key": True,
    "API_KEY": True,
    "privateKey": True,
    "private_key": True,
    "credential": True,
    "authorization": True,
    # Prefixed variants must be redacted too.
    "wpaPsk": True,
    "wpa_psk": True,
    "x_passphrase": True,
    "preSharedKey": True,
    "clientSecret": True,
    "accessToken": True,
    # Non-secrets must survive.
    "name": False,
    "hostname": False,
    "ipAddress": False,
    "macAddress": False,
    "state": False,
    "tokenCount": False,  # ends in "count", not a secret
    "secretary": False,  # starts with "secret" but is not one
    "keyboard": False,
    "id": False,
}


def test_is_secret_key_matrix() -> None:
    for name, expected in _EXACT_CASES.items():
        assert is_secret_key(name) is expected, name


def test_is_secret_key_rejects_non_strings() -> None:
    assert is_secret_key(123) is False
    assert is_secret_key(None) is False
    assert is_secret_key("") is False


def test_normalize_key_strips_separators_and_case() -> None:
    assert normalize_key("apiKey") == "apikey"
    assert normalize_key("api_key") == "apikey"
    assert normalize_key("API-Key") == "apikey"
    assert normalize_key("privateKey") == "privatekey"


def test_redact_replaces_value_keeps_key() -> None:
    result = redact({"password": "hunter2", "name": "wk-5"})
    assert result == {"password": REDACTED, "name": "wk-5"}


def test_redact_case_insensitive() -> None:
    result = redact({"Password": "x", "PSK": "y", "apiKey": "z"})
    assert result == {"Password": REDACTED, "PSK": REDACTED, "apiKey": REDACTED}


def test_redact_recurses_into_nested_dicts() -> None:
    payload = {"securityConfiguration": {"passphrase": "topsecret", "type": "wpa2-personal"}}
    result = redact(payload)
    assert result["securityConfiguration"]["passphrase"] == REDACTED
    assert result["securityConfiguration"]["type"] == "wpa2-personal"


def test_redact_recurses_into_lists_of_dicts() -> None:
    payload = {"clients": [{"hostname": "a", "token": "t1"}, {"hostname": "b", "apiKey": "k"}]}
    result = redact(payload)
    assert result["clients"][0] == {"hostname": "a", "token": REDACTED}
    assert result["clients"][1] == {"hostname": "b", "apiKey": REDACTED}


def test_redact_real_wifi_passphrase_path() -> None:
    # Live-verified shape: the PSK lives at securityConfiguration.passphrase.
    payload = {
        "id": "bc1",
        "name": "MyWifi",
        "securityConfiguration": {"passphrase": "Sup3r-Secret-PSK", "type": "wpa2-personal"},
    }
    result = redact(payload)
    assert result["securityConfiguration"]["passphrase"] == REDACTED
    assert "Sup3r-Secret-PSK" not in str(result)


def test_redact_replaces_non_string_secret_values() -> None:
    result = redact({"token": None, "apiKey": 12345, "password": ["a", "b"]})
    assert result == {"token": REDACTED, "apiKey": REDACTED, "password": REDACTED}


def test_redact_does_not_mutate_input() -> None:
    original = {"password": "secret", "nested": {"psk": "x", "ok": 1}}
    snapshot = copy.deepcopy(original)
    redact(original)
    assert original == snapshot


def test_redact_passthrough_for_scalars() -> None:
    assert redact("plain") == "plain"
    assert redact(42) == 42
    assert redact(None) is None
    assert redact([1, 2, 3]) == [1, 2, 3]


def test_redact_deeply_nested_list_in_dict_in_list() -> None:
    payload = [[{"authorization": "Bearer x"}], {"credential": "c"}]
    result = redact(payload)
    assert result[0][0]["authorization"] == REDACTED
    assert result[1]["credential"] == REDACTED
