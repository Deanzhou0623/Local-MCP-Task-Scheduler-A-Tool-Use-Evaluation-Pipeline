"""Helpers for constructing JobRun rows with Spec 06 scheduling metadata."""

from __future__ import annotations

from datetime import datetime

from app.core.ids import new_attempt_group_id, new_run_id
from app.core.timeutils import bucket_hour, bucket_shard, scheduled_bucket
from app.jobs.models import (
    DEFAULT_PRIORITY,
    RUN_PENDING,
    TRIGGER_IMMEDIATE,
    TRIGGER_RETRY,
    TRIGGER_SCHEDULED,
    TYPE_IMMEDIATE,
    Job,
    JobRun,
)


def new_run_for_job(
    job: Job,
    scheduled_at_utc: datetime,
    *,
    trigger_reason: str | None = None,
    attempt_group_id: str | None = None,
    attempt_number: int = 1,
    parent_run_id: str | None = None,
    retry_count: int = 0,
    priority: int | None = None,
) -> JobRun:
    """Build a pending run with sharded bucket and attempt metadata."""
    run_id = new_run_id()
    hour = bucket_hour(scheduled_at_utc)
    shard = bucket_shard(run_id)
    if trigger_reason is None:
        trigger_reason = TRIGGER_IMMEDIATE if job.type == TYPE_IMMEDIATE else TRIGGER_SCHEDULED
    return JobRun(
        run_id=run_id,
        job_id=job.job_id,
        user_id=job.user_id,
        scheduled_at=scheduled_at_utc,
        scheduled_bucket_hour=hour,
        scheduled_bucket_shard=shard,
        scheduled_bucket=scheduled_bucket(scheduled_at_utc, run_id),
        status=RUN_PENDING,
        retry_count=retry_count,
        attempt_group_id=attempt_group_id or new_attempt_group_id(),
        attempt_number=attempt_number,
        parent_run_id=parent_run_id,
        trigger_reason=trigger_reason,
        priority=priority if priority is not None else DEFAULT_PRIORITY,
    )


def retry_run_for_job(
    job: Job,
    previous: JobRun,
    scheduled_at_utc: datetime,
) -> JobRun:
    """Build a retry attempt for a previous run."""
    return new_run_for_job(
        job,
        scheduled_at_utc,
        trigger_reason=TRIGGER_RETRY,
        attempt_group_id=previous.attempt_group_id,
        attempt_number=(previous.attempt_number or 1) + 1,
        parent_run_id=previous.run_id,
        retry_count=(previous.retry_count or 0) + 1,
        priority=previous.priority,
    )
