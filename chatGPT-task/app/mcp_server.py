"""Real MCP server for the task scheduler.

Run with::

    python -m app.mcp_server

MCP stdio uses stdout for protocol messages, so this module must never call
``print()``. Diagnostic logging goes to stderr.
"""

from __future__ import annotations

import sys
from datetime import datetime

from dotenv import load_dotenv
from mcp.server.fastmcp import FastMCP
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.database import Base, SessionLocal, engine
from app.models import TERMINAL_STATUSES, Job
from app.scheduler import get_time_bucket, start_scheduler

load_dotenv()

# Format datetimes consistently in tool responses.
_DT_FORMAT = "%Y-%m-%d %H:%M:%S"


def _format_dt(value: datetime) -> str:
    return value.strftime(_DT_FORMAT)


# ---------------------------------------------------------------------------
# Tool handlers — plain functions that accept a SQLAlchemy Session.
# These contain all business logic and are testable without the MCP server.
# ---------------------------------------------------------------------------


def handle_create_task(db: Session, *, description: str, scheduled_at: str) -> dict:
    """Create a pending job."""
    if not description or not description.strip():
        return {"error": "description is required and must be non-empty"}

    if not scheduled_at:
        return {"error": "scheduled_at is required"}

    try:
        parsed = datetime.fromisoformat(scheduled_at)
    except ValueError:
        return {"error": f"scheduled_at is not ISO 8601 parseable: {scheduled_at!r}"}

    # Store naive datetimes; drop tzinfo if provided.
    if parsed.tzinfo is not None:
        parsed = parsed.replace(tzinfo=None)

    job = Job(
        description=description,
        scheduled_at=parsed,
        time_bucket=get_time_bucket(parsed),
    )
    db.add(job)
    db.commit()
    db.refresh(job)

    return {
        "job_id": job.id,
        "status": job.status,
        "scheduled_at": _format_dt(job.scheduled_at),
    }


def handle_get_status(db: Session, *, job_id: int) -> dict:
    """Return the status of a single job."""
    job = db.get(Job, job_id)
    if job is None:
        return {"error": f"Job {job_id} not found"}

    return {
        "job_id": job.id,
        "description": job.description,
        "status": job.status,
        "scheduled_at": _format_dt(job.scheduled_at),
        "result": job.result,
    }


def handle_list_tasks(db: Session) -> dict:
    """Return all jobs ordered by scheduled_at descending."""
    stmt = select(Job).order_by(Job.scheduled_at.desc())
    jobs = db.execute(stmt).scalars().all()
    return {
        "jobs": [
            {
                "job_id": job.id,
                "description": job.description,
                "status": job.status,
                "scheduled_at": _format_dt(job.scheduled_at),
            }
            for job in jobs
        ]
    }


def handle_cancel_task(db: Session, *, job_id: int) -> dict:
    """Cancel a job that has not completed."""
    job = db.get(Job, job_id)
    if job is None:
        return {"error": f"Job {job_id} not found"}

    if job.status in TERMINAL_STATUSES:
        return {"error": f"Job {job_id} is {job.status} and cannot be cancelled"}

    job.status = "cancelled"
    db.commit()

    return {"job_id": job.id, "status": job.status}


# ---------------------------------------------------------------------------
# Session wrapper + MCP tool registration
# ---------------------------------------------------------------------------


def _with_session(handler, **kwargs) -> dict:
    """Open a session, run the handler, and always close the session."""
    db = SessionLocal()
    try:
        return handler(db, **kwargs)
    finally:
        db.close()


mcp = FastMCP("task-scheduler")


@mcp.tool(description="Schedule a new task for future execution.")
def task_create(description: str, scheduled_at: str) -> dict:
    return _with_session(
        handle_create_task, description=description, scheduled_at=scheduled_at
    )


@mcp.tool(description="List all scheduled tasks.")
def task_list() -> dict:
    return _with_session(handle_list_tasks)


@mcp.tool(description="Get the status of a scheduled task by job_id.")
def task_status(job_id: int) -> dict:
    return _with_session(handle_get_status, job_id=job_id)


@mcp.tool(description="Cancel a scheduled task that has not completed yet.")
def task_cancel(job_id: int) -> dict:
    return _with_session(handle_cancel_task, job_id=job_id)


def _startup() -> None:
    """Create tables and start the background scheduler threads."""
    Base.metadata.create_all(bind=engine)
    start_scheduler()
    print("task-scheduler MCP server starting", file=sys.stderr)


if __name__ == "__main__":
    _startup()
    mcp.run()
