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

# --- Run trigger reasons / scheduling metadata (spec 05/06) ---------------
TRIGGER_IMMEDIATE = "immediate"
TRIGGER_SCHEDULED = "scheduled"
TRIGGER_RETRY = "retry"
TRIGGER_MANUAL = "manual"
DEFAULT_PRIORITY = 10

# --- Durable queue statuses (spec 05) --------------------------------------
QUEUE_READY = "ready"
QUEUE_LEASED = "leased"
QUEUE_DONE = "done"
QUEUE_FAILED = "failed"
QUEUE_CANCELLED = "cancelled"
QUEUE_ACTIVE = {QUEUE_READY, QUEUE_LEASED}

# --- Action trace statuses (spec 04) ---------------------------------------
TRACE_PENDING = "pending"
TRACE_RUNNING = "running"
TRACE_SUCCEEDED = "succeeded"
TRACE_FAILED = "failed"
TRACE_SKIPPED = "skipped"

# Per-event statuses on the append-only step log.
EVENT_STARTED = "started"
EVENT_SUCCEEDED = "succeeded"
EVENT_FAILED = "failed"
EVENT_SKIPPED = "skipped"


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
    # Action-specific params ("what should this action do?"), JSON text (spec 04).
    action_params_json: Mapped[str | None] = mapped_column(Text, nullable=True)
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
    scheduled_bucket_hour: Mapped[str | None] = mapped_column(String(10), nullable=True)
    scheduled_bucket_shard: Mapped[int | None] = mapped_column(Integer, nullable=True)
    scheduled_bucket: Mapped[str] = mapped_column(String(20))
    started_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    status: Mapped[str] = mapped_column(String(16), default=RUN_PENDING)
    retry_count: Mapped[int] = mapped_column(Integer, default=0)
    attempt_group_id: Mapped[str | None] = mapped_column(String(48), nullable=True)
    attempt_number: Mapped[int] = mapped_column(Integer, default=1)
    parent_run_id: Mapped[str | None] = mapped_column(String(40), nullable=True)
    trigger_reason: Mapped[str] = mapped_column(String(16), default=TRIGGER_SCHEDULED)
    priority: Mapped[int] = mapped_column(Integer, default=DEFAULT_PRIORITY)
    deadline_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
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
        Index(
            "idx_runs_bucket_hour_status_sched",
            "scheduled_bucket_hour",
            "status",
            "scheduled_at",
        ),
        # Job detail + run history.
        Index("idx_runs_job_sched", "job_id", "scheduled_at"),
        # Retry/run-history lookups by logical attempt group.
        Index("idx_runs_attempt_group", "attempt_group_id", "attempt_number"),
    )

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return f"<JobRun {self.run_id} job={self.job_id} status={self.status!r}>"


class JobRunQueue(Base):
    """Durable queue item for executing one ``JobRun`` (spec 05).

    The in-memory queue is no longer the source of truth. Watchers enqueue due
    runs here, and workers claim rows with a lease.
    """

    __tablename__ = "job_run_queue"

    queue_id: Mapped[str] = mapped_column(String(48), primary_key=True)
    run_id: Mapped[str] = mapped_column(
        String(40), ForeignKey("job_runs.run_id"), index=True
    )
    user_id: Mapped[str] = mapped_column(String(64))
    priority: Mapped[int] = mapped_column(Integer, default=DEFAULT_PRIORITY)
    status: Mapped[str] = mapped_column(String(16), default=QUEUE_READY)
    available_at: Mapped[datetime] = mapped_column(DateTime)
    locked_by: Mapped[str | None] = mapped_column(String(128), nullable=True)
    locked_until: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    attempt_number: Mapped[int] = mapped_column(Integer, default=1)
    error_message: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow_naive)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, default=utcnow_naive, onupdate=utcnow_naive
    )

    run: Mapped["JobRun"] = relationship()

    __table_args__ = (
        Index(
            "idx_queue_ready_priority_available",
            "status",
            "priority",
            "available_at",
        ),
        Index("idx_queue_run_status", "run_id", "status"),
        Index("idx_queue_locked_until", "status", "locked_until"),
    )

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return f"<JobRunQueue {self.queue_id} run={self.run_id} status={self.status!r}>"


Index(
    "uq_queue_active_run",
    JobRunQueue.run_id,
    unique=True,
    sqlite_where=JobRunQueue.status.in_([QUEUE_READY, QUEUE_LEASED]),
)


class ActionTrace(Base):
    """One execution trace per ``JobRun`` (spec 04, section 4).

    The scheduler — not the LLM — owns this record. It is created when a run
    starts executing and finalized to ``succeeded``/``failed``/``skipped`` so
    evals have ground truth about what actually happened.
    """

    __tablename__ = "action_traces"

    trace_id: Mapped[str] = mapped_column(String(48), primary_key=True)
    # One trace per run for spec 04 (no retries yet); unique guards that.
    run_id: Mapped[str] = mapped_column(
        String(40), ForeignKey("job_runs.run_id"), unique=True, index=True
    )
    job_id: Mapped[str] = mapped_column(String(40), index=True)
    user_id: Mapped[str] = mapped_column(String(64))
    action: Mapped[str] = mapped_column(String(128))
    status: Mapped[str] = mapped_column(String(16), default=TRACE_PENDING)
    started_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    summary: Mapped[str | None] = mapped_column(Text, nullable=True)
    # Serialized ActionResult.artifact (JSON text); exposed as a dict.
    artifact_json: Mapped[str | None] = mapped_column(Text, nullable=True)
    error_message: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow_naive)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, default=utcnow_naive, onupdate=utcnow_naive
    )

    events: Mapped[list["ActionTraceEvent"]] = relationship(
        back_populates="trace",
        cascade="all, delete-orphan",
        order_by="ActionTraceEvent.sequence",
    )

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return f"<ActionTrace {self.trace_id} status={self.status!r}>"


class ActionTraceEvent(Base):
    """Append-only, ordered step log for a trace (spec 04, section 4)."""

    __tablename__ = "action_trace_events"

    event_id: Mapped[str] = mapped_column(String(48), primary_key=True)
    trace_id: Mapped[str] = mapped_column(
        String(48), ForeignKey("action_traces.trace_id"), index=True
    )
    sequence: Mapped[int] = mapped_column(Integer)
    stage: Mapped[str] = mapped_column(String(64))
    status: Mapped[str] = mapped_column(String(16))
    input_json: Mapped[str | None] = mapped_column(Text, nullable=True)
    output_json: Mapped[str | None] = mapped_column(Text, nullable=True)
    error_message: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow_naive)

    trace: Mapped["ActionTrace"] = relationship(back_populates="events")

    __table_args__ = (
        # Read a trace's events in order.
        Index("idx_trace_events_trace_seq", "trace_id", "sequence"),
    )

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return f"<ActionTraceEvent {self.stage} seq={self.sequence}>"
