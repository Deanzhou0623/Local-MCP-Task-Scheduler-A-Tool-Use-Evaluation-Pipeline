"""Spec 05 durable queue, lease, retry, and status behavior."""

from __future__ import annotations

from datetime import timedelta

from app.jobs.models import (
    EVENT_SKIPPED,
    EVENT_SUCCEEDED,
    JOB_SCHEDULED,
    QUEUE_DONE,
    QUEUE_FAILED,
    QUEUE_LEASED,
    QUEUE_READY,
    RUN_FAILED,
    RUN_PENDING,
    RUN_QUEUED,
    RUN_RUNNING,
    RUN_SUCCEEDED,
    TRIGGER_RETRY,
    ActionTrace,
    ActionTraceEvent,
    JobRun,
    JobRunQueue,
)
from app.jobs.schemas import CreateJobRequest
from app.jobs.service import create_job
from app.scheduler.queue import (
    claim_next_queue_item,
    enqueue_run_once,
    recover_expired_leases,
    recover_stranded_queued_runs,
    run_queue,
    scheduler_status,
)
from app.scheduler.watcher import enqueue_due_runs, recovery_tick
from app.scheduler.worker import (
    STALE_RUNNING_SECONDS,
    WORKER_LEASE_SECONDS,
    _drain_one_signal,
    claim_and_process_once,
    process_queue_item,
    recover_stale_running_runs,
)

USER = "queue-user"


def _create(db, action: str = "review_pr", action_params: dict | None = None) -> dict:
    return create_job(
        db,
        CreateJobRequest(
            user_id=USER,
            action=action,
            job_params={"type": "immediate"},
            action_params=action_params,
        ),
    )


def _run(db, created: dict) -> JobRun:
    return db.get(JobRun, created["next_run"]["run_id"])


def test_watcher_persists_due_runs_to_durable_queue(db, frozen_clock):
    created = _create(db)
    run = _run(db, created)

    ids = enqueue_due_runs(db, frozen_clock.now)

    assert ids == [run.run_id]
    db.refresh(run)
    assert run.status == RUN_QUEUED
    item = db.query(JobRunQueue).filter_by(run_id=run.run_id).one()
    assert item.status == QUEUE_READY


def test_enqueue_run_once_is_idempotent(db):
    created = _create(db)
    run = _run(db, created)

    first = enqueue_run_once(db, run)
    second = enqueue_run_once(db, run)
    db.commit()

    assert first.queue_id == second.queue_id
    assert db.query(JobRunQueue).filter_by(run_id=run.run_id).count() == 1


def test_worker_claims_ready_item_by_priority(db, frozen_clock):
    low = _run(db, _create(db))
    high = _run(db, _create(db))
    low.priority = 10
    high.priority = 0
    low.status = high.status = RUN_QUEUED
    enqueue_run_once(db, low, priority=low.priority)
    enqueue_run_once(db, high, priority=high.priority)
    db.commit()

    item = claim_next_queue_item(
        db, worker_id="worker-a", lease_seconds=60, now=frozen_clock.now
    )
    db.commit()

    assert item.run_id == high.run_id
    assert item.status == QUEUE_LEASED
    assert item.locked_by == "worker-a"


def test_expired_lease_is_recovered_when_run_has_not_started(db, frozen_clock):
    run = _run(db, _create(db))
    run.status = RUN_QUEUED
    enqueue_run_once(db, run)
    db.commit()
    item = claim_next_queue_item(
        db, worker_id="worker-a", lease_seconds=60, now=frozen_clock.now
    )
    db.commit()

    recovered = recover_expired_leases(
        db, now=frozen_clock.now + timedelta(seconds=61)
    )
    db.commit()

    db.refresh(item)
    db.refresh(run)
    assert recovered == 1
    assert item.status == QUEUE_READY
    assert item.locked_by is None
    assert run.status == RUN_QUEUED


