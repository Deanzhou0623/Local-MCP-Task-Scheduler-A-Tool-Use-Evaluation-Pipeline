"""Worker: execute a queued run and advance its parent job (spec 01 §12, spec 04 §5).

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

from datetime import datetime

from sqlalchemy.orm import Session

from app.core.database import SessionLocal
from app.core.errors import AppError
from app.core.ids import new_run_id
from app.core.timeutils import next_recurring_run_utc, time_bucket, utcnow_naive
from app.jobs.executors import EXECUTORS, ActionContext
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
    TRACE_FAILED,
    TRACE_RUNNING,
    TRACE_SKIPPED,
    TRACE_SUCCEEDED,
    TYPE_RECURRING,
    Job,
    JobRun,
)
from app.jobs.service import job_action_params
from app.jobs.traces import create_trace_for_run, finish_trace
from app.scheduler.queue import run_queue

# ActionResult.status -> trace status (spec 04, section 5 mapping table).
_RESULT_TO_TRACE = {
    "succeeded": TRACE_SUCCEEDED,
    "skipped": TRACE_SKIPPED,
    "failed": TRACE_FAILED,
}


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

    result_status = _execute_with_trace(db, run, job)
    run.status = RUN_SUCCEEDED if result_status in ("succeeded", "skipped") else RUN_FAILED
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
        return "failed"

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
        return "failed"
    except Exception as exc:  # noqa: BLE001 - record any failure as a trace
        finish_trace(db, trace, TRACE_FAILED, error=str(exc))
        run.error_message = str(exc)
        db.commit()
        return "failed"

    finish_trace(
        db,
        trace,
        _RESULT_TO_TRACE[result.status],
        summary=result.summary,
        artifact=result.artifact,
        error=result.summary if result.status == "failed" else None,
    )
    if result.status == "failed":
        run.error_message = result.summary
    db.commit()
    return result.status


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
