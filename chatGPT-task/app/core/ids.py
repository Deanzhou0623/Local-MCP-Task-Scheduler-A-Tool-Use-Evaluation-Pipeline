"""Identifier generation for jobs and runs.

Spec 01 shows opaque, prefixed string IDs (``job_...`` / ``run_...``). The MVP
uses a uuid4 hex suffix; the prefix keeps IDs self-describing in logs and tool
traces, which matters when the eval pipeline reads back arguments and results.
"""

from __future__ import annotations

import uuid


def new_job_id() -> str:
    return f"job_{uuid.uuid4().hex}"


def new_run_id() -> str:
    return f"run_{uuid.uuid4().hex}"


def new_trace_id() -> str:
    return f"trace_{uuid.uuid4().hex}"


def new_trace_event_id() -> str:
    return f"evt_{uuid.uuid4().hex}"
