"""Adapter from the MCP public tool schemas to OpenAI Responses tool defs.

Evals must test the *same* tool surface real clients see, so schemas are read
straight from the running FastMCP tool manager. A schema change therefore shows
up as an eval regression/improvement rather than drifting silently.
"""

from __future__ import annotations

from app.mcp.server import mcp

# The public tool surface under evaluation (task_runs_list_v1 added in spec 06).
PUBLIC_TOOLS = [
    "task_create_v1",
    "task_list_v1",
    "task_get_v1",
    "task_modify_v1",
    "task_delete_v1",
    "task_trace_get_v1",
    "task_runs_list_v1",
]


def mcp_tool_schemas() -> dict[str, dict]:
    """Return {tool_name: json_schema} as registered on the MCP server."""
    out: dict[str, dict] = {}
    for name, tool in mcp._tool_manager._tools.items():
        out[name] = tool.parameters
    return out


def openai_tool_defs() -> list[dict]:
    """OpenAI Responses-API function tool definitions for the scheduler tools."""
    schemas = mcp_tool_schemas()
    defs = []
    for name in PUBLIC_TOOLS:
        tool = mcp._tool_manager._tools.get(name)
        if tool is None:
            continue
        defs.append(
            {
                "type": "function",
                "name": name,
                "description": tool.description or "",
                "parameters": schemas[name],
            }
        )
    return defs
