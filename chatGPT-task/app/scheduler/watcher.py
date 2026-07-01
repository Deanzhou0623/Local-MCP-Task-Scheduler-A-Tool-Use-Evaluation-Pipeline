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
from app.scheduler.queue import (
    enqueue_run_once,
    recover_expired_leases,
    recover_stranded_queued_runs,
    run_queue,
)
from app.scheduler.worker import recover_stale_running_runs

# Also scan this many past buckets so a run that fell due just before an hour
# boundary is not missed.
BUCKET_LOOKBACK_HOURS = 1
WATCH_BATCH_SIZE = 100
RECOVERY_INTERVAL_SECONDS = 60


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


def enqueue_due_runs(db: Session, now_utc: datetime | None = None) -> list[str]:
    """Persist queue rows for due runs and return enqueued run ids."""
    due = find_due_runs(now_utc or utcnow_naive(), db)
    ids: list[str] = []
    for run in due:
        enqueue_run_once(db, run, priority=run.priority)
        run.status = RUN_QUEUED
        ids.append(run.run_id)
    db.commit()
    return ids


def recovery_tick(db: Session, now_utc: datetime | None = None) -> dict[str, int]:
    """Run synchronous recovery helpers for stuck queue/run states."""
    now_utc = now_utc or utcnow_naive()
    result = {
        "expired_leases": recover_expired_leases(db, now=now_utc),
        "stranded_queued": recover_stranded_queued_runs(db),
        "stale_running": recover_stale_running_runs(db, now=now_utc),
    }
    db.commit()
    return result


def watcher_loop(interval: int = 10) -> None:
    """Scan, mark due runs ``queued``, and persist durable queue rows."""
    last_recovery_at: datetime | None = None
    while True:
        now = utcnow_naive()
        db = SessionLocal()
        try:
            if (
                last_recovery_at is None
                or (now - last_recovery_at).total_seconds() >= RECOVERY_INTERVAL_SECONDS
            ):
                recovery_tick(db, now)
                last_recovery_at = now
            ids = enqueue_due_runs(db, now)
        finally:
            db.close()
        for run_id in ids:
            run_queue.put(run_id)
        time.sleep(interval)
