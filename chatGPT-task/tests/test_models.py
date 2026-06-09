"""Tests for the Job model."""

from __future__ import annotations

from datetime import datetime

from app.models import STATUS_PENDING, Job
from app.scheduler import get_time_bucket


def test_job_defaults_to_pending(db):
    scheduled_at = datetime(2026, 6, 8, 11, 20)
    job = Job(
        description="review PR #123",
        scheduled_at=scheduled_at,
        time_bucket=get_time_bucket(scheduled_at),
    )
    db.add(job)
    db.commit()
    db.refresh(job)

    assert job.id is not None
    assert job.status == STATUS_PENDING
    assert job.time_bucket == "2026060811"
    assert job.created_at is not None
    assert job.updated_at is not None
