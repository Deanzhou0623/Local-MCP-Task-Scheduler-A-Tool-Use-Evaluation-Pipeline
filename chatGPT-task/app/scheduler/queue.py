"""The in-memory work queue shared by the watcher and the worker.

This stands in for a production job queue (SQS/Redis/etc.). The watcher *puts*
due run ids; the worker *gets* them. Feature 05 replaces this with a durable
queue, so keeping it isolated here makes that swap a single-file change.
"""

from __future__ import annotations

from queue import Queue

# Run ids the watcher has marked ``queued`` and handed to the worker.
run_queue: "Queue[str]" = Queue()
