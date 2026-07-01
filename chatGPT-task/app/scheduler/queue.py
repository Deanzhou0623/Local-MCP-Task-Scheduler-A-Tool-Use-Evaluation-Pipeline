"""Durable queue helpers for executing due runs (spec 05).

The original prototype used an in-memory ``Queue[str]``. Spec 05 keeps that
object only as a best-effort local wakeup signal; durable queue rows are the
source of truth.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from queue import Queue

from sqlalchemy import func, select, update
from sqlalchemy.orm import Session

from app.core.ids import new_queue_id
from app.core.timeutils import utcnow_naive
from app.jobs.models import (
    DEFAULT_PRIORITY,
    QUEUE_ACTIVE,
    QUEUE_CANCELLED,
    QUEUE_DONE,
    QUEUE_FAILED,
    QUEUE_LEASED,
    QUEUE_READY,
    RUN_QUEUED,
    RUN_RUNNING,
    JobRun,
    JobRunQueue,
)

# Optional local wakeup signal. Workers should poll ``job_run_queue`` and must
# not depend on this in-memory queue for correctness.
run_queue: "Queue[str]" = Queue()


def enqueue_run_once(
    db: Session, run: JobRun, *, priority: int | None = None
) -> JobRunQueue:
    """Create one active queue row for ``run`` or return the existing one."""
    existing = db.execute(
        select(JobRunQueue).where(
            JobRunQueue.run_id == run.run_id,
            JobRunQueue.status.in_(QUEUE_ACTIVE),
        )
    ).scalar_one_or_none()
    if existing is not None:
        return existing

    item = JobRunQueue(
        queue_id=new_queue_id(),
        run_id=run.run_id,
        user_id=run.user_id,
        priority=priority if priority is not None else (run.priority or DEFAULT_PRIORITY),
        status=QUEUE_READY,
        available_at=run.scheduled_at,
        attempt_number=run.attempt_number or 1,
    )
    db.add(item)
    db.flush()
    return item


def claim_next_queue_item(
    db: Session,
    *,
    worker_id: str,
    lease_seconds: int,
    now: datetime | None = None,
) -> JobRunQueue | None:
    """Claim the next ready item with a conditional update.

    SQLite has no ``FOR UPDATE SKIP LOCKED``. This uses a single update against
    the selected queue id, then reads back the row claimed by this worker.
    """
    now = now or utcnow_naive()
    locked_until = now + timedelta(seconds=lease_seconds)
    selected_id = db.execute(
        select(JobRunQueue.queue_id)
        .where(
            JobRunQueue.status == QUEUE_READY,
            JobRunQueue.available_at <= now,
        )
        .order_by(
            JobRunQueue.priority.asc(),
            JobRunQueue.available_at.asc(),
            JobRunQueue.queue_id.asc(),
        )
        .limit(1)
    ).scalar_one_or_none()
    if selected_id is None:
        return None

    result = db.execute(
        update(JobRunQueue)
        .where(
            JobRunQueue.queue_id == selected_id,
            JobRunQueue.status == QUEUE_READY,
        )
        .values(
            status=QUEUE_LEASED,
            locked_by=worker_id,
            locked_until=locked_until,
            updated_at=now,
        )
    )
    if result.rowcount == 0:
        return None

    item = db.get(JobRunQueue, selected_id)
    db.flush()
    return item


def complete_queue_item(db: Session, item: JobRunQueue) -> None:
    item.status = QUEUE_DONE
    item.locked_by = None
    item.locked_until = None
    item.error_message = None
    item.updated_at = utcnow_naive()
    db.flush()


def fail_queue_item(db: Session, item: JobRunQueue, error: str | None = None) -> None:
    item.status = QUEUE_FAILED
    item.locked_by = None
    item.locked_until = None
    item.error_message = error
    item.updated_at = utcnow_naive()
    db.flush()


def cancel_queue_items_for_run(db: Session, run_id: str) -> int:
    items = db.execute(
        select(JobRunQueue).where(
            JobRunQueue.run_id == run_id,
            JobRunQueue.status.in_(QUEUE_ACTIVE),
        )
    ).scalars().all()
    for item in items:
        item.status = QUEUE_CANCELLED
        item.locked_by = None
        item.locked_until = None
        item.updated_at = utcnow_naive()
    db.flush()
    return len(items)


def recover_expired_leases(db: Session, *, now: datetime | None = None) -> int:
    now = now or utcnow_naive()
    items = db.execute(
        select(JobRunQueue).where(
            JobRunQueue.status == QUEUE_LEASED,
            JobRunQueue.locked_until <= now,
        )
    ).scalars().all()
    recovered = 0
    for item in items:
        run = db.get(JobRun, item.run_id)
        if run is not None and run.status == RUN_RUNNING:
            continue
        item.status = QUEUE_READY
        item.locked_by = None
        item.locked_until = None
        item.updated_at = now
        recovered += 1
    db.flush()
    return recovered


def recover_stranded_queued_runs(db: Session) -> int:
    """Recreate queue rows for runs marked queued without active queue state."""
    runs = db.execute(
        select(JobRun).where(JobRun.status == RUN_QUEUED)
    ).scalars().all()
    recovered = 0
    for run in runs:
        existing = db.execute(
            select(JobRunQueue.queue_id).where(
                JobRunQueue.run_id == run.run_id,
                JobRunQueue.status.in_(QUEUE_ACTIVE),
            )
        ).scalar_one_or_none()
        if existing is None:
            enqueue_run_once(db, run, priority=run.priority)
            recovered += 1
    db.flush()
    return recovered


def queue_status_counts(db: Session) -> dict[str, int]:
    rows = db.execute(
        select(JobRunQueue.status, func.count(JobRunQueue.queue_id)).group_by(
            JobRunQueue.status
        )
    ).all()
    return {status: count for status, count in rows}


def run_status_counts(db: Session) -> dict[str, int]:
    rows = db.execute(
        select(JobRun.status, func.count(JobRun.run_id)).group_by(JobRun.status)
    ).all()
    return {status: count for status, count in rows}


def scheduler_status(db: Session, *, now: datetime | None = None) -> dict:
    now = now or utcnow_naive()
    expired = db.execute(
        select(func.count(JobRunQueue.queue_id)).where(
            JobRunQueue.status == QUEUE_LEASED,
            JobRunQueue.locked_until <= now,
        )
    ).scalar_one()
    queue = queue_status_counts(db)
    queue["expired_leases"] = expired
    return {"ok": True, "runs": run_status_counts(db), "queue": queue}
