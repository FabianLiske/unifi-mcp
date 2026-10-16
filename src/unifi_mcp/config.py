"""Runtime configuration (environment variables via pydantic-settings).

Timeout / retry budget (design §44)
-----------------------------------
Every UniFi request and every retry is bounded so a single MCP tool call
always *completes and reports* before the external LiteLLM/MCP client
deadline (:data:`EXTERNAL_MCP_TOOL_TIMEOUT_SECONDS`). See
:func:`worst_case_read_budget` for the arithmetic and
:mod:`unifi_mcp.budget` for the runtime enforcement.
"""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path
from typing import Literal
from urllib.parse import urlsplit

from pydantic import Field, SecretStr, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

#: External MCP / LiteLLM tool-call deadline (seconds).
#:
#: The LiteLLM proxy that fronts this MCP server enforces a per-tool-call
#: deadline on the MCP client side (``LITELLM_MCP_CLIENT_TIMEOUT``; default 60 s
#: in the deployed v1.98.0 image). If a single unifi-mcp tool call exceeds that,
#: the LiteLLM client aborts the call *while unifi-mcp may still be mid-retry*
#: — the classic retry-amplification / unknown-write-outcome failure (F05).
#: The internal per-tool-call budget must stay strictly below this value.
EXTERNAL_MCP_TOOL_TIMEOUT_SECONDS: float = 60.0

#: Default per-tool-call wall-clock budget (seconds). Strictly below the
#: external deadline (:data:`EXTERNAL_MCP_TOOL_TIMEOUT_SECONDS`) with headroom,
#: and large enough to accommodate one worst-case retrying read at the default
#: timeouts (see :func:`worst_case_read_budget`). The
#: :attr:`Settings.unifi_tool_call_budget_seconds` field defaults to this.
DEFAULT_TOOL_CALL_BUDGET_SECONDS: float = 45.0


def worst_case_read_budget(
    *,
    read: float,
    connect: float,
    retries: int,
    backoff_base: float,
    backoff_cap: float,
) -> float:
    """Worst-case wall-clock of one *retrying read* request.

    Sum of every attempt's connect + read time plus every backoff between
    attempts. Pool-acquire wait is deliberately *excluded* here: it is bounded
    separately by ``unifi_pool_timeout_seconds`` and enforced at runtime by the
    per-tool-call budget guard (it is not part of the request's own duration).

    Backoff uses the full-jitter *cap*: between attempts the delay is at most
    ``min(backoff_base * 2**i, backoff_cap)`` seconds, so the worst case sums
    those ceilings.

    This is the building block for the per-tool-call budget: a single retrying
    read must fit inside ``unifi_tool_call_budget_seconds`` (validated below),
    and the runtime budget guard additionally bounds any multi-request tool
    call to the same total.
    """
    attempts = 1 + retries
    per_attempt = connect + read
    backoff_total = sum(min(backoff_base * (2.0**i), backoff_cap) for i in range(retries))
    return attempts * per_attempt + backoff_total


