"""Scheduler package: watcher + worker over ``job_runs``.

Public helpers are imported lazily to avoid service/worker import cycles.
"""

from __future__ import annotations

import os
import threading

__all__ = [
    "start_scheduler",
    "find_due_runs",
    "hot_buckets",
    "process_run",
    "run_queue",
]


def start_scheduler(interval: int = 10) -> tuple[threading.Thread, ...]:
    """Start daemon watcher + a small durable-queue worker pool."""
    from app.scheduler.watcher import watcher_loop
    from app.scheduler.worker import worker_loop

    worker_count = int(os.getenv("SCHEDULER_WORKER_COUNT", "2"))
    watcher = threading.Thread(
        target=watcher_loop, args=(interval,), daemon=True, name="watcher"
    )
    workers = tuple(
        threading.Thread(
            target=worker_loop,
            kwargs={"worker_id": f"worker_{i}"},
            daemon=True,
            name=f"worker-{i}",
        )
        for i in range(worker_count)
    )
    watcher.start()
    for worker in workers:
        worker.start()
    return (watcher, *workers)


def find_due_runs(*args, **kwargs):
    from app.scheduler.watcher import find_due_runs as _find_due_runs

    return _find_due_runs(*args, **kwargs)


def hot_buckets(*args, **kwargs):
    from app.scheduler.watcher import hot_buckets as _hot_buckets

    return _hot_buckets(*args, **kwargs)


def process_run(*args, **kwargs):
    from app.scheduler.worker import process_run as _process_run

    return _process_run(*args, **kwargs)


def __getattr__(name: str):
    if name == "run_queue":
        from app.scheduler.queue import run_queue

        return run_queue
    raise AttributeError(name)
