"""Business logic for the four core flows (spec 01, section 12).

These functions are the single source of truth shared by the REST API
(:mod:`app.api`) and the MCP tool registry (:mod:`app.mcp.registry`). They take a
SQLAlchemy ``Session`` plus validated Pydantic input, return the success
envelope as a plain dict, and raise :class:`AppError` for every failure so both
surfaces emit the identical structured error contract.
"""

from __future__ import annotations

import json
from datetime import datetime

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.errors import (
    CONFLICT,
    NOT_FOUND,
    PERMISSION_DENIED,
    UNSUPPORTED_ACTION,
    VALIDATION_ERROR,
    AppError,
)
from app.core.ids import new_attempt_group_id, new_job_id, new_run_id
from app.jobs.actions import SUPPORTED_ACTIONS, is_supported
from app.jobs.executors import validate_action_params
from app.jobs.models import (
    JOB_COMPLETED,
    JOB_DELETED,
    JOB_PAUSED,
    JOB_SCHEDULED,
    RUN_CANCELLABLE,
    RUN_CANCELLED,
    RUN_PENDING,
    DEFAULT_PRIORITY,
    TRIGGER_IMMEDIATE,
    TRIGGER_SCHEDULED,
    TYPE_IMMEDIATE,
    TYPE_ONE_TIME,
    TYPE_RECURRING,
    ActionTrace,
    Job,
    JobRun,
)
from app.jobs.schemas import (
    CreateJobRequest,
    JobParamsCreate,
    ListJobsParams,
    ModifyJobRequest,
)
from app.jobs.traces import (
    latest_trace_for_run,
    trace_summary_dict,
    trace_to_dict,
)
from app.scheduler.queue import cancel_queue_items_for_run
from app.core.timeutils import (
    iso_in_tz,
    iso_utc,
    next_recurring_run_utc,
    one_time_to_utc,
    time_bucket,
    utcnow_naive,
    validate_cron,
    validate_timezone,
)

# How many recent runs job-detail returns.
RECENT_RUNS_LIMIT = 20


# ---------------------------------------------------------------------------
# Serializers
# ---------------------------------------------------------------------------
def job_action_params(job: Job) -> dict:
    """Parse a job's stored ``action_params_json`` text into a dict (spec 04)."""
    if not job.action_params_json:
        return {}
    return json.loads(job.action_params_json)


def serialize_job(job: Job) -> dict:
    """Full job representation (job timestamps in UTC ``Z``; ``time`` in tz)."""
    return {
        "job_id": job.job_id,
        "user_id": job.user_id,
        "action": job.action,
        "type": job.type,
        "time": iso_in_tz(job.time, job.timezone),
        "schedule": job.schedule,
        "timezone": job.timezone,
        "action_params": job_action_params(job),
        "status": job.status,
        "created_at": iso_utc(job.created_at),
        "updated_at": iso_utc(job.updated_at),
    }


def serialize_run(run: JobRun) -> dict:
    """Short run representation used in ``next_run`` and run history."""
    return {
        "run_id": run.run_id,
        "job_id": run.job_id,
        "scheduled_at": iso_in_tz(run.scheduled_at, _run_tz(run)),
        "status": run.status,
    }


def _run_tz(run: JobRun) -> str:
    # Present a run's scheduled_at in its parent job's timezone.
    return run.job.timezone if run.job is not None else "UTC"


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------
def _load_owned_job(db: Session, job_id: str, user_id: str) -> Job:
    """Load a job enforcing the ownership boundary."""
    job = db.get(Job, job_id)
    if job is None:
        raise AppError(NOT_FOUND, "Job not found.", field="job_id")
    if job.user_id != user_id:
        raise AppError(
            PERMISSION_DENIED, "You do not own this job.", field="user_id"
        )
    return job


def _create_run(db: Session, job: Job, scheduled_at_utc: datetime) -> JobRun:
    trigger_reason = TRIGGER_IMMEDIATE if job.type == TYPE_IMMEDIATE else TRIGGER_SCHEDULED
    run = JobRun(
        run_id=new_run_id(),
        job_id=job.job_id,
        user_id=job.user_id,
        scheduled_at=scheduled_at_utc,
        scheduled_bucket=time_bucket(scheduled_at_utc),
        status=RUN_PENDING,
        attempt_group_id=new_attempt_group_id(),
        attempt_number=1,
        trigger_reason=trigger_reason,
        priority=DEFAULT_PRIORITY,
    )
    db.add(run)
    return run