def max_retries_for_budget(
    *,
    read: float,
    connect: float,
    backoff_base: float,
    backoff_cap: float,
    tool_call_budget: float,
) -> int:
    """Largest retry count whose worst-case read budget fits *tool_call_budget*.

    Used to give an actionable recommendation when the operator overrides the
    timeouts to values that no longer fit the current retry count.
    """
    retries = 0
    while (
        worst_case_read_budget(
            read=read,
            connect=connect,
            retries=retries,
            backoff_base=backoff_base,
            backoff_cap=backoff_cap,
        )
        <= tool_call_budget
    ):
        retries += 1
    return max(0, retries - 1)


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
    # Per-attempt timeouts (design §7/§44):
    #   read    — max time to wait for the response body on one attempt
    #   connect — max time to establish a TCP connection on one attempt
    #   pool    — max time to wait for a free pooled connection (bounded, so a
    #             saturated pool surfaces as a bounded error instead of a
    #             silent multi-minute stall)
    unifi_timeout_seconds: float = Field(default=15.0, gt=0)
    unifi_connect_timeout_seconds: float = Field(default=5.0, gt=0)
    unifi_pool_timeout_seconds: float = Field(default=5.0, gt=0)
    # Bounded retries: applies to suitable Reads (GET, on both transient status
    # and transport errors) and to PUT on transient *status* only. Mutating
    # transport-failure outcomes are never blindly retried (F05). With the
    # defaults, one worst-case retrying read is
    # (1+retries)*(connect+read) + backoff <= 40.5 s < the 45 s tool-call
    # budget, and the budget stays < the 60 s external deadline.
    unifi_max_retries: int = Field(default=1, ge=0, le=5)
    # Exponential backoff base + cap (seconds). Full jitter is applied at the
    # client: delay = uniform(0, min(base * 2**attempt, cap)).
    unifi_retry_backoff_base_seconds: float = Field(default=0.5, gt=0)
    unifi_retry_backoff_cap_seconds: float = Field(default=2.0, gt=0)
    # Bounded concurrency: max in-flight gateway requests per client. Caps the
    # fan-out a single tool call (or a burst of concurrent calls) can generate,
    # so a burst cannot saturate the connection pool and amplify a degraded
    # gateway (F01/F05).
    unifi_max_concurrent_requests: int = Field(default=8, ge=1, le=64)
    # Per-tool-call wall-clock budget (seconds). A single MCP tool call may
    # fan out into several sequential gateway requests; this bounds the *total*
    # so the call completes and reports before the external LiteLLM/MCP client
    # deadline (60 s). Must be < EXTERNAL_MCP_TOOL_TIMEOUT_SECONDS and must
    # accommodate the worst-case of a single retrying read (validated below).
    unifi_tool_call_budget_seconds: float = Field(default=DEFAULT_TOOL_CALL_BUDGET_SECONDS, gt=0)

    # --- MCP server ---
    mcp_auth_token: SecretStr
    mcp_bind_host: str = "0.0.0.0"
    mcp_bind_port: int = Field(default=8000, ge=1, le=65535)
    # Human-readable instance identity (e.g. "local-dev", "production")
    # injected into the server title and instructions so clients can tell
    # multiple instances of this deployment apart.
    mcp_instance_label: str = "production"

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

    @field_validator("mcp_instance_label")
    @classmethod
    def _empty_instance_label_to_default(cls, value: str) -> str:
        # `MCP_INSTANCE_LABEL=` (empty/whitespace) means "unset", not an
        # empty label that would render as "UniFi Network MCP ()".
        return value.strip() or "production"

    @model_validator(mode="after")
    def _check_tls(self) -> Settings:
        if self.unifi_tls_mode == "strict" and self.unifi_ca_bundle is None:
            msg = "unifi_ca_bundle is required when unifi_tls_mode is 'strict'"
            raise ValueError(msg)
        return self

    @model_validator(mode="after")
    def _check_retry_budget(self) -> Settings:
        """Keep the internal retry budget inside the external deadline.

        Two invariants (design §44 / F05):

        1. The per-tool-call budget must stay strictly below the external
           LiteLLM/MCP tool-call deadline, or the client aborts mid-retry.
        2. A single worst-case retrying read must fit inside the per-tool-call
           budget, or the client burns its budget on one request and can never
           complete a multi-request tool call.

        Violations are a configuration error: they fail startup (or the test
        that built the Settings) instead of silently misbehaving at runtime.
        """
        if self.unifi_tool_call_budget_seconds >= EXTERNAL_MCP_TOOL_TIMEOUT_SECONDS:
            msg = (
                "unifi_tool_call_budget_seconds "
                f"({self.unifi_tool_call_budget_seconds}) must be strictly below the "
                f"external MCP/LiteLLM tool-call deadline "
                f"({EXTERNAL_MCP_TOOL_TIMEOUT_SECONDS} s); otherwise the LiteLLM client "
                "aborts the call mid-retry (retry amplification / unknown write "
                "outcome). Lower it (e.g. 45) or reduce the per-attempt timeouts."
            )
            raise ValueError(msg)
        wc = worst_case_read_budget(
            read=self.unifi_timeout_seconds,
            connect=self.unifi_connect_timeout_seconds,
            retries=self.unifi_max_retries,
            backoff_base=self.unifi_retry_backoff_base_seconds,
            backoff_cap=self.unifi_retry_backoff_cap_seconds,
        )
        if wc > self.unifi_tool_call_budget_seconds:
            suggested = max_retries_for_budget(
                read=self.unifi_timeout_seconds,
                connect=self.unifi_connect_timeout_seconds,
                backoff_base=self.unifi_retry_backoff_base_seconds,
                backoff_cap=self.unifi_retry_backoff_cap_seconds,
                tool_call_budget=self.unifi_tool_call_budget_seconds,
            )
            msg = (
                f"worst-case retrying read budget ({wc:.1f} s with read="
                f"{self.unifi_timeout_seconds}s, connect="
                f"{self.unifi_connect_timeout_seconds}s, max_retries="
                f"{self.unifi_max_retries}) exceeds unifi_tool_call_budget_seconds "
                f"({self.unifi_tool_call_budget_seconds} s); a single request would burn "
                "the whole tool-call budget. Lower the per-attempt timeouts or the "
                f"retry count (unifi_max_retries <= {suggested} fits), or raise the "
                f"budget (but keep it < {EXTERNAL_MCP_TOOL_TIMEOUT_SECONDS} s)."
            )
            raise ValueError(msg)
        return self


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """Return the cached process-wide settings instance."""
    return Settings()  # type: ignore[call-arg]
