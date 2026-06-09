"""Watcher: find due runs by time bucket and enqueue them (spec 01, §12-13).

The watcher never issues a broad ``WHERE scheduled_at <= now`` scan. It looks
only at the current hourly ``scheduled_bucket`` plus a short lookback window, so
each poll stays bounded by due-time locality instead of total table size.
"""

from __future__ import annotations

import time
from datetime import datetime, timedelta

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.database import SessionLocal
from app.core.timeutils import time_bucket, utcnow_naive
from app.jobs.models import RUN_PENDING, RUN_QUEUED, JobRun
from app.scheduler.queue import run_queue

# Also scan this many past buckets so a run that fell due just before an hour
# boundary is not missed.
BUCKET_LOOKBACK_HOURS = 1
WATCH_BATCH_SIZE = 100


def hot_buckets(now_utc: datetime) -> list[str]:
    """Current bucket plus a short lookback window."""
    return [
        time_bucket(now_utc - timedelta(hours=h))
        for h in range(BUCKET_LOOKBACK_HOURS + 1)
    ]


def find_due_runs(now_utc: datetime, db: Session) -> list[JobRun]:
    """Pending runs in the hot buckets whose scheduled time has arrived."""
    stmt = (
        select(JobRun)
        .where(
            JobRun.scheduled_bucket.in_(hot_buckets(now_utc)),
            JobRun.status == RUN_PENDING,
            JobRun.scheduled_at <= now_utc,
        )
        .order_by(JobRun.scheduled_at.asc())
        .limit(WATCH_BATCH_SIZE)
    )
    return list(db.execute(stmt).scalars().all())


def watcher_loop(interval: int = 10) -> None:
    """Scan, mark due runs ``queued``, and hand their ids to the worker."""
    while True:
        db = SessionLocal()
        try:
            due = find_due_runs(utcnow_naive(), db)
            for run in due:
                run.status = RUN_QUEUED
            db.commit()
            ids = [run.run_id for run in due]
        finally:
            db.close()
        for run_id in ids:
            run_queue.put(run_id)
        time.sleep(interval)
