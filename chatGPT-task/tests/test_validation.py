"""Pydantic, cron, and timezone validation (spec 01, section 14)."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from app.core.errors import VALIDATION_ERROR, AppError
from app.jobs.schemas import CreateJobRequest
from app.core.timeutils import (
    next_recurring_run_utc,
    time_bucket,
    validate_cron,
    validate_timezone,
)


def test_create_request_rejects_extra_fields():
    with pytest.raises(ValidationError):
        CreateJobRequest(
            user_id="u1",
            action="review_pr",
            job_params={"type": "immediate"},
            surprise="nope",
        )


def test_job_params_rejects_extra_fields():
    with pytest.raises(ValidationError):
        CreateJobRequest(
            user_id="u1",
            action="review_pr",
            job_params={"type": "immediate", "bogus": 1},
        )


def test_job_params_rejects_unknown_type():
    with pytest.raises(ValidationError):
        CreateJobRequest(
            user_id="u1", action="review_pr", job_params={"type": "weekly"}
        )


def test_validate_cron_accepts_valid():
    validate_cron("0 8 * * *")  # no raise


def test_validate_cron_rejects_invalid():
    with pytest.raises(AppError) as exc:
        validate_cron("not a cron")
    assert exc.value.code == VALIDATION_ERROR
    assert exc.value.field == "job_params.schedule"


def test_validate_timezone_accepts_iana():
    validate_timezone("America/Vancouver")


def test_validate_timezone_rejects_garbage():
    with pytest.raises(AppError) as exc:
        validate_timezone("Mars/Phobos")
    assert exc.value.code == VALIDATION_ERROR


def test_time_bucket_format():
    from datetime import datetime

    assert time_bucket(datetime(2026, 6, 10, 8, 30)) == "2026061008"
    assert time_bucket(datetime(2026, 6, 10, 8, 30), "run_abc").startswith(
        "2026061008#S"
    )


def test_next_recurring_run_respects_timezone():
    from datetime import datetime

    # 10:00 UTC on 2026-06-09 is 03:00 in Vancouver (PDT, -07:00); the next
    # 08:00-local fire is later the same local day -> 15:00 UTC.
    nxt = next_recurring_run_utc(
        "0 8 * * *", "America/Vancouver", datetime(2026, 6, 9, 10, 0)
    )
    assert nxt == datetime(2026, 6, 9, 15, 0)


def test_next_recurring_run_crosses_spring_dst_boundary():
    from datetime import datetime

    # After 08:00 PST on Mar 7, the next 08:00 Vancouver fire is Mar 8 after
    # the switch to PDT, so UTC moves from 16:00 to 15:00.
    nxt = next_recurring_run_utc(
        "0 8 * * *", "America/Vancouver", datetime(2026, 3, 7, 17, 0)
    )
    assert nxt == datetime(2026, 3, 8, 15, 0)


def test_next_recurring_run_crosses_fall_dst_boundary():
    from datetime import datetime

    # After 08:00 PDT on Oct 31, the next 08:00 Los Angeles fire is Nov 1 after
    # the switch to PST, so UTC moves from 15:00 to 16:00.
    nxt = next_recurring_run_utc(
        "0 8 * * *", "America/Los_Angeles", datetime(2026, 10, 31, 16, 0)
    )
    assert nxt == datetime(2026, 11, 1, 16, 0)
