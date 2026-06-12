"""Supported task actions (spec 04, section 5).

An ``action`` names the work a job performs. The executor registry in
:mod:`app.jobs.executors` is the single source of truth for which actions are
supported; ``SUPPORTED_ACTIONS`` is derived from it so the allow-list and the
executors can never drift apart. Execution remains mock (no real external
APIs); spec 04 makes it traceable rather than real.

The allow-list backs the ``UNSUPPORTED_ACTION`` error: an LLM that invents an
action name must be rejected, not silently accepted.
"""

from __future__ import annotations

from app.jobs.executors import EXECUTORS

# Derived from the executor registry — add an executor to support an action.
SUPPORTED_ACTIONS: frozenset[str] = frozenset(EXECUTORS)


def is_supported(action: str) -> bool:
    return action in SUPPORTED_ACTIONS