def test_stale_running_run_fails_and_creates_retry(db, frozen_clock):
    run = _run(db, _create(db))
    run.status = RUN_QUEUED
    enqueue_run_once(db, run)
    db.commit()
    item = claim_next_queue_item(
        db, worker_id="worker-a", lease_seconds=60, now=frozen_clock.now
    )
    run.status = RUN_RUNNING
    run.started_at = frozen_clock.now
    db.commit()

    recovered = recover_stale_running_runs(
        db, now=frozen_clock.now + timedelta(seconds=61), stale_after_seconds=60
    )
    db.commit()

    db.refresh(item)
    retry = db.query(JobRun).filter_by(parent_run_id=run.run_id).one()
    assert recovered == 1
    assert run.status == RUN_FAILED
    assert item.status == QUEUE_FAILED
    assert retry.status == RUN_PENDING
    assert retry.trigger_reason == TRIGGER_RETRY


def test_stranded_queued_run_gets_queue_row(db):
    run = _run(db, _create(db))
    run.status = RUN_QUEUED
    db.commit()

    recovered = recover_stranded_queued_runs(db)
    db.commit()

    assert recovered == 1
    assert db.query(JobRunQueue).filter_by(run_id=run.run_id).one().status == QUEUE_READY


def test_recovery_tick_runs_all_recovery_helpers(db, frozen_clock):
    stranded = _run(db, _create(db))
    stranded.status = RUN_QUEUED

    leased = _run(db, _create(db))
    leased.status = RUN_QUEUED
    enqueue_run_once(db, leased)
    db.commit()
    claim_next_queue_item(
        db, worker_id="worker-a", lease_seconds=60, now=frozen_clock.now
    )
    db.commit()

    recovered = recovery_tick(db, frozen_clock.now + timedelta(seconds=61))

    assert recovered["stranded_queued"] == 1
    assert recovered["expired_leases"] == 1


def test_process_queue_item_completes_successful_run(db, frozen_clock):
    run = _run(db, _create(db))
    run.status = RUN_QUEUED
    enqueue_run_once(db, run)
    db.commit()
    item = claim_next_queue_item(
        db, worker_id="worker-a", lease_seconds=60, now=frozen_clock.now
    )
    db.commit()

    processed = process_queue_item(db, item)

    db.refresh(item)
    assert processed.status == RUN_SUCCEEDED
    assert item.status == QUEUE_DONE


def test_retryable_failure_creates_retry_attempt(db, frozen_clock):
    created = _create(db, "generate_report", {"fail_mode": "retryable"})
    run = _run(db, created)
    run.status = RUN_QUEUED
    enqueue_run_once(db, run)
    db.commit()
    item = claim_next_queue_item(
        db, worker_id="worker-a", lease_seconds=60, now=frozen_clock.now
    )
    db.commit()

    processed = process_queue_item(db, item)

    retry = (
        db.query(JobRun)
        .filter(JobRun.parent_run_id == processed.run_id)
        .one()
    )
    db.refresh(item)
    assert processed.status == RUN_FAILED
    assert item.status == QUEUE_FAILED
    assert retry.status == RUN_PENDING
    assert retry.trigger_reason == TRIGGER_RETRY
    assert retry.attempt_number == 2
    assert retry.scheduled_at > frozen_clock.now
    assert retry.attempt_group_id == processed.attempt_group_id
    assert processed.job.status == JOB_SCHEDULED


def test_checkpoint_retry_skips_completed_expensive_stages(db, frozen_clock):
    created = _create(
        db,
        "generate_report",
        {"pipeline": "checkpoint", "fail_once_at": "send_notification"},
    )
    first = _run(db, created)
    first.status = RUN_QUEUED
    enqueue_run_once(db, first)
    db.commit()
    first_item = claim_next_queue_item(
        db, worker_id="worker-a", lease_seconds=60, now=frozen_clock.now
    )
    process_queue_item(db, first_item)

    retry = db.query(JobRun).filter_by(parent_run_id=first.run_id).one()
    retry.status = RUN_QUEUED
    enqueue_run_once(db, retry)
    db.commit()
    retry_item = claim_next_queue_item(
        db, worker_id="worker-a", lease_seconds=60, now=retry.scheduled_at
    )
    processed_retry = process_queue_item(db, retry_item)

    retry_trace = db.query(ActionTrace).filter_by(run_id=retry.run_id).one()
    events = (
        db.query(ActionTraceEvent)
        .filter_by(trace_id=retry_trace.trace_id)
        .order_by(ActionTraceEvent.sequence)
        .all()
    )
    statuses = {event.stage: event.status for event in events}
    assert processed_retry.status == RUN_SUCCEEDED
    assert statuses["mock_llm_call"] == EVENT_SKIPPED
    assert statuses["render_result"] == EVENT_SKIPPED
    assert statuses["send_notification"] == EVENT_SUCCEEDED


