"""Time, timezone, and cron helpers.

Storage convention: every datetime persisted to the database is **naive UTC**.
Presentation depends on the field:

- Job ``created_at`` / ``updated_at`` render as UTC with a ``Z`` suffix.
- Run ``scheduled_at`` and a one-time job's ``time`` render in the job's own
  IANA timezone with an explicit offset (e.g. ``2026-06-10T08:00:00-07:00``),
  matching the Spec 01 response examples.

``scheduled_bucket`` is the hourly UTC partition key the watcher filters on so
each poll stays bounded by due-time locality instead of total table size.
"""

from __future__ import annotations

from datetime import datetime, timezone
from zoneinfo import ZoneInfo

from croniter import croniter

from app.core.clock import now_utc
from app.core.errors import VALIDATION_ERROR, AppError

UTC = timezone.utc


def utcnow_naive() -> datetime:
    """Current time as a naive UTC datetime, via the injectable clock.

    Routing through :func:`app.core.clock.now_utc` means tests and the eval
    harness can pin ``now`` without touching any call site.
    """
    return now_utc()


def time_bucket(dt_utc_naive: datetime) -> str:
    """Hourly UTC bucket, e.g. ``2026-06-10T08`` (spec 01, section 11)."""
    return dt_utc_naive.strftime("%Y-%m-%dT%H")


def validate_timezone(tz_name: str) -> ZoneInfo:
    """Return the ZoneInfo or raise a structured validation error."""
    try:
        return ZoneInfo(tz_name)
    except Exception as exc:  # noqa: BLE001 - any lookup failure is a bad tz
        raise AppError(
            VALIDATION_ERROR,
            f"Unknown timezone '{tz_name}'.",
            field="job_params.timezone",
            expected="IANA timezone",
        ) from exc


def validate_cron(schedule: str) -> None:
    """Raise a structured validation error if ``schedule`` is not valid cron."""
    if not isinstance(schedule, str) or not croniter.is_valid(schedule):
        raise AppError(
            VALIDATION_ERROR,
            "Recurring jobs require a valid cron schedule.",
            field="job_params.schedule",
            expected="cron expression",
        )


def next_recurring_run_utc(
    schedule: str, tz_name: str, after_utc_naive: datetime
) -> datetime:
    """Next fire time strictly after ``after``, computed in ``tz_name``.

    The cron expression is evaluated in the job's local timezone so that
    ``0 8 * * *`` means 08:00 local across DST boundaries, then the result is
    converted back to naive UTC for storage and bucketing.
    """
    tz = ZoneInfo(tz_name)
    after_local = after_utc_naive.replace(tzinfo=UTC).astimezone(tz)
    nxt_local = croniter(schedule, after_local).get_next(datetime)
    return nxt_local.astimezone(UTC).replace(tzinfo=None, microsecond=0)


def one_time_to_utc(dt: datetime, tz_name: str) -> datetime:
    """Normalize a one-time ``time`` to naive UTC.

    A naive timestamp is interpreted in the job's timezone; an aware timestamp
    keeps its own offset.
    """
    aware = dt.replace(tzinfo=ZoneInfo(tz_name)) if dt.tzinfo is None else dt
    return aware.astimezone(UTC).replace(tzinfo=None, microsecond=0)


def iso_utc(dt_utc_naive: datetime | None) -> str | None:
    """Render naive UTC as ``...Z`` (spec job timestamps)."""
    if dt_utc_naive is None:
        return None
    return dt_utc_naive.replace(microsecond=0).isoformat() + "Z"


def iso_in_tz(dt_utc_naive: datetime | None, tz_name: str) -> str | None:
    """Render naive UTC in the job timezone with an explicit offset."""
    if dt_utc_naive is None:
        return None
    aware = dt_utc_naive.replace(tzinfo=UTC).astimezone(ZoneInfo(tz_name))
    return aware.replace(microsecond=0).isoformat()
