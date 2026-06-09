"""Tests for the MCP tool handler logic (no MCP server required)."""

from __future__ import annotations

from app.mcp_server import (
    handle_cancel_task,
    handle_create_task,
    handle_get_status,
    handle_list_tasks,
)
from app.models import STATUS_COMPLETED, Job


def test_handle_create_task(db):
    result = handle_create_task(
        db, description="review PR #123", scheduled_at="2026-06-08T11:20:00"
    )
    assert result["job_id"] == 1
    assert result["status"] == "pending"
    assert result["scheduled_at"] == "2026-06-08 11:20:00"


def test_handle_create_task_requires_description(db):
    result = handle_create_task(db, description="  ", scheduled_at="2026-06-08T11:20:00")
    assert "error" in result


def test_handle_create_task_rejects_bad_datetime(db):
    result = handle_create_task(db, description="x", scheduled_at="not-a-date")
    assert "error" in result


def test_handle_get_status(db):
    created = handle_create_task(
        db, description="review PR #123", scheduled_at="2026-06-08T11:20:00"
    )
    status = handle_get_status(db, job_id=created["job_id"])
    assert status["job_id"] == created["job_id"]
    assert status["description"] == "review PR #123"
    assert status["status"] == "pending"
    assert status["result"] is None


def test_handle_get_status_missing_job_returns_error(db):
    result = handle_get_status(db, job_id=999)
    assert result == {"error": "Job 999 not found"}


def test_handle_list_tasks_orders_by_scheduled_at_desc(db):
    handle_create_task(db, description="earlier", scheduled_at="2026-06-08T09:00:00")
    handle_create_task(db, description="later", scheduled_at="2026-06-08T18:00:00")

    listing = handle_list_tasks(db)
    descriptions = [job["description"] for job in listing["jobs"]]
    assert descriptions == ["later", "earlier"]


def test_handle_cancel_task(db):
    created = handle_create_task(
        db, description="cancel me", scheduled_at="2026-06-08T11:20:00"
    )
    result = handle_cancel_task(db, job_id=created["job_id"])
    assert result == {"job_id": created["job_id"], "status": "cancelled"}


def test_cannot_cancel_completed_job(db):
    created = handle_create_task(
        db, description="done", scheduled_at="2026-06-08T11:20:00"
    )
    job = db.get(Job, created["job_id"])
    job.status = STATUS_COMPLETED
    db.commit()

    result = handle_cancel_task(db, job_id=created["job_id"])
    assert "error" in result


def test_cancel_missing_job_returns_error(db):
    result = handle_cancel_task(db, job_id=999)
    assert result == {"error": "Job 999 not found"}