def _cancel_pending_runs(db: Session, job: Job) -> int:
    """Cancel all not-yet-started runs for a job; return how many were cancelled."""
    stmt = select(JobRun).where(
        JobRun.job_id == job.job_id, JobRun.status.in_(RUN_CANCELLABLE)
    )
    runs = db.execute(stmt).scalars().all()
    for run in runs:
        run.status = RUN_CANCELLED
        cancel_queue_items_for_run(db, run.run_id)
    return len(runs)


def _next_pending_run(db: Session, job: Job) -> JobRun | None:
    stmt = (
        select(JobRun)
        .where(JobRun.job_id == job.job_id, JobRun.status == RUN_PENDING)
        .order_by(JobRun.scheduled_at.asc())
        .limit(1)
    )
    return db.execute(stmt).scalars().first()


def _first_run_time_utc(
    job_type: str, schedule: str | None, time_utc: datetime | None, tz: str
) -> datetime:
    """Compute the first/next run instant for a (re)scheduled job."""
    if job_type == TYPE_IMMEDIATE:
        return utcnow_naive()
    if job_type == TYPE_ONE_TIME:
        assert time_utc is not None  # guaranteed by validation
        return time_utc
    return next_recurring_run_utc(schedule, tz, utcnow_naive())


def _validate_type_params(
    job_type: str,
    *,
    time: datetime | None,
    schedule: str | None,
    tz: str | None,
) -> datetime | None:
    """Apply the per-type create/modify rules. Returns the one-time UTC instant.

    ``time`` is the raw (possibly tz-aware) datetime supplied by the caller;
    the returned value is its naive-UTC form for one-time jobs, else ``None``.
    """
    if job_type == TYPE_IMMEDIATE:
        if tz is not None:
            validate_timezone(tz)
        if time is not None or schedule is not None:
            raise AppError(
                VALIDATION_ERROR,
                "Immediate jobs must not include time or schedule.",
                field="job_params",
            )
        return None

    if tz is None:
        raise AppError(
            VALIDATION_ERROR,
            "One-time and recurring jobs require an explicit IANA timezone.",
            field="job_params.timezone",
            expected="IANA timezone",
        )
    validate_timezone(tz)

    if job_type == TYPE_ONE_TIME:
        if time is None:
            raise AppError(
                VALIDATION_ERROR,
                "One-time jobs require a time.",
                field="job_params.time",
                expected="ISO 8601 timestamp",
            )
        if schedule is not None:
            raise AppError(
                VALIDATION_ERROR,
                "One-time jobs must not include a schedule.",
                field="job_params.schedule",
            )
        return one_time_to_utc(time, tz)

    # recurring
    if not schedule:
        raise AppError(
            VALIDATION_ERROR,
            "Recurring jobs require a cron schedule.",
            field="job_params.schedule",
            expected="cron expression",
        )
    if time is not None:
        raise AppError(
            VALIDATION_ERROR,
            "Recurring jobs must not include a time.",
            field="job_params.time",
        )
    validate_cron(schedule)
    return None


# ---------------------------------------------------------------------------
# Flow 1: Create
# ---------------------------------------------------------------------------
def create_job(db: Session, req: CreateJobRequest) -> dict:
    if not is_supported(req.action):
        raise AppError(
            UNSUPPORTED_ACTION,
            f"Action '{req.action}' is not supported.",
            field="action",
            expected=sorted(SUPPORTED_ACTIONS),
        )

    # Required action params are validated up front so the model gets fast,
    # fixable feedback before any job is stored (spec 04, section 6).
    validate_action_params(req.action, req.action_params)

    p: JobParamsCreate = req.job_params
    time_utc = _validate_type_params(
        p.type, time=p.time, schedule=p.schedule, tz=p.timezone
    )
    job_timezone = p.timezone or "UTC"

    job = Job(
        job_id=new_job_id(),
        user_id=req.user_id,
        action=req.action,
        type=p.type,
        time=time_utc,
        schedule=p.schedule,
        timezone=job_timezone,
        action_params_json=(
            json.dumps(req.action_params) if req.action_params else None
        ),
        status=JOB_SCHEDULED,
    )
    db.add(job)
    db.flush()  # assign defaults / make job queryable for the run FK

    first_run = _first_run_time_utc(p.type, p.schedule, time_utc, job_timezone)
    run = _create_run(db, job, first_run)

    db.commit()
    db.refresh(job)
    db.refresh(run)
    return {"ok": True, "job": serialize_job(job), "next_run": serialize_run(run)}


