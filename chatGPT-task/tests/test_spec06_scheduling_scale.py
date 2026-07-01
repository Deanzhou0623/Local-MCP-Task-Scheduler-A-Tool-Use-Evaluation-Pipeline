"""Spec 06: sharded scheduling, run history, and recurring metadata."""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import create_engine

from app.core.database import init_database
from app.core.timeutils import bucket_hour
from app.jobs.models import (
    RUN_PENDING,
    RUN_SUCCEEDED,
    TRIGGER_IMMEDIATE,
    TRIGGER_SCHEDULED,
    JobRun,
)
from app.jobs.schemas import CreateJobRequest
from app.jobs.service import create_job, list_runs
from app.jobs.schemas import ListRunsParams
from app.mcp import dispatch
from app.scheduler import process_run
from app.scheduler.watcher import find_due_runs_for_shard, find_overdue_pending_runs

USER = "spec06-user"


def _create(db, **job_params):
    return create_job(
        db,
        CreateJobRequest(user_id=USER, action="review_pr", job_params=job_params),
    )


def test_new_run_has_sharded_bucket_and_attempt_metadata(db):
    created = _create(db, type="immediate")
    run = db.get(JobRun, created["next_run"]["run_id"])

    assert run.scheduled_bucket_hour == bucket_hour(run.scheduled_at)
    assert run.scheduled_bucket.startswith(f"{run.scheduled_bucket_hour}#S")
    assert run.scheduled_bucket_shard is not None
    assert run.attempt_group_id.startswith("attemptgrp_")
    assert run.attempt_number == 1
    assert run.trigger_reason == TRIGGER_IMMEDIATE


def test_find_due_runs_for_one_shard(db):
    created = _create(db, type="immediate")
    run = db.get(JobRun, created["next_run"]["run_id"])
    run.scheduled_at = datetime(2026, 6, 10, 8, 20)
    run.scheduled_bucket_hour = bucket_hour(run.scheduled_at)
    run.scheduled_bucket = f"{run.scheduled_bucket_hour}#S{run.scheduled_bucket_shard:03d}"
    db.commit()

    found = find_due_runs_for_shard(
        datetime(2026, 6, 10, 8, 30),
        db,
        bucket_dt=run.scheduled_at,
        shard=run.scheduled_bucket_shard,
    )

    assert [r.run_id for r in found] == [run.run_id]


def test_overdue_pending_sweep_finds_cold_bucket(db):
    created = _create(db, type="immediate")
    run = db.get(JobRun, created["next_run"]["run_id"])
    run.scheduled_at = datetime(2026, 6, 10, 5, 0)
    run.scheduled_bucket_hour = bucket_hour(run.scheduled_at)
    run.scheduled_bucket = f"{run.scheduled_bucket_hour}#S{run.scheduled_bucket_shard:03d}"
    db.commit()

    found = find_overdue_pending_runs(datetime(2026, 6, 10, 8, 30), db)

    assert [r.run_id for r in found] == [run.run_id]


def test_recurring_next_run_has_fresh_attempt_group_and_sharded_bucket(db):
    created = _create(
        db,
        type="recurring",
        schedule="* * * * *",
        timezone="UTC",
    )
    first = db.get(JobRun, created["next_run"]["run_id"])
    process_run(db, first.run_id)

    pending = (
        db.query(JobRun)
        .filter(JobRun.job_id == created["job"]["job_id"], JobRun.status == RUN_PENDING)
        .one()
    )
    assert pending.trigger_reason == TRIGGER_SCHEDULED
    assert pending.attempt_number == 1
    assert pending.attempt_group_id != first.attempt_group_id
    assert pending.scheduled_bucket.startswith(f"{pending.scheduled_bucket_hour}#S")


def test_list_runs_returns_paginated_trace_summaries_and_metrics(db):
    created = _create(db, type="immediate")
    run_id = created["next_run"]["run_id"]
    process_run(db, run_id)

    body = list_runs(
        db,
        ListRunsParams(user_id=USER, job_id=created["job"]["job_id"], page=1),
    )

    assert body["ok"] is True
    assert body["pagination"]["total"] == 1
    item = body["runs"][0]
    assert item["run_id"] == run_id
    assert item["status"] == RUN_SUCCEEDED
    assert item["trace"]["trace_id"].startswith("trace_")
    assert item["attempt_number"] == 1
    assert item["queue_delay_seconds"] is not None
    assert item["execution_seconds"] is not None
    assert item["lateness_seconds"] is not None


def test_rest_run_history(client):
    created = client.post(
        "/v1/jobs",
        json={
            "user_id": USER,
            "action": "review_pr",
            "job_params": {"type": "immediate"},
        },
    ).json()

    body = client.get(
        f"/v1/jobs/{created['job']['job_id']}/runs", params={"user_id": USER}
    ).json()

    assert body["ok"] is True
    assert body["pagination"]["total"] == 1
    assert body["runs"][0]["scheduled_bucket"].startswith(
        f"{body['runs'][0]['scheduled_bucket_hour']}#S"
    )


def test_mcp_run_history_dispatch(isolated_db):
    created = dispatch(
        "task.create@v1",
        {"user_id": USER, "action": "review_pr", "job_params": {"type": "immediate"}},
    )
    body = dispatch(
        "task.runs.list@v1",
        {"user_id": USER, "job_id": created["job"]["job_id"]},
    )

    assert body["ok"] is True
    assert body["pagination"]["total"] == 1


def test_sqlite_startup_migrates_old_job_runs_table(tmp_path):
    db_path = tmp_path / "old.db"
    engine = create_engine(
        f"sqlite:///{db_path}",
        connect_args={"check_same_thread": False},
    )
    with engine.begin() as conn:
        conn.exec_driver_sql(
            """
            CREATE TABLE job_runs (
                run_id VARCHAR(40) NOT NULL PRIMARY KEY,
                job_id VARCHAR(40) NOT NULL,
                user_id VARCHAR(64) NOT NULL,
                scheduled_at DATETIME NOT NULL,
                scheduled_bucket VARCHAR(13) NOT NULL,
                started_at DATETIME,
                finished_at DATETIME,
                status VARCHAR(16) NOT NULL,
                retry_count INTEGER NOT NULL,
                error_message TEXT,
                created_at DATETIME NOT NULL,
                updated_at DATETIME NOT NULL
            )
            """
        )
        conn.exec_driver_sql(
            """
            INSERT INTO job_runs (
                run_id, job_id, user_id, scheduled_at, scheduled_bucket,
                status, retry_count, created_at, updated_at
            )
            VALUES (
                'run_old', 'job_old', 'u1', '2026-06-10 23:30:00',
                '2026-06-10T23', 'pending', 0,
                '2026-06-10 23:00:00', '2026-06-10 23:00:00'
            )
            """
        )

    init_database(engine)

    with engine.connect() as conn:
        columns = {
            row[1] for row in conn.exec_driver_sql("PRAGMA table_info(job_runs)")
        }
        row = conn.exec_driver_sql(
            """
            SELECT scheduled_bucket_hour, scheduled_bucket_shard, scheduled_bucket,
                   attempt_number, trigger_reason, priority
              FROM job_runs
             WHERE run_id = 'run_old'
            """
        ).one()

    assert {
        "scheduled_bucket_hour",
        "scheduled_bucket_shard",
        "attempt_group_id",
        "attempt_number",
        "parent_run_id",
        "trigger_reason",
        "priority",
        "deadline_at",
    }.issubset(columns)
    assert row == ("2026061023", 0, "2026061023#S000", 1, "scheduled", 10)
