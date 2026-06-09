"""Worker: execute a queued run and advance its parent job (spec 01, §12).

``process_run`` is the testable core — it runs one run synchronously, with no
threads — and ``worker_loop`` is the thin loop that drains the queue and calls
it. A successful recurring run whose job is still active schedules the next run.
"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy.orm import Session

from app.core.database import SessionLocal
from app.core.ids import new_run_id
from app.core.timeutils import next_recurring_run_utc, time_bucket, utcnow_naive
from app.jobs.actions import execute_action
from app.jobs.models import (
    JOB_COMPLETED,
    JOB_DELETED,
    JOB_FAILED,
    JOB_PAUSED,
    JOB_RUNNING,
    JOB_SCHEDULED,
    RUN_FAILED,
    RUN_PENDING,
    RUN_QUEUED,
    RUN_RUNNING,
    RUN_SUCCEEDED,
    TYPE_RECURRING,
    Job,
    JobRun,
)
from app.scheduler.queue import run_queue


def process_run(db: Session, run_id: str) -> JobRun | None:
    """Execute a single run and advance its parent job. Returns the run."""
    run = db.get(JobRun, run_id)
    if run is None:
        return None
    job = db.get(Job, run.job_id)
    if job is None or run.status not in (RUN_QUEUED, RUN_PENDING):
        return run

    run.status = RUN_RUNNING
    run.started_at = utcnow_naive()
    job.status = JOB_RUNNING
    db.commit()

    try:
        execute_action(job.action, job_id=job.job_id)
        run.status = RUN_SUCCEEDED
    except Exception as exc:  # noqa: BLE001 - record any failure
        run.status = RUN_FAILED
        run.error_message = str(exc)
    run.finished_at = utcnow_naive()

    if run.status == RUN_SUCCEEDED:
        if job.type == TYPE_RECURRING:
            # Only continue an active recurring job.
            if job.status not in (JOB_PAUSED, JOB_DELETED, JOB_FAILED):
                nxt = next_recurring_run_utc(
                    job.schedule, job.timezone, utcnow_naive()
                )
                _create_next_run(db, job, nxt)
                job.status = JOB_SCHEDULED
        else:
            job.status = JOB_COMPLETED
    else:
        job.status = JOB_FAILED

    db.commit()
    return run


def _create_next_run(db: Session, job: Job, scheduled_at_utc: datetime) -> None:
    db.add(
        JobRun(
            run_id=new_run_id(),
            job_id=job.job_id,
            user_id=job.user_id,
            scheduled_at=scheduled_at_utc,
            scheduled_bucket=time_bucket(scheduled_at_utc),
            status=RUN_PENDING,
        )
    )


def worker_loop() -> None:
    """Drain the queue, executing each run."""
    while True:
        run_id = run_queue.get()
        db = SessionLocal()
        try:
            process_run(db, run_id)
        finally:
            db.close()
            run_queue.task_done()
