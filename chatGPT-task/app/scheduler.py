"""Watcher + worker scheduler.

The watcher periodically scans SQLite for due pending jobs, marks them as
``queued``, and pushes their IDs into an in-memory queue. The worker consumes
job IDs from that queue and executes them.

This demonstrates the time-bucket partitioning idea without a production queue
or NoSQL store.
"""

from __future__ import annotations

import queue
import threading
import time
from datetime import datetime

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.database import SessionLocal
from app.models import (
    STATUS_CANCELLED,
    STATUS_COMPLETED,
    STATUS_FAILED,
    STATUS_PENDING,
    STATUS_QUEUED,
    STATUS_RUNNING,
    Job,
)

# In-memory queue that simulates a production job queue.
job_queue: "queue.Queue[int]" = queue.Queue()


def get_time_bucket(scheduled_at: datetime) -> str:
    """Convert a datetime to an hourly bucket string.

    Example: ``2026-06-08 11:20`` -> ``"2026060811"``.
    """
    return scheduled_at.strftime("%Y%m%d%H")


def find_due_jobs(current_time: datetime, db: Session) -> list[Job]:
    """Return pending jobs in the current hour bucket that are due now."""
    bucket = get_time_bucket(current_time)
    stmt = select(Job).where(
        Job.time_bucket == bucket,
        Job.scheduled_at <= current_time,
        Job.status == STATUS_PENDING,
    )
    return list(db.execute(stmt).scalars().all())


def watcher_loop(interval: int = 10) -> None:
    """Scan for due jobs every ``interval`` seconds and enqueue them."""
    while True:
        db = SessionLocal()
        try:
            due_jobs = find_due_jobs(datetime.utcnow(), db)
            for job in due_jobs:
                job.status = STATUS_QUEUED
            db.commit()
            for job in due_jobs:
                job_queue.put(job.id)
        finally:
            db.close()
        time.sleep(interval)


def worker_loop() -> None:
    """Continuously consume job IDs and execute them."""
    while True:
        job_id = job_queue.get()
        db = SessionLocal()
        try:
            job = db.get(Job, job_id)
            if job is None or job.status == STATUS_CANCELLED:
                continue

            job.status = STATUS_RUNNING
            db.commit()

            try:
                # Placeholder action. A real implementation would dispatch on
                # the job description / action type here.
                job.result = f"Executed: {job.description}"
                job.status = STATUS_COMPLETED
                db.commit()
            except Exception as exc:  # noqa: BLE001 - record any failure
                job.status = STATUS_FAILED
                job.result = f"Error: {exc}"
                db.commit()
        finally:
            db.close()
            job_queue.task_done()


def start_scheduler(interval: int = 10) -> tuple[threading.Thread, threading.Thread]:
    """Start daemon watcher and worker threads."""
    watcher = threading.Thread(
        target=watcher_loop, args=(interval,), daemon=True, name="watcher"
    )
    worker = threading.Thread(target=worker_loop, daemon=True, name="worker")
    watcher.start()
    worker.start()
    return watcher, worker