def test_permanent_failure_does_not_retry(db, frozen_clock):
    created = _create(db, "generate_report", {"fail_mode": "permanent"})
    run = _run(db, created)
    run.status = RUN_QUEUED
    enqueue_run_once(db, run)
    db.commit()
    item = claim_next_queue_item(
        db, worker_id="worker-a", lease_seconds=60, now=frozen_clock.now
    )
    db.commit()

    processed = process_queue_item(db, item)

    assert processed.status == RUN_FAILED
    assert db.query(JobRun).filter(JobRun.parent_run_id == processed.run_id).count() == 0
    assert item.status == QUEUE_FAILED


def test_scheduler_status_reports_run_and_queue_counts(client, db):
    run = _run(db, _create(db))
    run.status = RUN_QUEUED
    enqueue_run_once(db, run)
    db.commit()

    body = client.get("/v1/scheduler/status").json()

    assert body["ok"] is True
    assert body["runs"][RUN_QUEUED] == 1
    assert body["queue"][QUEUE_READY] == 1
    assert body["queue"]["expired_leases"] == 0


# --- Recovery-vs-live-worker safety (issues #2/#3) -------------------------
def test_stale_cutoff_exceeds_worker_lease():
    # Recovery must wait longer than a lease so it can't race a live worker.
    assert STALE_RUNNING_SECONDS > WORKER_LEASE_SECONDS


def test_running_run_with_valid_lease_is_not_recovered(db, frozen_clock):
    """A run whose queue lease is still valid must never be failed/retried,
    even if its ``started_at`` looks stale under a small cutoff."""
    run = _run(db, _create(db))
    run.status = RUN_QUEUED
    enqueue_run_once(db, run)
    db.commit()
    # Long, still-valid lease (120s).
    claim_next_queue_item(
        db, worker_id="worker-a", lease_seconds=120, now=frozen_clock.now
    )
    run.status = RUN_RUNNING
    run.started_at = frozen_clock.now
    db.commit()

    # 61s later: stale by a 30s cutoff, but the 120s lease has NOT expired.
    recovered = recover_stale_running_runs(
        db,
        now=frozen_clock.now + timedelta(seconds=61),
        stale_after_seconds=30,
    )
    db.commit()

    db.refresh(run)
    assert recovered == 0
    assert run.status == RUN_RUNNING
    assert db.query(JobRun).filter_by(parent_run_id=run.run_id).count() == 0


# --- Wakeup signal is bounded (issue #1: no in-memory queue leak) ----------
def test_processing_drains_one_wakeup_signal(db, frozen_clock):
    while not run_queue.empty():  # start from a clean signal queue
        run_queue.get_nowait()

    run = _run(db, _create(db))
    run.status = RUN_QUEUED
    enqueue_run_once(db, run)
    db.commit()

    run_queue.put(run.run_id)       # watcher-style wakeup for the real item
    run_queue.put("orphan-signal")  # an extra signal with no matching item
    assert run_queue.qsize() == 2

    processed = claim_and_process_once(db, worker_id="w", lease_seconds=60)
    _drain_one_signal()

    # Exactly one signal consumed per processed item -> queue cannot grow.
    assert processed.status == RUN_SUCCEEDED
    assert run_queue.qsize() == 1

    while not run_queue.empty():  # leave the shared queue clean for other tests
        run_queue.get_nowait()
