"""UniFi Network MCP Server.

Controlled adapter between MCP and the official local UniFi Network API.
The MVP is read-only: semantic, filtered and redacted read tools.
"""

from unifi_mcp.app import create_app

__version__ = "0.1.0"

__all__ = ["create_app", "__version__"]
