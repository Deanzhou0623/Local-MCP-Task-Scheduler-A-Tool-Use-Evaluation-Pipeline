"""Service-layer behavior for the four core flows (spec 01, sections 6-9, 12)."""

from __future__ import annotations

import pytest

from app.core.errors import (
    CONFLICT,
    NOT_FOUND,
    PERMISSION_DENIED,
    UNSUPPORTED_ACTION,
    VALIDATION_ERROR,
    AppError,
)
from app.jobs.models import (
    JOB_DELETED,
    JOB_PAUSED,
    JOB_SCHEDULED,
    RUN_CANCELLED,
    RUN_PENDING,
    Job,
    JobRun,
)
from app.jobs.schemas import CreateJobRequest, ListJobsParams, ModifyJobRequest
from app.jobs.service import (
    create_job,
    delete_job,
    get_job,
    list_jobs,
    modify_job,
)

USER = "user_123"


def _create(db, **job_params):
    req = CreateJobRequest(user_id=USER, action="review_pr", job_params=job_params)
    return create_job(db, req)


# --- Create ---------------------------------------------------------------
def test_create_immediate_job(db):
    res = _create(db, type="immediate")
    assert res["ok"] is True
    assert res["job"]["type"] == "immediate"
    assert res["job"]["status"] == JOB_SCHEDULED
    assert res["next_run"]["status"] == RUN_PENDING


def test_create_one_time_job(db):
    res = _create(db, type="one_time", time="2026-06-10T09:00:00", timezone="UTC")
    assert res["job"]["type"] == "one_time"
    assert res["next_run"]["scheduled_at"].startswith("2026-06-10T09:00:00")


def test_create_recurring_job(db):
    res = _create(
        db, type="recurring", schedule="0 8 * * *", timezone="America/Vancouver"
    )
    assert res["job"]["schedule"] == "0 8 * * *"
    # next_run rendered in the job timezone with an offset.
    assert res["next_run"]["scheduled_at"].endswith(("-07:00", "-08:00"))


def test_create_recurring_without_schedule_is_rejected(db):
    with pytest.raises(AppError) as exc:
        _create(db, type="recurring", timezone="UTC")
    assert exc.value.code == VALIDATION_ERROR
    assert exc.value.field == "job_params.schedule"


def test_create_recurring_without_timezone_is_rejected(db):
    with pytest.raises(AppError) as exc:
        _create(db, type="recurring", schedule="0 8 * * *")
    assert exc.value.code == VALIDATION_ERROR
    assert exc.value.field == "job_params.timezone"


def test_create_one_time_without_time_is_rejected(db):
    with pytest.raises(AppError) as exc:
        _create(db, type="one_time", timezone="UTC")
    assert exc.value.code == VALIDATION_ERROR
    assert exc.value.field == "job_params.time"


def test_create_immediate_with_schedule_is_rejected(db):
    with pytest.raises(AppError) as exc:
        _create(db, type="immediate", schedule="0 8 * * *")
    assert exc.value.code == VALIDATION_ERROR


def test_create_unsupported_action(db):
    with pytest.raises(AppError) as exc:
        create_job(
            db,
            CreateJobRequest(
                user_id=USER, action="launch_rocket", job_params={"type": "immediate"}
            ),
        )
    assert exc.value.code == UNSUPPORTED_ACTION


# --- List / Get -----------------------------------------------------------
def test_list_jobs_by_user(db):
    _create(db, type="immediate")
    other = CreateJobRequest(
        user_id="other", action="review_pr", job_params={"type": "immediate"}
    )
    create_job(db, other)

    res = list_jobs(db, ListJobsParams(user_id=USER))
    assert res["pagination"]["total"] == 1
    assert res["jobs"][0]["user_id"] == USER


