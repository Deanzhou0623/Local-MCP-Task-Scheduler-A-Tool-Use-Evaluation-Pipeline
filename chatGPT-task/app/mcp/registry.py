"""MCP tool layer: a data-driven registry over the service functions.

The registry (spec 01, sections 5 & 13) maps stable, versioned tool names to
handlers. Each handler:

1. validates raw ``args`` into a Pydantic model (Pydantic errors → a
   ``VALIDATION_ERROR`` envelope),
2. opens a session and calls the matching service function,
3. converts any :class:`AppError` into the structured error envelope.

A dictionary keeps routing introspectable and testable: a test can assert that
every declared tool has a handler, and adding the next tool is a one-line entry
rather than another branch in an ``if/elif`` chain.
"""

from __future__ import annotations

from typing import Callable

from pydantic import BaseModel, ValidationError

from app.core.database import SessionLocal
from app.core.errors import (
    INTERNAL_ERROR,
    VALIDATION_ERROR,
    AppError,
    error_envelope,
)
from app.jobs.schemas import (
    CreateJobRequest,
    DeleteJobToolArgs,
    GetJobToolArgs,
    ListJobsParams,
    ModifyJobRequest,
    ModifyJobToolArgs,
)
from app.jobs.service import (
    create_job,
    delete_job,
    get_job,
    list_jobs,
    modify_job,
)


def _validation_envelope(exc: ValidationError) -> dict:
    """Turn the first Pydantic error into the structured contract."""
    first = exc.errors()[0]
    loc = [str(p) for p in first.get("loc", ()) if p not in ("body",)]
    field = ".".join(loc) or None
    return error_envelope(VALIDATION_ERROR, first.get("msg", "Invalid input."), field=field)


def _parse(model: type[BaseModel], args: dict | None):
    """Validate ``args`` into ``model`` or return a validation envelope dict."""
    try:
        return model(**(args or {}))
    except ValidationError as exc:
        return _validation_envelope(exc)


def _execute(service_call: Callable) -> dict:
    """Run a service call inside a session and normalize all failures."""
    db = SessionLocal()
    try:
        return service_call(db)
    except AppError as exc:
        db.rollback()
        return exc.to_envelope()
    except Exception as exc:  # noqa: BLE001 - never leak a raw traceback
        db.rollback()
        return error_envelope(INTERNAL_ERROR, str(exc))
    finally:
        db.close()


# ---------------------------------------------------------------------------
# Tool handlers — each accepts the raw MCP ``args`` dict.
# ---------------------------------------------------------------------------
def create_task(args: dict) -> dict:
    req = _parse(CreateJobRequest, args)
    if isinstance(req, dict):
        return req
    return _execute(lambda db: create_job(db, req))


def list_tasks(args: dict) -> dict:
    params = _parse(ListJobsParams, args)
    if isinstance(params, dict):
        return params
    return _execute(lambda db: list_jobs(db, params))


def get_task(args: dict) -> dict:
    parsed = _parse(GetJobToolArgs, args)
    if isinstance(parsed, dict):
        return parsed
    return _execute(
        lambda db: get_job(db, job_id=parsed.job_id, user_id=parsed.user_id)
    )


def modify_task(args: dict) -> dict:
    parsed = _parse(ModifyJobToolArgs, args)
    if isinstance(parsed, dict):
        return parsed
    body = ModifyJobRequest(
        user_id=parsed.user_id,
        action=parsed.action,
        status=parsed.status,
        job_params=parsed.job_params,
    )
    return _execute(lambda db: modify_job(db, job_id=parsed.job_id, req=body))


def delete_task(args: dict) -> dict:
    parsed = _parse(DeleteJobToolArgs, args)
    if isinstance(parsed, dict):
        return parsed
    return _execute(
        lambda db: delete_job(db, job_id=parsed.job_id, user_id=parsed.user_id)
    )


# ---------------------------------------------------------------------------
# Registry + dispatch
# ---------------------------------------------------------------------------
TOOL_REGISTRY: dict[str, Callable[[dict], dict]] = {
    "task.create@v1": create_task,
    "task.list@v1": list_tasks,
    "task.get@v1": get_task,
    "task.modify@v1": modify_task,
    "task.delete@v1": delete_task,
}


def dispatch(tool_name: str, args: dict | None = None) -> dict:
    """Route a tool call through the registry."""
    handler = TOOL_REGISTRY.get(tool_name)
    if handler is None:
        return error_envelope(
            VALIDATION_ERROR,
            f"Unknown tool '{tool_name}'.",
            field="tool",
        )
    return handler(args or {})
