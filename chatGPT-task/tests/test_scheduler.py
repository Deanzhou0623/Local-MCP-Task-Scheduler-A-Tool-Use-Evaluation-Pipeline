"""Tests for time bucketing and due-job discovery."""

from __future__ import annotations

from datetime import datetime, timedelta

from app.models import STATUS_PENDING, STATUS_QUEUED, Job
from app.scheduler import find_due_jobs, get_time_bucket


def test_get_time_bucket():
    assert get_time_bucket(datetime(2026, 6, 8, 11, 20)) == "2026060811"
    assert get_time_bucket(datetime(2026, 1, 1, 0, 0)) == "2026010100"


def _make_job(db, scheduled_at, status=STATUS_PENDING):
    job = Job(
        description="task",
        scheduled_at=scheduled_at,
        time_bucket=get_time_bucket(scheduled_at),
        status=status,
    )
    db.add(job)
    db.commit()
    db.refresh(job)
    return job


def test_find_due_jobs_returns_due_pending_in_bucket(db):
    now = datetime(2026, 6, 8, 11, 30)
    due = _make_job(db, now - timedelta(minutes=10))

    found = find_due_jobs(now, db)
    assert [j.id for j in found] == [due.id]


def test_find_due_jobs_ignores_future_and_other_buckets(db):
    now = datetime(2026, 6, 8, 11, 30)
    # Future job, same bucket but not yet due.
    _make_job(db, now + timedelta(minutes=20))
    # Due time but a different (earlier) hour bucket.
    _make_job(db, datetime(2026, 6, 8, 10, 5))
    # Already queued -> not pending.
    _make_job(db, now - timedelta(minutes=5), status=STATUS_QUEUED)

    found = find_due_jobs(now, db)
    assert found == []
