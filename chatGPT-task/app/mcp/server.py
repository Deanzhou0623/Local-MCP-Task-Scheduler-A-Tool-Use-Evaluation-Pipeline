"""Real MCP server for the task scheduler.

Run with::

    python -m app.mcp.server

Each FastMCP tool is a thin wrapper that builds an ``args`` dict and routes it
through :data:`app.mcp.registry.TOOL_REGISTRY`. The registry is the single
dispatch point shared with the eval pipeline, so the MCP surface and evals
exercise the exact same handlers.

MCP stdio uses stdout for protocol messages, so this module must never call
``print()`` to stdout. Diagnostics go to stderr.
"""

from __future__ import annotations

import sys
from typing import Optional

from dotenv import load_dotenv
from mcp.server.fastmcp import FastMCP

from app.core.database import Base, engine
from app.jobs import models  # noqa: F401 - register tables
from app.mcp.registry import dispatch
from app.scheduler import start_scheduler

load_dotenv()

mcp = FastMCP("task-scheduler")


@mcp.tool(
    name="task.create@v1",
    description=(
        "Create a scheduled job and its first run. job_params.type is one of "
        "immediate, one_time, or recurring."
    ),
)
def task_create(user_id: str, action: str, job_params: dict) -> dict:
    return dispatch(
        "task.create@v1",
        {"user_id": user_id, "action": action, "job_params": job_params},
    )


@mcp.tool(name="task.list@v1", description="List jobs for a user.")
def task_list(
    user_id: str,
    status: Optional[str] = None,
    start_time: Optional[str] = None,
    end_time: Optional[str] = None,
    page_size: int = 20,
    page: int = 1,
) -> dict:
    return dispatch(
        "task.list@v1",
        {
            "user_id": user_id,
            "status": status,
            "start_time": start_time,
            "end_time": end_time,
            "page_size": page_size,
            "page": page,
        },
    )


@mcp.tool(name="task.get@v1", description="View one job and its recent runs.")
def task_get(user_id: str, job_id: str) -> dict:
    return dispatch("task.get@v1", {"user_id": user_id, "job_id": job_id})


@mcp.tool(name="task.modify@v1", description="Modify an existing job.")
def task_modify(
    user_id: str,
    job_id: str,
    action: Optional[str] = None,
    status: Optional[str] = None,
    job_params: Optional[dict] = None,
) -> dict:
    return dispatch(
        "task.modify@v1",
        {
            "user_id": user_id,
            "job_id": job_id,
            "action": action,
            "status": status,
            "job_params": job_params,
        },
    )


@mcp.tool(name="task.delete@v1", description="Soft-delete an existing job.")
def task_delete(user_id: str, job_id: str) -> dict:
    return dispatch("task.delete@v1", {"user_id": user_id, "job_id": job_id})


def _startup() -> None:
    Base.metadata.create_all(bind=engine)
    start_scheduler()
    print("task-scheduler MCP server starting", file=sys.stderr)


if __name__ == "__main__":
    _startup()
    mcp.run()
