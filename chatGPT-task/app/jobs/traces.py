"""Action-trace repository helpers and serializers (spec 04, sections 4-5).

The scheduler owns the trace. The worker calls :func:`create_trace_for_run`
before executing, executors call :func:`append_trace_event` per step, and the
worker calls :func:`finish_trace` to finalize. JSON payloads are stored as text
(SQLite MVP) and parsed back into dicts by the serializers, so a trace reads out
in the same shape an executor wrote it.
"""

from __future__ import annotations

import json
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.core.ids import new_trace_event_id, new_trace_id
from app.core.timeutils import iso_utc, utcnow_naive
from app.jobs.models import (
    TRACE_PENDING,
    ActionTrace,
    ActionTraceEvent,
    Job,
    JobRun,
)


def _dumps(value: Any | None) -> str | None:
    return None if value is None else json.dumps(value)


def _loads(text: str | None) -> Any | None:
    return None if text is None else json.loads(text)


# ---------------------------------------------------------------------------
# Repository helpers
# ---------------------------------------------------------------------------
def create_trace_for_run(db: Session, run: JobRun, job: Job) -> ActionTrace:
    """Create the (pending) trace row for a run about to execute."""
    trace = ActionTrace(
        trace_id=new_trace_id(),
        run_id=run.run_id,
        job_id=job.job_id,
        user_id=job.user_id,
        action=job.action,
        status=TRACE_PENDING,
    )
    db.add(trace)
    db.flush()  # make the trace_id queryable for event FKs
    return trace


def append_trace_event(
    db: Session,
    trace_id: str,
    *,
    stage: str,
    status: str,
    input: dict | None = None,
    output: dict | None = None,
    error: str | None = None,
) -> ActionTraceEvent:
    """Append the next ordered event to a trace."""
    next_seq = (
        db.execute(
            select(func.count(ActionTraceEvent.event_id)).where(
                ActionTraceEvent.trace_id == trace_id
            )
        ).scalar_one()
        + 1
    )
    event = ActionTraceEvent(
        event_id=new_trace_event_id(),
        trace_id=trace_id,
        sequence=next_seq,
        stage=stage,
        status=status,
        input_json=_dumps(input),
        output_json=_dumps(output),
        error_message=error,
    )
    db.add(event)
    db.flush()
    return event


def finish_trace(
    db: Session,
    trace: ActionTrace,
    status: str,
    *,
    summary: str | None = None,
    artifact: dict | None = None,
    error: str | None = None,
) -> ActionTrace:
    """Finalize a trace's status and result fields."""
    trace.status = status
    trace.finished_at = utcnow_naive()
    if summary is not None:
        trace.summary = summary
    if artifact is not None:
        trace.artifact_json = _dumps(artifact)
    if error is not None:
        trace.error_message = error
    db.flush()
    return trace


def latest_trace_for_run(db: Session, run_id: str) -> ActionTrace | None:
    return db.execute(
        select(ActionTrace).where(ActionTrace.run_id == run_id)
    ).scalar_one_or_none()


def completed_stages_for_attempt_group(
    db: Session, attempt_group_id: str | None
) -> set[str]:
    """Stages that succeeded in any previous attempt in the same group."""
    if not attempt_group_id:
        return set()
    stmt = (
        select(ActionTraceEvent.stage)
        .join(ActionTrace, ActionTrace.trace_id == ActionTraceEvent.trace_id)
        .join(JobRun, JobRun.run_id == ActionTrace.run_id)
        .where(
            JobRun.attempt_group_id == attempt_group_id,
            ActionTraceEvent.status == "succeeded",
        )
    )
    return set(db.execute(stmt).scalars().all())


# ---------------------------------------------------------------------------
# Serializers
# ---------------------------------------------------------------------------
def trace_event_to_dict(event: ActionTraceEvent) -> dict:
    return {
        "sequence": event.sequence,
        "stage": event.stage,
        "status": event.status,
        "input_json": _loads(event.input_json),
        "output_json": _loads(event.output_json),
        "error_message": event.error_message,
    }


def trace_summary_dict(trace: ActionTrace) -> dict:
    """Compact trace view embedded next to a run in ``task_get_v1``."""
    return {
        "trace_id": trace.trace_id,
        "status": trace.status,
        "summary": trace.summary,
    }


def trace_to_dict(trace: ActionTrace, *, events: bool = False) -> dict:
    """Full trace representation; include ordered events when requested."""
    data = {
        "trace_id": trace.trace_id,
        "run_id": trace.run_id,
        "job_id": trace.job_id,
        "user_id": trace.user_id,
        "action": trace.action,
        "status": trace.status,
        "started_at": iso_utc(trace.started_at),
        "finished_at": iso_utc(trace.finished_at),
        "summary": trace.summary,
        "artifact": _loads(trace.artifact_json),
        "error_message": trace.error_message,
    }
    if events:
        data["events"] = [trace_event_to_dict(e) for e in trace.events]
    return data
