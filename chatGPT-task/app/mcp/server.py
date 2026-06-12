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
from typing import Annotated, Optional

from dotenv import load_dotenv
from mcp.server.fastmcp import FastMCP
from pydantic import Field

from app.core.database import Base, engine
from app.jobs import models  # noqa: F401 - register tables
from app.mcp.registry import dispatch
from app.scheduler import start_scheduler

load_dotenv()

mcp = FastMCP("task-scheduler")

PUBLIC_ACTIONS = [
    "send_reminder",
    "generate_report",
    "summarize_financial_news",
    "fetch_news",
    "send_email",
    "review_pr",
]
PUBLIC_JOB_TYPES = ["immediate", "one_time", "recurring"]
PUBLIC_EDITABLE_STATUSES = ["scheduled", "paused"]

ActionName = Annotated[
    str,
    Field(
        description=(
            "Supported actions: send_reminder, generate_report, "
            "summarize_financial_news, fetch_news, send_email, review_pr. "
            "Use send_reminder for ordinary reminder requests."
        )
    ),
]
JobTypeName = Annotated[
    str,
    Field(description="Job type. Use one of: immediate, one_time, recurring."),
]


def _normalize_action(action: str) -> str:
    """Map Claude's natural-language action guesses onto placeholder actions."""
    normalized = action.strip().lower().replace("-", "_").replace(" ", "_")
    if normalized in PUBLIC_ACTIONS:
        return normalized

    text = action.strip().lower()
    if "financial" in text and "news" in text:
        return "summarize_financial_news"
    if "report" in text:
        return "generate_report"
    if "email" in text:
        return "send_email"
    if "news" in text:
        return "fetch_news"
    tokens = set(text.replace("_", " ").replace("-", " ").split())
    if "pull request" in text or "pr" in tokens:
        return "review_pr"
    return "send_reminder"


@mcp.tool(
    name="task_create_v1",
    description=(
        "Create a scheduled job. Use action=send_reminder for reminder requests. "
        "For one_time jobs, set time and timezone. For recurring jobs, set "
        "schedule as cron and timezone. For immediate jobs, only type is needed."
    ),
)
def task_create(
    user_id: str,
    action: ActionName,
    type: JobTypeName,
    time: Optional[str] = None,
    schedule: Optional[str] = None,
    timezone: Optional[str] = None,
    action_params: Optional[dict] = None,
) -> dict:
    action = _normalize_action(action)
    return dispatch(
        "task.create@v1",
        {
            "user_id": user_id,
            "action": action,
            "job_params": {
                "type": type,
                "time": time,
                "schedule": schedule,
                "timezone": timezone,
            },
            "action_params": action_params,
        },
    )


@mcp.tool(name="task_list_v1", description="List jobs for a user.")
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


@mcp.tool(name="task_get_v1", description="View one job and its recent runs.")
def task_get(user_id: str, job_id: str) -> dict:
    return dispatch("task.get@v1", {"user_id": user_id, "job_id": job_id})


@mcp.tool(
    name="task_modify_v1",
    description=(
        "Modify an existing job. Use type/time/schedule/timezone fields directly; "
        "do not wrap them in job_params."
    ),
)
def task_modify(
    user_id: str,
    job_id: str,
    action: Optional[ActionName] = None,
    status: Optional[str] = None,
    type: Optional[JobTypeName] = None,
    time: Optional[str] = None,
    schedule: Optional[str] = None,
    timezone: Optional[str] = None,
    action_params: Optional[dict] = None,
) -> dict:
    if action is not None:
        action = _normalize_action(action)
    job_params = {
        key: value
        for key, value in {
            "type": type,
            "time": time,
            "schedule": schedule,
            "timezone": timezone,
        }.items()
        if value is not None
    }
    return dispatch(
        "task.modify@v1",
        {
            "user_id": user_id,
            "job_id": job_id,
            "action": action,
            "status": status,
            "job_params": job_params or None,
            "action_params": action_params,
        },
    )


@mcp.tool(name="task_delete_v1", description="Soft-delete an existing job.")
def task_delete(user_id: str, job_id: str) -> dict:
    return dispatch("task.delete@v1", {"user_id": user_id, "job_id": job_id})


@mcp.tool(
    name="task_trace_get_v1",
    description=(
        "Get the scheduler's execution trace for a fired run, including ordered "
        "events. Use after task_get_v1 surfaces a run's trace_id to explain what "
        "actually happened. Summarize the returned trace; do not invent steps."
    ),
)
def task_trace_get(user_id: str, trace_id: str) -> dict:
    return dispatch(
        "task.trace.get@v1", {"user_id": user_id, "trace_id": trace_id}
    )


def _harden_public_tool_schemas() -> None:
    """Add spec03 schema hints that FastMCP cannot infer from loose strings.

    The handler still performs service-level validation and returns structured
    error envelopes. These schema hints guide Claude toward the right shape
    before it calls the tool.
    """
    for tool in mcp._tool_manager._tools.values():
        tool.parameters["additionalProperties"] = False

    def add_enum(prop: dict, values: list[str]) -> None:
        if "anyOf" in prop:
            for option in prop["anyOf"]:
                if option.get("type") == "string":
                    option["enum"] = values
                    return
        prop["enum"] = values

    create_props = mcp._tool_manager._tools["task_create_v1"].parameters["properties"]
    add_enum(create_props["action"], PUBLIC_ACTIONS)
    add_enum(create_props["type"], PUBLIC_JOB_TYPES)

    modify_props = mcp._tool_manager._tools["task_modify_v1"].parameters["properties"]
    add_enum(modify_props["action"], PUBLIC_ACTIONS)
    add_enum(modify_props["type"], PUBLIC_JOB_TYPES)
    add_enum(modify_props["status"], PUBLIC_EDITABLE_STATUSES)


_harden_public_tool_schemas()


def _startup() -> None:
    Base.metadata.create_all(bind=engine)
    start_scheduler()
    print("task-scheduler MCP server starting", file=sys.stderr)


if __name__ == "__main__":
    _startup()
    mcp.run()
