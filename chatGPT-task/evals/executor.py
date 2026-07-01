"""Isolated execution of scheduler tool calls for evals (spec 07).

The eval harness (not the agent) owns the environment: each case runs against a
fresh in-memory SQLite DB with a pinned clock, exactly like the ``isolated_db``
test fixture. Tool calls are routed through the real MCP ``dispatch`` registry
so evals exercise the same handlers real clients use — no Claude Desktop, no
stdio server.

The scheduler DB is the *ground truth* for grading: a case passes only if the
scheduler actually recorded the intended job/run/trace, not merely because the
model claimed success.
"""

from __future__ import annotations

import copy
import importlib
from datetime import datetime, timezone
from typing import Any

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.core import clock
from app.core.database import Base
from app.mcp.registry import dispatch
from app.mcp.server import _normalize_action

# Modules that captured ``SessionLocal`` / ``engine`` at import time and must be
# repointed at the per-run eval engine (mirrors tests/conftest.py).
_SESSION_LOCAL_MODULES = (
    "app.core.database",
    "app.api.deps",
    "app.mcp.registry",
    "app.scheduler.watcher",
    "app.scheduler.worker",
)
_ENGINE_MODULES = ("app.core.database", "app.api.server")

# Public tool name -> internal registry key.
PUBLIC_TO_REGISTRY = {
    "task_create_v1": "task.create@v1",
    "task_list_v1": "task.list@v1",
    "task_get_v1": "task.get@v1",
    "task_modify_v1": "task.modify@v1",
    "task_delete_v1": "task.delete@v1",
    "task_trace_get_v1": "task.trace.get@v1",
    "task_runs_list_v1": "task.runs.list@v1",
}


class EvalEnv:
    """A fresh, isolated scheduler environment for one eval case."""

    def __init__(self) -> None:
        self.engine = create_engine(
            "sqlite://",
            connect_args={"check_same_thread": False},
            poolclass=StaticPool,
        )
        Base.metadata.create_all(bind=self.engine)
        self.Session = sessionmaker(bind=self.engine, expire_on_commit=False)
        # Snapshot originals so dispose() leaves global module state untouched
        # (keeps eval runs side-effect-free inside the pytest process).
        self._saved: list[tuple[object, str, object]] = []
        for name in _SESSION_LOCAL_MODULES:
            module = importlib.import_module(name)
            if hasattr(module, "SessionLocal"):
                self._saved.append((module, "SessionLocal", module.SessionLocal))
                setattr(module, "SessionLocal", self.Session)
        for name in _ENGINE_MODULES:
            module = importlib.import_module(name)
            if hasattr(module, "engine"):
                self._saved.append((module, "engine", module.engine))
                setattr(module, "engine", self.engine)

    def dispose(self) -> None:
        for module, attr, original in self._saved:
            setattr(module, attr, original)
        self._saved = []
        self.engine.dispose()


def pin_clock(now_iso: str) -> datetime:
    """Pin the deterministic clock to a case's ``now`` (ISO, may be tz-aware)."""
    dt = datetime.fromisoformat(now_iso)
    if dt.tzinfo is not None:
        dt = dt.astimezone(timezone.utc).replace(tzinfo=None)
    clock.set_now(dt)
    return dt


def unpin_clock() -> None:
    clock.reset()


def _shape_args(tool: str, arguments: dict) -> dict:
    """Map public-tool arguments onto the internal dispatch payload."""
    a = arguments
    if tool == "task_create_v1":
        return {
            "user_id": a.get("user_id"),
            "action": _normalize_action(a["action"]) if a.get("action") else a.get("action"),
            "job_params": {
                "type": a.get("type"),
                "time": a.get("time"),
                "schedule": a.get("schedule"),
                "timezone": a.get("timezone"),
            },
            "action_params": a.get("action_params"),
        }
    if tool == "task_modify_v1":
        job_params = {
            k: a[k] for k in ("type", "time", "schedule", "timezone") if a.get(k) is not None
        }
        action = a.get("action")
        return {
            "user_id": a.get("user_id"),
            "job_id": a.get("job_id"),
            "action": _normalize_action(action) if action else None,
            "status": a.get("status"),
            "job_params": job_params or None,
            "action_params": a.get("action_params"),
        }
    if tool == "task_list_v1":
        return {
            "user_id": a.get("user_id"),
            "status": a.get("status"),
            "start_time": a.get("start_time"),
            "end_time": a.get("end_time"),
            "page_size": a.get("page_size", 20),
            "page": a.get("page", 1),
        }
    if tool == "task_runs_list_v1":
        return {
            "user_id": a.get("user_id"),
            "job_id": a.get("job_id"),
            "status": a.get("status"),
            "page_size": a.get("page_size", 20),
            "page": a.get("page", 1),
        }
    if tool == "task_get_v1":
        return {"user_id": a.get("user_id"), "job_id": a.get("job_id")}
    if tool == "task_delete_v1":
        return {"user_id": a.get("user_id"), "job_id": a.get("job_id")}
    if tool == "task_trace_get_v1":
        return {"user_id": a.get("user_id"), "trace_id": a.get("trace_id")}
    raise ValueError(f"Unknown eval tool {tool!r}")


def call_public_tool(tool: str, arguments: dict) -> dict:
    """Execute one public tool call through the real MCP dispatch registry."""
    key = PUBLIC_TO_REGISTRY.get(tool)
    if key is None:
        return {"ok": False, "error": {"code": "VALIDATION_ERROR",
                                       "message": f"Unknown tool {tool!r}.", "field": "tool"}}
    return dispatch(key, _shape_args(tool, arguments))


def run_pending_run(env: EvalEnv, run_id: str) -> str | None:
    """Execute a pending run so a spec04 scheduler trace exists; return trace_id."""
    from app.jobs.traces import latest_trace_for_run
    from app.scheduler.worker import process_run

    session = env.Session()
    try:
        process_run(session, run_id)
        trace = latest_trace_for_run(session, run_id)
        return trace.trace_id if trace is not None else None
    finally:
        session.close()


def run_seed(env: EvalEnv, seed: list[dict], user_id: str) -> dict[str, str]:
    """Run oracle setup calls before the graded turn; return exported vars.

    Auto-captures ``job_id`` / ``run_id`` / ``trace_id``. A step with
    ``"run": true`` also executes the created run to produce a scheduler trace.
    """
    vars: dict[str, str] = {}
    for step in seed:
        args = dict(step.get("arguments", {}))
        args.setdefault("user_id", user_id)
        result = call_public_tool(step["tool"], args)
        job = result.get("job") if isinstance(result, dict) else None
        if job and job.get("job_id"):
            vars["job_id"] = job["job_id"]
        next_run = result.get("next_run") if isinstance(result, dict) else None
        if next_run and next_run.get("run_id"):
            vars["run_id"] = next_run["run_id"]
            if step.get("run"):
                trace_id = run_pending_run(env, next_run["run_id"])
                if trace_id:
                    vars["trace_id"] = trace_id
    return vars


def substitute(value: Any, vars: dict[str, str]) -> Any:
    """Recursively replace ``{{var}}`` placeholders using seed-exported vars."""
    if isinstance(value, str):
        out = value
        for name, replacement in vars.items():
            out = out.replace("{{" + name + "}}", str(replacement))
        return out
    if isinstance(value, dict):
        return {k: substitute(v, vars) for k, v in value.items()}
    if isinstance(value, list):
        return [substitute(v, vars) for v in value]
    return copy.copy(value)
