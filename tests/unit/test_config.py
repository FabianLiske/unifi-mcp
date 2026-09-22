"""Unit tests for unifi_mcp.config."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from unifi_mcp.config import Settings, get_settings


def _settings(**overrides: object) -> Settings:
    base = {
        "unifi_base_url": "https://172.26.1.1",
        "unifi_api_key": "test-key",
        "mcp_auth_token": "test-token",
    }
    base.update(overrides)
    return Settings(_env_file=None, **base)


def test_defaults(clean_env) -> None:
    s = _settings()
    assert s.unifi_base_url == "https://172.26.1.1"
    assert s.unifi_api_key.get_secret_value() == "test-key"
    assert s.unifi_site_id is None
    assert s.unifi_tls_mode == "extract-once"
    assert s.unifi_ca_bundle is None
    assert s.unifi_timeout_seconds == 15.0
    assert s.unifi_connect_timeout_seconds == 5.0
    assert s.unifi_max_retries == 2
    assert s.mcp_auth_token.get_secret_value() == "test-token"
    assert s.mcp_bind_host == "0.0.0.0"
    assert s.mcp_bind_port == 8000
    assert s.log_level == "INFO"
    assert s.log_format == "json"
    assert s.enable_write_tools is False
    assert s.enable_action_tools is False
    assert s.enable_delete_tools is False
    assert s.max_list_items == 200
    assert s.max_tool_response_bytes == 262144
    assert s.audit_log_path == ""


def test_base_url_strips_trailing_slash(clean_env) -> None:
    s = _settings(unifi_base_url="https://172.26.1.1/")
    assert s.unifi_base_url == "https://172.26.1.1"


def test_empty_site_id_becomes_none(clean_env) -> None:
    assert _settings(unifi_site_id="").unifi_site_id is None
    assert _settings(unifi_site_id="   ").unifi_site_id is None
    assert _settings(unifi_site_id="abc-123").unifi_site_id == "abc-123"


def test_base_url_requires_http_scheme(clean_env) -> None:
    with pytest.raises(ValidationError):
        _settings(unifi_base_url="172.26.1.1")
    with pytest.raises(ValidationError):
        _settings(unifi_base_url="ftp://172.26.1.1")


def test_strict_mode_requires_ca_bundle(clean_env) -> None:
    with pytest.raises(ValidationError):
        _settings(unifi_tls_mode="strict")
    s = _settings(unifi_tls_mode="strict", unifi_ca_bundle="certs/gateway.crt")
    assert str(s.unifi_ca_bundle) == "certs/gateway.crt"


def test_invalid_tls_mode(clean_env) -> None:
    with pytest.raises(ValidationError):
        _settings(unifi_tls_mode="maybe")


def test_env_overrides(clean_env) -> None:
    clean_env.setenv("UNIFI_BASE_URL", "https://env.example")
    clean_env.setenv("UNIFI_API_KEY", "env-key")
    clean_env.setenv("MCP_AUTH_TOKEN", "env-token")
    s = Settings(_env_file=None)
    assert s.unifi_base_url == "https://env.example"
    assert s.unifi_api_key.get_secret_value() == "env-key"
    assert s.mcp_auth_token.get_secret_value() == "env-token"


def test_bool_flag_parsing(clean_env) -> None:
    assert _settings(enable_write_tools="true").enable_write_tools is True
    assert _settings(enable_write_tools="false").enable_write_tools is False
    assert _settings(enable_write_tools="0").enable_write_tools is False
    assert _settings(enable_write_tools="1").enable_write_tools is True
    assert _settings(enable_action_tools="yes").enable_action_tools is True


def test_port_bounds(clean_env) -> None:
    with pytest.raises(ValidationError):
        _settings(mcp_bind_port=70000)
    with pytest.raises(ValidationError):
        _settings(mcp_bind_port=0)


def test_positive_limits(clean_env) -> None:
    with pytest.raises(ValidationError):
        _settings(max_list_items=0)
    with pytest.raises(ValidationError):
        _settings(unifi_timeout_seconds=0)


def test_dotenv_loading(clean_env, tmp_path) -> None:
    env_file = tmp_path / ".env"
    lines = [
        "UNIFI_BASE_URL=https://dotenv.example",
        "UNIFI_API_KEY=dv-key",
        "MCP_AUTH_TOKEN=dv-token",
    ]
    env_file.write_text("\n".join(lines) + "\n")
    s = Settings(_env_file=str(env_file))
    assert s.unifi_base_url == "https://dotenv.example"
    assert s.unifi_api_key.get_secret_value() == "dv-key"


def test_get_settings_cached(clean_env, tmp_path) -> None:
    clean_env.chdir(tmp_path)
    get_settings.cache_clear()
    clean_env.setenv("UNIFI_BASE_URL", "https://a.example")
    clean_env.setenv("UNIFI_API_KEY", "k")
    clean_env.setenv("MCP_AUTH_TOKEN", "t")
    first = get_settings()
    clean_env.setenv("UNIFI_BASE_URL", "https://b.example")
    second = get_settings()
    assert first is second
    get_settings.cache_clear()
