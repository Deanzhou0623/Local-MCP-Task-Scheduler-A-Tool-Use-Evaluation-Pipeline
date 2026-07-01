"""Worker: execute queued runs and advance parent jobs.

``process_run`` is the testable core — it runs one run synchronously, with no
threads — and ``worker_loop`` is the thin loop that drains the queue and calls
it. A successful recurring run whose job is still active schedules the next run.

Spec 04: each run gets exactly one ``ActionTrace``. The trace is created and the
executor's events are written *before* the final run/job status update, so an
executor crash still leaves a persisted ``failed`` trace. The executor's
``succeeded``/``skipped``/``failed`` result maps onto the run status — ``skipped``
counts as success for the run, the distinction living on the trace.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timedelta
from queue import Empty

from sqlalchemy.orm import Session
from sqlalchemy import select

from app.core.database import SessionLocal
from app.core.errors import AppError
from app.core.timeutils import next_recurring_run_utc, utcnow_naive
from app.jobs.executors import (
    ACTION_FAILED_PERMANENT,
    ACTION_FAILED_RETRYABLE,
    ACTION_SKIPPED,
    ACTION_SUCCEEDED,
    EXECUTORS,
    ActionContext,
)
from app.jobs.models import (
    JOB_COMPLETED,
    JOB_DELETED,
    JOB_FAILED,
    JOB_PAUSED,
    JOB_RUNNING,
    JOB_SCHEDULED,
    QUEUE_LEASED,
    RUN_FAILED,
    RUN_PENDING,
    RUN_QUEUED,
    RUN_RUNNING,
    RUN_SUCCEEDED,
    TRACE_FAILED,
    TRACE_RUNNING,
    TRACE_SKIPPED,
    TRACE_SUCCEEDED,
    TYPE_RECURRING,
    Job,
    JobRun,
    JobRunQueue,
)
from app.jobs.runs import new_run_for_job, retry_run_for_job
from app.jobs.service import job_action_params
from app.jobs.traces import create_trace_for_run, finish_trace
from app.scheduler.queue import (
    claim_next_queue_item,
    complete_queue_item,
    fail_queue_item,
    run_queue,
)

# ActionResult.status -> trace status (spec 04, section 5 mapping table).
_RESULT_TO_TRACE = {
    ACTION_SUCCEEDED: TRACE_SUCCEEDED,
    ACTION_SKIPPED: TRACE_SKIPPED,
    "failed": TRACE_FAILED,
    ACTION_FAILED_RETRYABLE: TRACE_FAILED,
    ACTION_FAILED_PERMANENT: TRACE_FAILED,
}

MAX_RETRIES = 3
RETRY_BACKOFFS = (30, 120, 600)
WORKER_LEASE_SECONDS = 60
WORKER_POLL_INTERVAL_SECONDS = 1
# Stale-running recovery must wait longer than a lease so it never races a live
# worker; a run is only recovered once its lease has genuinely expired.
STALE_RUNNING_SECONDS = WORKER_LEASE_SECONDS * 2


def process_run(db: Session, run_id: str) -> JobRun | None:
    """Execute a single run and advance its parent job. Returns the run."""
    run, _result_status = _process_run_result(db, run_id)
    return run


def _process_run_result(db: Session, run_id: str) -> tuple[JobRun | None, str | None]:
    """Execute a run and return both the row and executor result status."""
    run = db.get(JobRun, run_id)
    if run is None:
        return None, None
    job = db.get(Job, run.job_id)
    if job is None or run.status not in (RUN_QUEUED, RUN_PENDING):
        return run, None

    run.status = RUN_RUNNING
    run.started_at = utcnow_naive()
    job.status = JOB_RUNNING
    db.commit()

    result_status = _execute_with_trace(db, run, job)
    run.status = (
        RUN_SUCCEEDED
        if result_status in (ACTION_SUCCEEDED, ACTION_SKIPPED)
        else RUN_FAILED
    )
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
    return run, result_status


def _execute_with_trace(db: Session, run: JobRun, job: Job) -> str:
    """Create the trace, run the executor, finalize the trace. Returns the
    ``ActionResult`` status (``succeeded`` / ``skipped`` / ``failed``).

    The trace is always persisted — including on executor failure — before the
    caller updates the final run/job status.
    """
    trace = create_trace_for_run(db, run, job)
    trace.status = TRACE_RUNNING
    trace.started_at = utcnow_naive()
    db.commit()

    executor = EXECUTORS.get(job.action)
    if executor is None:
        # Defensive: create/modify reject unknown actions, so this is unexpected.
        message = f"No executor registered for action '{job.action}'."
        finish_trace(db, trace, TRACE_FAILED, error=message)
        run.error_message = message
        db.commit()
        return ACTION_FAILED_PERMANENT

    ctx = ActionContext(
        trace_id=trace.trace_id,
        run_id=run.run_id,
        job_id=job.job_id,
        user_id=job.user_id,
        action=job.action,
        params=job_action_params(job),
        db=db,
    )

    try:
        result = executor.execute(ctx)
    except AppError as exc:
        finish_trace(db, trace, TRACE_FAILED, error=exc.message)
        run.error_message = exc.message
        db.commit()
        return ACTION_FAILED_PERMANENT
    except Exception as exc:  # noqa: BLE001 - record any failure as a trace
        finish_trace(db, trace, TRACE_FAILED, error=str(exc))
        run.error_message = str(exc)
        db.commit()
        return ACTION_FAILED_RETRYABLE

    finish_trace(
        db,
        trace,
        _RESULT_TO_TRACE[result.status],
        summary=result.summary,
        artifact=result.artifact,
        error=result.summary if result.status.startswith("failed") else None,
    )
    if result.status.startswith("failed"):
        run.error_message = result.summary
    db.commit()
    return result.status


def _create_next_run(db: Session, job: Job, scheduled_at_utc: datetime) -> None:
    db.add(new_run_for_job(job, scheduled_at_utc))


def _retry_backoff_seconds(retry_count: int) -> int:
    idx = min(max(retry_count, 0), len(RETRY_BACKOFFS) - 1)
    return RETRY_BACKOFFS[idx]


def create_retry_run(db: Session, run: JobRun, *, now: datetime | None = None) -> JobRun:
    """Create the next retry attempt for a failed run.

    Built via the spec 06 ``retry_run_for_job`` helper so the retry inherits the
    attempt group, a fresh sharded bucket, and attempt metadata.
    """
    now = now or utcnow_naive()
    scheduled_at = now + timedelta(seconds=_retry_backoff_seconds(run.retry_count or 0))
    job = db.get(Job, run.job_id)
    retry = retry_run_for_job(job, run, scheduled_at)
    db.add(retry)
    if job is not None and job.status not in (JOB_DELETED, JOB_PAUSED):
        job.status = JOB_SCHEDULED
    db.flush()
    return retry


def _has_live_lease(db: Session, run_id: str, now: datetime) -> bool:
    """True if a queue item still holds an unexpired lease for this run."""
    return (
        db.execute(
            select(JobRunQueue.queue_id).where(
                JobRunQueue.run_id == run_id,
                JobRunQueue.status == QUEUE_LEASED,
                JobRunQueue.locked_until > now,
            )
        ).scalar_one_or_none()
        is not None
    )


def recover_stale_running_runs(
    db: Session,
    *,
    now: datetime | None = None,
    stale_after_seconds: int = STALE_RUNNING_SECONDS,
    max_retries: int = MAX_RETRIES,
) -> int:
    """Fail stale running attempts and create retry attempts when allowed.

    A run is only recovered once it has been running past ``stale_after_seconds``
    *and* no queue item still holds a live lease — so recovery never races a
    worker that is genuinely still executing the run.
    """
    now = now or utcnow_naive()
    cutoff = now - timedelta(seconds=stale_after_seconds)
    runs = db.execute(
        select(JobRun).where(
            JobRun.status == RUN_RUNNING,
            JobRun.started_at <= cutoff,
        )
    ).scalars().all()
    recovered = 0
    for run in runs:
        if _has_live_lease(db, run.run_id, now):
            # A worker still holds a valid lease; not actually stale.
            continue
        message = "Run exceeded worker lease and was recovered."
        run.status = RUN_FAILED
        run.finished_at = now
        run.error_message = message
        job = db.get(Job, run.job_id)
        if job is not None:
            job.status = JOB_FAILED
        for item in db.execute(
            select(JobRunQueue).where(
                JobRunQueue.run_id == run.run_id,
                JobRunQueue.status.in_(("ready", "leased")),
            )
        ).scalars().all():
            fail_queue_item(db, item, message)
        if (run.retry_count or 0) < max_retries:
            create_retry_run(db, run, now=now)
        recovered += 1
    db.flush()
    return recovered


def process_queue_item(
    db: Session, item: JobRunQueue, *, max_retries: int = MAX_RETRIES
) -> JobRun | None:
    """Execute a leased queue item and finalize queue/retry state."""
    run, result_status = _process_run_result(db, item.run_id)
    if run is None:
        fail_queue_item(db, item, "Run not found.")
        db.commit()
        return None

    if run.status == RUN_SUCCEEDED:
        complete_queue_item(db, item)
    else:
        fail_queue_item(db, item, run.error_message)
        if (
            result_status == ACTION_FAILED_RETRYABLE
            and (run.retry_count or 0) < max_retries
        ):
            create_retry_run(db, run)
    db.commit()
    return run


def claim_and_process_once(
    db: Session, *, worker_id: str, lease_seconds: int
) -> JobRun | None:
    """Claim at most one ready queue item and execute it. Returns the run."""
    item = claim_next_queue_item(
        db, worker_id=worker_id, lease_seconds=lease_seconds
    )
    if item is None:
        db.commit()
        return None
    db.commit()
    return process_queue_item(db, item)


def _drain_one_signal() -> None:
    """Consume one in-memory wakeup signal without blocking.

    ``run_queue`` is only a best-effort latency hint; the durable table is the
    source of truth. Draining one signal per processed item keeps the in-memory
    queue from growing unbounded under load.
    """
    try:
        run_queue.get_nowait()
    except Empty:
        pass


def worker_loop(
    *,
    worker_id: str | None = None,
    lease_seconds: int = WORKER_LEASE_SECONDS,
    poll_interval: int = WORKER_POLL_INTERVAL_SECONDS,
) -> None:
    """Claim and execute durable queue rows, waking promptly on new work.

    When idle, block on the wakeup signal (up to ``poll_interval``) so a freshly
    enqueued run runs without waiting a full poll. When busy, loop immediately
    and drain one signal per processed item so ``run_queue`` cannot grow.
    """
    worker_id = worker_id or f"worker_{uuid.uuid4().hex[:12]}"
    while True:
        db = SessionLocal()
        try:
            processed = claim_and_process_once(
                db, worker_id=worker_id, lease_seconds=lease_seconds
            )
        finally:
            db.close()
        if processed is None:
            # Idle: wait for a wakeup signal or time out. Either way the signal
            # is consumed, so the queue never accumulates.
            try:
                run_queue.get(timeout=poll_interval)
            except Empty:
                pass
        else:
            _drain_one_signal()
