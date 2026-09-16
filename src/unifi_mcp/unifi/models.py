"""Typed models for the official local UniFi Network API (v10.4.57 reference)."""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, ConfigDict, Field


class Page(BaseModel):
    """Paging envelope returned by all list endpoints.

    Example (live-verified in WP-2)::

        {"offset": 0, "limit": 25, "count": 25, "totalCount": 47, "data": [...]}
    """

    model_config = ConfigDict(populate_by_name=True, extra="ignore")

    offset: int = 0
    limit: int = 25
    count: int = 0
    total_count: int = Field(default=0, alias="totalCount")
    data: list[Any] = Field(default_factory=list)


class ApplicationInfo(BaseModel):
    """Top-level `GET /info` payload."""

    model_config = ConfigDict(extra="ignore")

    application_version: str = Field(alias="applicationVersion")
