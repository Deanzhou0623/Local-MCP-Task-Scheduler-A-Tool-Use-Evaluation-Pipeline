"""Spec 02: automated MCP-protocol checks (the MCP Inspector flow, in-process).

These mirror the manual MCP Inspector tests in spec 02 sections 10 and 14, but
drive the *real* server over an in-memory client<->server session from the MCP
SDK — the same `tools/list` and `tools/call` requests Inspector would send.
This guards the protocol surface: exactly the five strict task tools, no
inference tool, and structured-JSON results / error envelopes.

DB isolation: the tools open sessions via ``app.mcp.registry.SessionLocal``,
which the ``isolated_db`` fixture repoints at an in-memory engine.
"""

from __future__ import annotations

import json

import anyio
from mcp.shared.memory import (
    create_connected_server_and_client_session as connect,
)

from app.mcp.server import mcp

USER = "demo-user"

EXPECTED_TOOLS = [
    "task.create@v1",
    "task.delete@v1",
    "task.get@v1",
    "task.list@v1",
    "task.modify@v1",
]


def _body(result) -> dict:
    """Extract the tool's JSON envelope from a CallToolResult."""
    if result.structuredContent:
        return result.structuredContent
    return json.loads(result.content[0].text)


def _run(scenario):
    """Run an async scenario(client) against a connected in-memory session."""

    async def go():
        async with connect(mcp._mcp_server) as client:
            await client.initialize()
            return await scenario(client)

    return anyio.run(go)


# --- Tool discovery (tools/list) ------------------------------------------
def test_tools_list_exposes_exactly_the_five_task_tools(isolated_db):
    async def scenario(client):
        return await client.list_tools()

    tools = _run(scenario)
    assert sorted(t.name for t in tools.tools) == EXPECTED_TOOLS
    # The strict scheduler exposes no inference tool.
    assert not any(t.name.startswith("time.") for t in tools.tools)


def test_create_tool_schema_declares_required_fields(isolated_db):
    async def scenario(client):
        return await client.list_tools()

    tools = _run(scenario)
    create = next(t for t in tools.tools if t.name == "task.create@v1")
    required = set(create.inputSchema.get("required", []))
    assert {"user_id", "action", "job_params"} <= required


# --- Create ---------------------------------------------------------------
def test_create_immediate_returns_ok_with_job_id(isolated_db):
    async def scenario(client):
        return await client.call_tool(
            "task.create@v1",
            {"user_id": USER, "action": "generate_report",
             "job_params": {"type": "immediate"}},
        )

    body = _body(_run(scenario))
    assert body["ok"] is True
    assert body["job"]["job_id"].startswith("job_")


def test_create_one_time_keeps_explicit_timezone(isolated_db):
    async def scenario(client):
        return await client.call_tool(
            "task.create@v1",
            {"user_id": USER, "action": "summarize_financial_news",
             "job_params": {"type": "one_time", "time": "2026-06-10T08:00:00",
                            "timezone": "America/Vancouver"}},
        )

    body = _body(_run(scenario))
    assert body["ok"] is True
    assert body["job"]["timezone"] == "America/Vancouver"


# --- Strict error envelopes -----------------------------------------------
def test_missing_timezone_returns_validation_error(isolated_db):
    async def scenario(client):
        return await client.call_tool(
            "task.create@v1",
            {"user_id": USER, "action": "summarize_financial_news",
             "job_params": {"type": "one_time", "time": "2026-06-10T08:00:00"}},
        )

    body = _body(_run(scenario))
    assert body["ok"] is False
    assert body["error"]["code"] == "VALIDATION_ERROR"
    assert body["error"]["field"] == "job_params.timezone"


def test_unsupported_action_returns_unsupported_action(isolated_db):
    async def scenario(client):
        return await client.call_tool(
            "task.create@v1",
            {"user_id": USER, "action": "fetch_and_email_news",
             "job_params": {"type": "immediate"}},
        )

    body = _body(_run(scenario))
    assert body["ok"] is False
    assert body["error"]["code"] == "UNSUPPORTED_ACTION"


def test_unknown_job_params_field_is_rejected(isolated_db):
    async def scenario(client):
        return await client.call_tool(
            "task.create@v1",
            {"user_id": USER, "action": "generate_report",
             "job_params": {"type": "immediate", "mystery": 1}},
        )

    body = _body(_run(scenario))
    assert body["ok"] is False
    assert body["error"]["code"] == "VALIDATION_ERROR"


def test_get_missing_job_returns_not_found(isolated_db):
    async def scenario(client):
        return await client.call_tool(
            "task.get@v1", {"user_id": USER, "job_id": "job_does_not_exist"}
        )

    body = _body(_run(scenario))
    assert body["ok"] is False
    assert body["error"]["code"] == "NOT_FOUND"


# --- Full lifecycle: create -> list -> get -> modify -> delete ------------
def test_full_lifecycle_over_mcp(isolated_db):
    async def scenario(client):
        created = _body(await client.call_tool(
            "task.create@v1",
            {"user_id": USER, "action": "summarize_financial_news",
             "job_params": {"type": "recurring", "schedule": "0 8 * * *",
                            "timezone": "America/Vancouver"}},
        ))
        job_id = created["job"]["job_id"]

        listed = _body(await client.call_tool("task.list@v1", {"user_id": USER}))
        got = _body(await client.call_tool(
            "task.get@v1", {"user_id": USER, "job_id": job_id}))
        modified = _body(await client.call_tool(
            "task.modify@v1",
            {"user_id": USER, "job_id": job_id,
             "job_params": {"schedule": "30 8 * * *"}},
        ))
        deleted = _body(await client.call_tool(
            "task.delete@v1", {"user_id": USER, "job_id": job_id}))
        deleted_again = _body(await client.call_tool(
            "task.delete@v1", {"user_id": USER, "job_id": job_id}))
        return listed, got, modified, deleted, deleted_again

    listed, got, modified, deleted, deleted_again = _run(scenario)

    assert listed["pagination"]["total"] == 1
    assert got["ok"] is True
    assert modified["job"]["schedule"] == "30 8 * * *"
    assert modified["cancelled_run_count"] == 1
    assert deleted["status"] == "deleted"
    # Idempotent: re-deleting cancels nothing more.
    assert deleted_again["status"] == "deleted"
    assert deleted_again["cancelled_run_count"] == 0
