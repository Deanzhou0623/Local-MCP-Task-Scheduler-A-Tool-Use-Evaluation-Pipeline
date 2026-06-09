"""Injectable wall clock — the one source of "now" for the whole app.

Production reads the real clock. Tests and the eval harness *pin* ``now`` so
that relative-time requests ("tomorrow at 8:00") resolve to a deterministic
instant and can be graded against a known reference.

This is environment engineering for evals, not scheduler intelligence: the
clock never decides anything, it only makes "now" reproducible. Every ``now``
in the system flows through :func:`app.core.timeutils.utcnow_naive`, which
delegates here, so pinning this one function freezes services, the scheduler,
and model timestamp defaults at once.

The override is a process-global, intended for single-threaded test/eval
contexts (one task at a time). Always pair ``set_now`` with ``reset`` — the
:func:`frozen_at` context manager and the ``frozen_clock`` test fixture do this
for you.
"""

from __future__ import annotations

from contextlib import contextmanager
from datetime import datetime, timezone
from typing import Iterator

UTC = timezone.utc

_frozen: datetime | None = None


def _to_naive_utc(dt: datetime) -> datetime:
    """Normalize any datetime to naive UTC at second precision."""
    if dt.tzinfo is not None:
        dt = dt.astimezone(UTC).replace(tzinfo=None)
    return dt.replace(microsecond=0)


def now_utc() -> datetime:
    """Current instant as naive UTC (seconds precision), honoring any pin."""
    if _frozen is not None:
        return _frozen
    return datetime.now(UTC).replace(tzinfo=None, microsecond=0)


def set_now(dt: datetime) -> None:
    """Pin the clock to a fixed instant. Remember to :func:`reset`."""
    global _frozen
    _frozen = _to_naive_utc(dt)


def reset() -> None:
    """Return to the real wall clock."""
    global _frozen
    _frozen = None


@contextmanager
def frozen_at(dt: datetime) -> Iterator[datetime]:
    """Pin ``now`` to ``dt`` for the duration of the block."""
    set_now(dt)
    try:
        yield now_utc()
    finally:
        reset()
