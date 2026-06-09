"""ORM models for the task scheduler (spec 01, section 11).

Two tables:

- ``jobs`` holds the durable job *definition* (what to run, when, for whom).
- ``job_runs`` holds each concrete *execution attempt*.

All datetimes are stored as naive UTC (see :mod:`app.timeutils`). Indexes match
the watcher, job-detail, and list query paths described in the spec.
"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import (
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.core.database import Base
from app.core.timeutils import utcnow_naive

# --- Job types -------------------------------------------------------------
TYPE_IMMEDIATE = "immediate"
TYPE_ONE_TIME = "one_time"
TYPE_RECURRING = "recurring"
JOB_TYPES = {TYPE_IMMEDIATE, TYPE_ONE_TIME, TYPE_RECURRING}

# --- Job statuses ----------------------------------------------------------
JOB_SCHEDULED = "scheduled"
JOB_PAUSED = "paused"
JOB_RUNNING = "running"
JOB_COMPLETED = "completed"
JOB_FAILED = "failed"
JOB_DELETED = "deleted"

# --- Run statuses ----------------------------------------------------------
RUN_PENDING = "pending"
RUN_QUEUED = "queued"
RUN_RUNNING = "running"
RUN_SUCCEEDED = "succeeded"
RUN_FAILED = "failed"
RUN_CANCELLED = "cancelled"

# Runs the watcher/delete logic may still cancel (not yet started executing).
RUN_CANCELLABLE = {RUN_PENDING, RUN_QUEUED}


class Job(Base):
    __tablename__ = "jobs"

    job_id: Mapped[str] = mapped_column(String(40), primary_key=True)
    user_id: Mapped[str] = mapped_column(String(64))
    action: Mapped[str] = mapped_column(String(128))
    type: Mapped[str] = mapped_column(String(16))
    # Absolute fire time for one-time jobs (naive UTC); null otherwise.
    time: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    # Cron expression for recurring jobs; null otherwise.
    schedule: Mapped[str | None] = mapped_column(String(128), nullable=True)
    timezone: Mapped[str] = mapped_column(String(64), default="UTC")
    status: Mapped[str] = mapped_column(String(16), default=JOB_SCHEDULED)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow_naive)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, default=utcnow_naive, onupdate=utcnow_naive
    )

    runs: Mapped[list["JobRun"]] = relationship(
        back_populates="job", cascade="all, delete-orphan"
    )

    __table_args__ = (
        # List queries filter by user + status and sort by created_at.
        Index("idx_jobs_user_status_created", "user_id", "status", "created_at"),
    )

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return f"<Job {self.job_id} type={self.type!r} status={self.status!r}>"


class JobRun(Base):
    __tablename__ = "job_runs"

    run_id: Mapped[str] = mapped_column(String(40), primary_key=True)
    job_id: Mapped[str] = mapped_column(
        String(40), ForeignKey("jobs.job_id"), index=True
    )
    # Copied from the parent job so run queries can filter by owner directly.
    user_id: Mapped[str] = mapped_column(String(64))
    scheduled_at: Mapped[datetime] = mapped_column(DateTime)
    scheduled_bucket: Mapped[str] = mapped_column(String(13))
    started_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    status: Mapped[str] = mapped_column(String(16), default=RUN_PENDING)
    retry_count: Mapped[int] = mapped_column(Integer, default=0)
    error_message: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow_naive)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, default=utcnow_naive, onupdate=utcnow_naive
    )

    job: Mapped["Job"] = relationship(back_populates="runs")

    __table_args__ = (
        # Watcher: scan due pending runs within hot buckets.
        Index(
            "idx_runs_bucket_status_sched",
            "scheduled_bucket",
            "status",
            "scheduled_at",
        ),
        # Job detail + run history.
        Index("idx_runs_job_sched", "job_id", "scheduled_at"),
    )

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return f"<JobRun {self.run_id} job={self.job_id} status={self.status!r}>"
