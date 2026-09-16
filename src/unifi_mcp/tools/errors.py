"""Structured, LLM-friendly tool errors and the tool-output size limit.

Design doc §30: tool failures should be short structured JSON, not raw HTTP
errors (``not_found``, ``ambiguous_match``). Design doc §12: tool output has
a hard size limit, exceeded with a ``response_too_large`` error.

Tool code should catch :class:`ToolError` and return ``error.to_dict()`` as
the tool result.
"""

from __future__ import annotations

import json
from typing import Any


class ToolError(Exception):
    """Base class for structured errors returned to the LLM.

    ``to_dict()`` yields ``{"error": <code>, ...fields, "message": <text>}``
    with the code first and the message last (design §30).
    """

    code: str = "tool_error"

    def __init__(self, message: str, **fields: Any) -> None:
        super().__init__(message)
        self.message = message
        self.fields = fields

    def to_dict(self) -> dict[str, Any]:
        out: dict[str, Any] = {"error": self.code}
        out.update(self.fields)
        out["message"] = self.message
        return out


class NotFoundError(ToolError):
    """No resource matched the supplied identifier (design §30)."""

    code = "not_found"

    def __init__(self, resource: str, query: str, message: str | None = None) -> None:
        default = f"No {resource} matched the supplied identifier."
        super().__init__(message or default, resource=resource, query=query)


class AmbiguousMatchError(ToolError):
    """Several resources matched; the caller must disambiguate (design §30)."""

    code = "ambiguous_match"

    def __init__(self, matches: list[dict[str, Any]], message: str | None = None) -> None:
        default = "Multiple resources matched; use an exact identifier."
        super().__init__(message or default, matches=matches)


class ResponseTooLargeError(ToolError):
    """Tool output exceeded the hard response size limit (design §12)."""

    code = "response_too_large"

    def __init__(self, message: str | None = None) -> None:
        super().__init__(message or "Narrow the query or use pagination.")


def check_response_size(payload: Any, *, max_bytes: int) -> int:
    """Raise :class:`ResponseTooLargeError` if the JSON size exceeds *max_bytes*.

    Returns the serialized UTF-8 size in bytes when the limit is respected.
    """
    size = len(json.dumps(payload, separators=(",", ":"), ensure_ascii=False).encode("utf-8"))
    if size > max_bytes:
        raise ResponseTooLargeError()
    return size
