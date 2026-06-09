"""Scheduler package: watcher + worker over ``job_runs``.

Public surface (stable regardless of internal file layout):

- ``start_scheduler`` — launch the background watcher and worker threads.
- ``find_due_runs`` / ``hot_buckets`` — watcher querying (also unit-tested).
- ``process_run`` — execute one run synchronously (also unit-tested).
- ``run_queue`` — the in-memory hand-off queue.
"""

from __future__ import annotations

import threading

from app.scheduler.queue import run_queue
from app.scheduler.watcher import find_due_runs, hot_buckets, watcher_loop
from app.scheduler.worker import process_run, worker_loop

__all__ = [
    "start_scheduler",
    "find_due_runs",
    "hot_buckets",
    "process_run",
    "run_queue",
]


def start_scheduler(interval: int = 10) -> tuple[threading.Thread, threading.Thread]:
    """Start daemon watcher + worker threads."""
    watcher = threading.Thread(
        target=watcher_loop, args=(interval,), daemon=True, name="watcher"
    )
    worker = threading.Thread(target=worker_loop, daemon=True, name="worker")
    watcher.start()
    worker.start()
    return watcher, worker
