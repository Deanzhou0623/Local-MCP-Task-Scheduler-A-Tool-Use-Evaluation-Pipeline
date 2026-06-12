"""Spec 04: traceable action execution.

Exercises the executor registry, trace recording, and the trace read surface by
calling the service layer and ``process_run`` directly with the ``db`` fixture
(no threads), plus one dispatch round-trip through the MCP registry.
"""

from __future__ import annotations

import pytest

from app.core.errors import AppError
from app.jobs import executors as executors_mod
from app.jobs.executors import ActionContext, ActionResult
from app.jobs.models import (
    RUN_FAILED,
    RUN_SUCCEEDED,
    TRACE_FAILED,
    TRACE_SKIPPED,
    TRACE_SUCCEEDED,
    ActionTrace,
)
from app.jobs.schemas import CreateJobRequest, ModifyJobRequest
from app.jobs.service import create_job, get_trace, modify_job
from app.jobs.traces import append_trace_event
from app.mcp import dispatch
from app.scheduler import process_run

USER = "demo-user"

EMAIL_PARAMS = {
    "to": "dean@example.com",
    "subject": "AI evals report",
    "body": "Placeholder AI evals report.",
}


def _create(db, action: str, action_params: dict | None = None) -> dict:
    """Create an immediate job and return the success envelope."""
    req = CreateJobRequest(
        user_id=USER,
        action=action,
        job_params={"type": "immediate"},
        action_params=action_params,
    )
    return create_job(db, req)


def _run_first(db, created: dict):
    """Execute the job's first (pending) run synchronously."""
    return process_run(db, created["next_run"]["run_id"])


# --- action_params storage ------------------------------------------------
def test_create_stores_and_returns_action_params(db):
    created = _create(db, "send_email", EMAIL_PARAMS)
    assert created["ok"] is True
    assert created["job"]["action_params"] == EMAIL_PARAMS


def test_modify_replaces_action_params(db):
    created = _create(db, "send_email", EMAIL_PARAMS)
    job_id = created["job"]["job_id"]
    new_params = {**EMAIL_PARAMS, "subject": "Updated subject"}
    modified = modify_job(
        db,
        job_id=job_id,
        req=ModifyJobRequest(user_id=USER, action_params=new_params),
    )
    assert modified["job"]["action_params"]["subject"] == "Updated subject"


def test_missing_email_body_at_create_is_rejected(db):
    incomplete = {"to": "dean@example.com", "subject": "Hi"}
    with pytest.raises(AppError) as exc:
        _create(db, "send_email", incomplete)
    assert exc.value.code == "VALIDATION_ERROR"
    assert exc.value.field == "action_params.body"


# --- one trace per run, ordered events ------------------------------------
def test_worker_creates_one_trace_per_run(db):
    created = _create(db, "send_email", EMAIL_PARAMS)
    run = _run_first(db, created)
    traces = db.query(ActionTrace).filter_by(run_id=run.run_id).all()
    assert len(traces) == 1


def test_send_email_success_records_ordered_steps_and_artifact(db):
    created = _create(db, "send_email", EMAIL_PARAMS)
    run = _run_first(db, created)
    assert run.status == RUN_SUCCEEDED

    trace = db.query(ActionTrace).filter_by(run_id=run.run_id).one()
    assert trace.status == TRACE_SUCCEEDED

    got = get_trace(db, trace_id=trace.trace_id, user_id=USER)["trace"]
    stages = [(e["sequence"], e["stage"]) for e in got["events"]]
    assert stages == [
        (1, "validate_action_params"),
        (2, "render_email"),
        (3, "mock_send_email"),
    ]
    # The scheduler — not the LLM — records the artifact.
    assert got["artifact"]["provider"] == "mock"
    assert got["artifact"]["message_id"].startswith("mock_msg_")


def test_placeholder_executor_still_writes_an_event(db):
    created = _create(db, "generate_report")
    run = _run_first(db, created)
    assert run.status == RUN_SUCCEEDED
    trace = db.query(ActionTrace).filter_by(run_id=run.run_id).one()
    got = get_trace(db, trace_id=trace.trace_id, user_id=USER)["trace"]
    assert [e["stage"] for e in got["events"]] == ["mock_execute"]


# --- failure path still produces a trace ----------------------------------
def test_invalid_params_at_execute_time_fails_with_trace(db):
    """A job that reaches execution with invalid params (e.g. created before the
    requirement existed) still runs, fails validation, and saves a trace."""
    created = _create(db, "send_email", EMAIL_PARAMS)
    job_id = created["job"]["job_id"]

    # Simulate a job whose stored params lost the body after creation.
    from app.jobs.models import Job

    job = db.get(Job, job_id)
    job.action_params_json = '{"to": "dean@example.com", "subject": "Hi"}'
    db.commit()

    run = _run_first(db, created)
    assert run.status == RUN_FAILED

    trace = db.query(ActionTrace).filter_by(run_id=run.run_id).one()
    assert trace.status == TRACE_FAILED
    got = get_trace(db, trace_id=trace.trace_id, user_id=USER)["trace"]
    failed = [e for e in got["events"] if e["status"] == "failed"]
    assert failed and failed[0]["stage"] == "validate_action_params"


# --- skipped maps to a succeeded run --------------------------------------
def test_skipped_result_marks_run_succeeded_trace_skipped(db, monkeypatch):
    class SkipExecutor:
        def execute(self, ctx: ActionContext) -> ActionResult:
            append_trace_event(
                ctx.db, ctx.trace_id, stage="mock_execute", status="skipped"
            )
            return ActionResult(status="skipped", summary="Nothing to do.")

    monkeypatch.setitem(executors_mod.EXECUTORS, "generate_report", SkipExecutor())

    created = _create(db, "generate_report")
    run = _run_first(db, created)
    assert run.status == RUN_SUCCEEDED  # skipped counts as run success

    trace = db.query(ActionTrace).filter_by(run_id=run.run_id).one()
    assert trace.status == TRACE_SKIPPED


# --- trace read ownership -------------------------------------------------
def test_get_trace_rejects_wrong_user(db):
    created = _create(db, "send_email", EMAIL_PARAMS)
    run = _run_first(db, created)
    trace = db.query(ActionTrace).filter_by(run_id=run.run_id).one()
    with pytest.raises(AppError) as exc:
        get_trace(db, trace_id=trace.trace_id, user_id="someone-else")
    assert exc.value.code == "PERMISSION_DENIED"


def test_get_trace_unknown_id_is_not_found(db):
    with pytest.raises(AppError) as exc:
        get_trace(db, trace_id="trace_missing", user_id=USER)
    assert exc.value.code == "NOT_FOUND"


# --- registry wiring ------------------------------------------------------
def test_trace_get_dispatch_round_trip(isolated_db):
    """The MCP registry routes ``task.trace.get@v1`` end to end."""
    from app.core.database import SessionLocal

    session = SessionLocal()
    try:
        created = _create(session, "send_email", EMAIL_PARAMS)
        run = _run_first(session, created)
        trace = session.query(ActionTrace).filter_by(run_id=run.run_id).one()
        trace_id = trace.trace_id
    finally:
        session.close()

    res = dispatch("task.trace.get@v1", {"user_id": USER, "trace_id": trace_id})
    assert res["ok"] is True
    assert res["trace"]["trace_id"] == trace_id
    assert res["trace"]["status"] == "succeeded"
