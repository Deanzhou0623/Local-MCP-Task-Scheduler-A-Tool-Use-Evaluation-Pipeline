"""MCP surface: the tool registry and the FastMCP server.

Importing this package pulls in only the lightweight registry. The FastMCP
server (which registers tools and emits name-format warnings) lives in
``app.mcp.server`` and is imported explicitly when running the process.
"""

from app.mcp.registry import TOOL_REGISTRY, dispatch

__all__ = ["TOOL_REGISTRY", "dispatch"]
