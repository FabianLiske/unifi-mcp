"""Synchronous console entry point for the production server.

Wraps :func:`unifi_mcp.app.main` so the package can declare a ``unifi-mcp``
console script (used as the Docker image ``ENTRYPOINT``) without the runpy
warning that ``python -m unifi_mcp.app`` triggers (the package ``__init__``
already imports :mod:`unifi_mcp.app`).
"""

from __future__ import annotations

import asyncio

from unifi_mcp.app import main


def run() -> None:
    """Run the async production entry point to completion."""
    asyncio.run(main())
