"""Runtime configuration (environment variables via pydantic-settings)."""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path
from typing import Literal
from urllib.parse import urlsplit

from pydantic import Field, SecretStr, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """All runtime configuration, sourced from environment variables.

    Precedence: process environment > `.env` file > field defaults.
    """

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    # --- UniFi Network ---
    unifi_base_url: str
    unifi_api_key: SecretStr
    unifi_site_id: str | None = None
    unifi_tls_mode: Literal["strict", "extract-once", "insecure"] = "extract-once"
    unifi_ca_bundle: Path | None = None
    unifi_timeout_seconds: float = Field(default=15.0, gt=0)
    unifi_connect_timeout_seconds: float = Field(default=5.0, gt=0)
    unifi_max_retries: int = Field(default=2, ge=0)

    # --- MCP server ---
    mcp_auth_token: SecretStr
    mcp_bind_host: str = "0.0.0.0"
    mcp_bind_port: int = Field(default=8000, ge=1, le=65535)

    # --- Logging ---
    log_level: Literal["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"] = "INFO"
    log_format: Literal["json", "text"] = "json"

    # --- Feature flags (MVP: all off) ---
    enable_write_tools: bool = False
    enable_action_tools: bool = False
    enable_delete_tools: bool = False

    # --- Limits ---
    max_list_items: int = Field(default=200, gt=0)
    max_tool_response_bytes: int = Field(default=262144, gt=0)

    # --- Write audit log (design §27) ---
    # Empty = stdout only (default). Set to a file path (e.g. a host mount
    # at /var/log/unifi-mcp/audit.jsonl) to additionally append every write
    # attempt as JSONL; the parent directory must exist.
    audit_log_path: str = ""

    @field_validator("unifi_base_url")
    @classmethod
    def _check_base_url(cls, value: str) -> str:
        url = value.strip().rstrip("/")
        parts = urlsplit(url)
        if parts.scheme not in {"http", "https"} or not parts.netloc:
            msg = "unifi_base_url must be an absolute http(s) URL"
            raise ValueError(msg)
        return url

    @field_validator("unifi_site_id")
    @classmethod
    def _empty_site_id_to_none(cls, value: str | None) -> str | None:
        # `UNIFI_SITE_ID=` (empty) means "no explicit site", not an
        # empty site id that would build broken site-scoped URLs.
        if value is not None and not value.strip():
            return None
        return value

    @model_validator(mode="after")
    def _check_tls(self) -> Settings:
        if self.unifi_tls_mode == "strict" and self.unifi_ca_bundle is None:
            msg = "unifi_ca_bundle is required when unifi_tls_mode is 'strict'"
            raise ValueError(msg)
        return self


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """Return the cached process-wide settings instance."""
    return Settings()  # type: ignore[call-arg]