# ---------------------------------------------------------------------------
# Flow 2: View
# ---------------------------------------------------------------------------
def list_jobs(db: Session, params: ListJobsParams) -> dict:
    stmt = select(Job).where(Job.user_id == params.user_id)
    if params.status is not None:
        stmt = stmt.where(Job.status == params.status)
    else:
        # Deleted jobs are hidden from default list results.
        stmt = stmt.where(Job.status != JOB_DELETED)
    stmt = stmt.order_by(Job.created_at.desc(), Job.job_id.desc())

    jobs = list(db.execute(stmt).scalars().all())

    # Attach next-run info, then optionally filter by the requested time window.
    items: list[tuple[Job, datetime | None]] = []
    for job in jobs:
        nxt = _next_pending_run(db, job)
        nxt_at = nxt.scheduled_at if nxt else None
        if not _within_window(nxt_at, params.start_time, params.end_time):
            continue
        items.append((job, nxt_at))

    total = len(items)
    start = (params.page - 1) * params.page_size
    page_items = items[start : start + params.page_size]

    return {
        "ok": True,
        "jobs": [
            {
                "job_id": job.job_id,
                "user_id": job.user_id,
                "action": job.action,
                "type": job.type,
                "schedule": job.schedule,
                "timezone": job.timezone,
                "status": job.status,
                "next_run_at": iso_in_tz(nxt_at, job.timezone),
                "created_at": iso_utc(job.created_at),
                "updated_at": iso_utc(job.updated_at),
            }
            for job, nxt_at in page_items
        ],
        "pagination": {
            "page": params.page,
            "page_size": params.page_size,
            "total": total,
        },
    }


def _within_window(
    nxt_at: datetime | None,
    start_time: datetime | None,
    end_time: datetime | None,
) -> bool:
    """Time-range filter on a job's next run (naive-UTC comparison)."""
    if start_time is None and end_time is None:
        return True
    if nxt_at is None:
        return False
    if start_time is not None and nxt_at < _to_naive_utc(start_time):
        return False
    if end_time is not None and nxt_at > _to_naive_utc(end_time):
        return False
    return True


def _to_naive_utc(dt: datetime) -> datetime:
    from app.core.timeutils import UTC

    if dt.tzinfo is not None:
        return dt.astimezone(UTC).replace(tzinfo=None)
    return dt


def get_job(db: Session, *, job_id: str, user_id: str) -> dict:
    job = _load_owned_job(db, job_id, user_id)
    stmt = (
        select(JobRun)
        .where(JobRun.job_id == job.job_id)
        .order_by(JobRun.scheduled_at.desc())
        .limit(RECENT_RUNS_LIMIT)
    )
    runs = db.execute(stmt).scalars().all()

    def _run_dict(run: JobRun) -> dict:
        # Embed the run's trace summary so a user can inspect what happened
        # without first knowing the trace id (spec 04, section 8).
        trace = latest_trace_for_run(db, run.run_id)
        return {
            "run_id": run.run_id,
            "scheduled_at": iso_in_tz(run.scheduled_at, job.timezone),
            "status": run.status,
            "trace": trace_summary_dict(trace) if trace is not None else None,
        }

    return {
        "ok": True,
        "job": serialize_job(job),
        "runs": [_run_dict(run) for run in runs],
    }


# ---------------------------------------------------------------------------
# Flow 5: Trace lookup (spec 04)
# ---------------------------------------------------------------------------
def get_trace(db: Session, *, trace_id: str, user_id: str) -> dict:
    """Return one execution trace and its ordered events, ownership-checked.

    ``NOT_FOUND`` when the trace id is unknown; ``PERMISSION_DENIED`` when it
    belongs to another user — mirroring ``_load_owned_job`` (spec 04, section 8).
    """
    trace = db.get(ActionTrace, trace_id)
    if trace is None:
        raise AppError(NOT_FOUND, "Trace not found.", field="trace_id")
    if trace.user_id != user_id:
        raise AppError(
            PERMISSION_DENIED, "You do not own this trace.", field="user_id"
        )
    return {"ok": True, "trace": trace_to_dict(trace, events=True)}