def test_list_filters_by_status(db):
    created = _create(db, type="immediate")
    delete_job(db, job_id=created["job"]["job_id"], user_id=USER)

    # Deleted job hidden by default...
    assert list_jobs(db, ListJobsParams(user_id=USER))["pagination"]["total"] == 0
    # ...but visible when explicitly filtered.
    res = list_jobs(db, ListJobsParams(user_id=USER, status=JOB_DELETED))
    assert res["pagination"]["total"] == 1


def test_get_job_detail_includes_runs(db):
    created = _create(db, type="immediate")
    res = get_job(db, job_id=created["job"]["job_id"], user_id=USER)
    assert res["job"]["job_id"] == created["job"]["job_id"]
    assert len(res["runs"]) == 1


def test_get_missing_job_is_not_found(db):
    with pytest.raises(AppError) as exc:
        get_job(db, job_id="job_missing", user_id=USER)
    assert exc.value.code == NOT_FOUND


def test_get_other_users_job_is_permission_denied(db):
    created = _create(db, type="immediate")
    with pytest.raises(AppError) as exc:
        get_job(db, job_id=created["job"]["job_id"], user_id="intruder")
    assert exc.value.code == PERMISSION_DENIED


# --- Modify ---------------------------------------------------------------
def test_modify_recurring_schedule_replaces_run(db):
    created = _create(db, type="recurring", schedule="0 8 * * *", timezone="UTC")
    old_run_id = created["next_run"]["run_id"]

    res = modify_job(
        db,
        job_id=created["job"]["job_id"],
        req=ModifyJobRequest(user_id=USER, job_params={"schedule": "30 8 * * *"}),
    )
    assert res["job"]["schedule"] == "30 8 * * *"
    assert res["cancelled_run_count"] == 1
    assert res["next_run"]["run_id"] != old_run_id

    old_run = db.get(JobRun, old_run_id)
    assert old_run.status == RUN_CANCELLED


def test_modify_one_time_time(db):
    created = _create(db, type="one_time", time="2026-06-10T09:00:00", timezone="UTC")
    res = modify_job(
        db,
        job_id=created["job"]["job_id"],
        req=ModifyJobRequest(user_id=USER, job_params={"time": "2026-06-11T09:00:00"}),
    )
    assert res["cancelled_run_count"] == 1
    assert res["next_run"]["scheduled_at"].startswith("2026-06-11T09:00:00")


def test_modify_pause_cancels_runs_and_creates_none(db):
    created = _create(db, type="recurring", schedule="0 8 * * *", timezone="UTC")
    res = modify_job(
        db,
        job_id=created["job"]["job_id"],
        req=ModifyJobRequest(user_id=USER, status="paused"),
    )
    assert res["job"]["status"] == JOB_PAUSED
    assert res["cancelled_run_count"] == 1
    assert res["next_run"] is None


def test_modify_deleted_job_is_conflict(db):
    created = _create(db, type="immediate")
    delete_job(db, job_id=created["job"]["job_id"], user_id=USER)
    with pytest.raises(AppError) as exc:
        modify_job(
            db,
            job_id=created["job"]["job_id"],
            req=ModifyJobRequest(user_id=USER, action="send_email"),
        )
    assert exc.value.code == CONFLICT


# --- Delete ---------------------------------------------------------------
def test_delete_cancels_pending_runs(db):
    created = _create(db, type="recurring", schedule="0 8 * * *", timezone="UTC")
    res = delete_job(db, job_id=created["job"]["job_id"], user_id=USER)
    assert res["status"] == JOB_DELETED
    assert res["cancelled_run_count"] == 1
    job = db.get(Job, created["job"]["job_id"])
    assert job.status == JOB_DELETED


def test_delete_is_idempotent(db):
    created = _create(db, type="immediate")
    job_id = created["job"]["job_id"]
    first = delete_job(db, job_id=job_id, user_id=USER)
    second = delete_job(db, job_id=job_id, user_id=USER)
    assert first["status"] == second["status"] == JOB_DELETED
    assert second["cancelled_run_count"] == 0
