"""Watcher bucket filtering and worker run processing (spec 01, section 12)."""

from __future__ import annotations

from datetime import datetime, timedelta

from app.core.timeutils import bucket_hour, bucket_shard, time_bucket
from app.jobs.models import (
    JOB_COMPLETED,
    JOB_SCHEDULED,
    RUN_PENDING,
    RUN_QUEUED,
    RUN_SUCCEEDED,
    Job,
    JobRun,
)
from app.jobs.schemas import CreateJobRequest
from app.jobs.service import create_job
from app.scheduler import find_due_runs, hot_buckets, process_run

USER = "user_123"


def _make_run(db, scheduled_at, status=RUN_PENDING):
    run_id = f"run_{scheduled_at.isoformat()}_{status}"
    job = Job(
        job_id=f"job_{scheduled_at.isoformat()}",
        user_id=USER,
        action="review_pr",
        type="one_time",
        time=scheduled_at,
        timezone="UTC",
        status=JOB_SCHEDULED,
    )
    db.add(job)
    run = JobRun(
        run_id=run_id,
        job_id=job.job_id,
        user_id=USER,
        scheduled_at=scheduled_at,
        scheduled_bucket_hour=bucket_hour(scheduled_at),
        scheduled_bucket_shard=bucket_shard(run_id),
        scheduled_bucket=time_bucket(scheduled_at, run_id),
        status=status,
    )
    db.add(run)
    db.commit()
    return run


def test_hot_buckets_includes_lookback():
    now = datetime(2026, 6, 10, 8, 5)
    buckets = hot_buckets(now)
    assert "2026061008" in buckets
    assert "2026061007" in buckets


def test_find_due_runs_returns_due_pending_in_bucket(db):
    now = datetime(2026, 6, 10, 8, 30)
    due = _make_run(db, now - timedelta(minutes=10))
    found = find_due_runs(now, db)
    assert [r.run_id for r in found] == [due.run_id]


def test_find_due_runs_ignores_future_and_old_buckets(db):
    now = datetime(2026, 6, 10, 8, 30)
    _make_run(db, now + timedelta(minutes=20))  # future, same bucket
    _make_run(db, datetime(2026, 6, 10, 5, 0))  # due but outside lookback
    _make_run(db, now - timedelta(minutes=5), status=RUN_QUEUED)  # not pending
    assert find_due_runs(now, db) == []


def test_process_run_completes_one_time_job(db):
    created = create_job(
        db,
        CreateJobRequest(
            user_id=USER, action="review_pr", job_params={"type": "immediate"}
        ),
    )
    run_id = created["next_run"]["run_id"]

    run = process_run(db, run_id)
    assert run.status == RUN_SUCCEEDED
    job = db.get(Job, created["job"]["job_id"])
    assert job.status == JOB_COMPLETED


def test_process_run_recurring_creates_next_run(db):
    created = create_job(
        db,
        CreateJobRequest(
            user_id=USER,
            action="review_pr",
            job_params={"type": "recurring", "schedule": "* * * * *", "timezone": "UTC"},
        ),
    )
    job_id = created["job"]["job_id"]
    process_run(db, created["next_run"]["run_id"])

    job = db.get(Job, job_id)
    assert job.status == JOB_SCHEDULED
    pending = [r for r in job.runs if r.status == RUN_PENDING]
    assert len(pending) == 1