# ---------------------------------------------------------------------------
# Flow 3: Modify
# ---------------------------------------------------------------------------
def modify_job(db: Session, *, job_id: str, req: ModifyJobRequest) -> dict:
    job = _load_owned_job(db, job_id, req.user_id)

    if job.status == JOB_DELETED:
        raise AppError(
            CONFLICT, "Deleted jobs cannot be modified.", field="status"
        )
    if job.type == TYPE_IMMEDIATE and job.status == JOB_COMPLETED:
        raise AppError(
            CONFLICT,
            "Completed immediate jobs cannot be modified.",
            field="status",
        )

    was_paused = job.status == JOB_PAUSED

    # Resolve the target configuration. A field omitted from the patch keeps
    # the job's current value.
    new_action = req.action if req.action is not None else job.action
    if req.action is not None and not is_supported(new_action):
        raise AppError(
            UNSUPPORTED_ACTION,
            f"Action '{new_action}' is not supported.",
            field="action",
            expected=sorted(SUPPORTED_ACTIONS),
        )

    # Resolve action params (patch replaces; omitted keeps current) and validate
    # the resulting config so a user can fix missing details before the run fires.
    if req.action_params is not None:
        target_action_params = req.action_params
    else:
        target_action_params = job_action_params(job)
    validate_action_params(new_action, target_action_params)

    p = req.job_params
    target_type = (p.type if p and p.type else job.type)
    target_tz = (p.timezone if p and p.timezone else job.timezone)
    schedule_provided = bool(p and p.schedule is not None)
    time_provided = bool(p and p.time is not None)

    # Determine target schedule/time, clearing fields that the target type
    # does not use so type switches stay consistent.
    if target_type == TYPE_RECURRING:
        target_schedule = p.schedule if schedule_provided else job.schedule
        raw_time = None
    elif target_type == TYPE_ONE_TIME:
        target_schedule = None
        raw_time = p.time if time_provided else job.time
    else:  # immediate
        target_schedule = None
        raw_time = None

    target_time_utc = _validate_type_params(
        target_type, time=raw_time, schedule=target_schedule, tz=target_tz
    )

    # Did anything that affects scheduling change?
    schedule_affecting = (
        target_type != job.type
        or target_schedule != job.schedule
        or target_time_utc != job.time
        or target_tz != job.timezone
    )

    # Apply field updates.
    job.action = new_action
    job.type = target_type
    job.schedule = target_schedule
    job.time = target_time_utc
    job.timezone = target_tz
    if req.action_params is not None:
        job.action_params_json = (
            json.dumps(target_action_params) if target_action_params else None
        )

    cancelled_count = 0
    new_run: JobRun | None = None

    if req.status == "paused" or (was_paused and req.status != "scheduled"):
        # Paused jobs create no new runs; drop any pending ones.
        cancelled_count = _cancel_pending_runs(db, job)
        job.status = JOB_PAUSED
    else:
        reactivating = req.status == "scheduled"
        if schedule_affecting or reactivating:
            cancelled_count = _cancel_pending_runs(db, job)
            first = _first_run_time_utc(
                target_type, target_schedule, target_time_utc, target_tz
            )
            new_run = _create_run(db, job, first)
            job.status = JOB_SCHEDULED
        else:
            # No scheduling change: keep existing runs and report the next one.
            new_run = _next_pending_run(db, job)

    db.commit()
    db.refresh(job)
    if new_run is not None:
        db.refresh(new_run)

    return {
        "ok": True,
        "job": serialize_job(job),
        "next_run": serialize_run(new_run) if new_run is not None else None,
        "cancelled_run_count": cancelled_count,
    }


# ---------------------------------------------------------------------------
# Flow 4: Delete
# ---------------------------------------------------------------------------
def delete_job(db: Session, *, job_id: str, user_id: str) -> dict:
    job = _load_owned_job(db, job_id, user_id)

    # Idempotent: re-deleting a deleted job returns its existing state.
    if job.status == JOB_DELETED:
        return {
            "ok": True,
            "job_id": job.job_id,
            "status": JOB_DELETED,
            "cancelled_run_count": 0,
        }

    cancelled_count = _cancel_pending_runs(db, job)
    job.status = JOB_DELETED
    db.commit()

    return {
        "ok": True,
        "job_id": job.job_id,
        "status": JOB_DELETED,
        "cancelled_run_count": cancelled_count,
    }
