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


def _tool_items():
    schemas = mcp_tool_schemas()
    for name in PUBLIC_TOOLS:
        tool = mcp._tool_manager._tools.get(name)
        if tool is not None:
            yield name, (tool.description or ""), schemas[name]


def openai_tool_defs() -> list[dict]:
    """OpenAI Responses-API function tool definitions for the scheduler tools."""
    return [
        {"type": "function", "name": name, "description": desc, "parameters": schema}
        for name, desc, schema in _tool_items()
    ]


def anthropic_tool_defs() -> list[dict]:
    """Anthropic Messages-API tool definitions (``input_schema`` shape)."""
    return [
        {"name": name, "description": desc, "input_schema": schema}
        for name, desc, schema in _tool_items()
    ]


def gemini_tool_defs() -> list[dict]:
    """Gemini function-declaration definitions (OpenAPI-subset ``parameters``).

    Gemini rejects several JSON-Schema keywords, so schemas are sanitized. This
    is best-effort and should be verified against a live key.
    """
    return [
        {"name": name, "description": desc, "parameters": _sanitize_gemini(schema)}
        for name, desc, schema in _tool_items()
    ]


def _sanitize_gemini(schema: dict):
    """Drop JSON-Schema keywords Gemini's function schema does not accept.

    Removes ``additionalProperties``/``$schema``/``title`` and collapses an
    ``anyOf: [T, null]`` (Optional) into a single nullable type.
    """
    if not isinstance(schema, dict):
        return schema
    if "anyOf" in schema:
        variants = [v for v in schema["anyOf"] if v.get("type") != "null"]
        nullable = any(v.get("type") == "null" for v in schema["anyOf"])
        base = dict(variants[0]) if variants else {}
        if nullable:
            base["nullable"] = True
        return _sanitize_gemini(base)
    out = {}
    for key, value in schema.items():
        if key in ("additionalProperties", "$schema", "title"):
            continue
        if key == "properties" and isinstance(value, dict):
            out[key] = {k: _sanitize_gemini(v) for k, v in value.items()}
        elif key == "items":
            out[key] = _sanitize_gemini(value)
        else:
            out[key] = value
    return out
