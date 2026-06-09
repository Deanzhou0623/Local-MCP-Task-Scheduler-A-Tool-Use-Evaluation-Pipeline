"""Deterministic clock for reproducible time-based evals.

These tests prove two things the eval harness relies on:

1. Pinning ``now`` (via the ``frozen_clock`` fixture) freezes every clock the
   scheduler reads — immediate run times, recurring next-run math, and model
   timestamp defaults — so a run is reproducible.
2. A grader can derive the *expected* timestamp for a relative request
   ("tomorrow at 8:00 local") purely from the pinned ``now`` and compare it to
   what the model put in ``job_params.time``.

Note the division of labor: computing "tomorrow" is the LLM's job (graded), and
``expected_tomorrow_local`` here stands in for the dataset's golden-answer
generator — it lives in the test, not in the scheduler. The scheduler stays
dumb; it only schedules the instant it is handed.
"""

from __future__ import annotations

from datetime import datetime, time, timedelta, timezone
from zoneinfo import ZoneInfo

from app.jobs.models import JobRun
from app.jobs.schemas import CreateJobRequest
from app.jobs.service import create_job

UTC = timezone.utc
USER = "user_123"
TZ = "America/Vancouver"


def _create(db, **job_params):
    req = CreateJobRequest(user_id=USER, action="summarize_financial_news",
                           job_params=job_params)
    return create_job(db, req)


def expected_tomorrow_local(now_utc: datetime, tz_name: str, hour: int = 8) -> datetime:
    """Golden-answer generator: 'tomorrow at HH:00 local', as naive local time.

    This is grader-side logic (what the dataset expects), not scheduler logic.
    """
    today_local = now_utc.replace(tzinfo=UTC).astimezone(ZoneInfo(tz_name)).date()
    return datetime.combine(today_local + timedelta(days=1), time(hour, 0))


# --- The scheduler's clock is pinned --------------------------------------
def test_immediate_run_lands_on_pinned_now(db, frozen_clock):
    res = _create(db, type="immediate")
    run = db.get(JobRun, res["next_run"]["run_id"])
    # Stored as naive UTC, exactly the frozen instant.
    assert run.scheduled_at == frozen_clock.now  # 2026-06-09T12:00:00


def test_model_timestamps_are_pinned(db, frozen_clock):
    res = _create(db, type="immediate")
    assert res["job"]["created_at"] == "2026-06-09T12:00:00Z"
    assert res["job"]["updated_at"] == "2026-06-09T12:00:00Z"


def test_recurring_next_run_is_deterministic(db, frozen_clock):
    # now = 05:00 local; next 08:00 local fire is the same day.
    res = _create(db, type="recurring", schedule="0 8 * * *", timezone=TZ)
    run = db.get(JobRun, res["next_run"]["run_id"])
    assert run.scheduled_at == datetime(2026, 6, 9, 15, 0)  # 08:00 PDT in UTC
    assert res["next_run"]["scheduled_at"] == "2026-06-09T08:00:00-07:00"


# --- A relative request is gradeable against the pinned clock -------------
def test_tomorrow_at_8_is_graded_against_pinned_now(db, frozen_clock):
    # What the grader expects the model to compute from the injected "now".
    golden = expected_tomorrow_local(frozen_clock.now, TZ)  # 2026-06-10 08:00 local
    assert golden == datetime(2026, 6, 10, 8, 0)

    # What the (correct) model would send to task.create as job_params.time.
    res = _create(db, type="one_time", time=golden.isoformat(), timezone=TZ)

    # The scheduler faithfully schedules that exact local instant.
    assert res["next_run"]["scheduled_at"] == "2026-06-10T08:00:00-07:00"
    run = db.get(JobRun, res["next_run"]["run_id"])
    assert run.scheduled_at == datetime(2026, 6, 10, 15, 0)  # 08:00 PDT in UTC


def test_repinning_changes_now(db, frozen_clock):
    # The default pin (June) would schedule in June; repinning to December must
    # move the run there. The numeric UTC offset of a future winter date is
    # tz-database-dependent (this host encodes permanent DST for 2026+), so we
    # assert the local wall time + date — which is reproducible — not the offset.
    frozen_clock.set(datetime(2026, 12, 25, 0, 0))
    res = _create(db, type="recurring", schedule="0 8 * * *", timezone=TZ)
    assert res["next_run"]["scheduled_at"].startswith("2026-12-25T08:00:00")
