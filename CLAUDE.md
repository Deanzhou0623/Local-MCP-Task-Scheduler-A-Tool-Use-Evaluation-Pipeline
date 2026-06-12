# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this repo is

A local **real MCP task scheduler** used as a controlled environment for studying LLM tool use. It is built spec-by-spec: `docs/spec01_real_mcp_scheduler_core.md` (scheduler core), `docs/spec02_mcp_inspector_and_claude_desktop_testing.md` (MCP Inspector / Claude Desktop testing), and `docs/spec04_traceable_action_execution.md` (executor registry + action traces) are **implemented**; `docs/spec03,05..08_*.md` are the forward roadmap (tighter schemas/error contract, production hardening, run history + recurring jobs, the evals pipeline, results analysis). Note: Spec 04 is *traceable action execution*, not real external-API integration — execution stays mock (e.g. `send_email` is a `SendEmailMockExecutor` that validates/renders/mock-sends, never a real email); it adds an executor interface and execution traces over simple/mock actions so behavior is inspectable. Real external calls are out of scope for the current roadmap. `docs/Q&A.md` records the design rationale (time-bucket partitioning, `namespace.verb@version` tool naming, registry-vs-if-else dispatch) — read it before changing those decisions.

All code lives under `chatGPT-task/`. The top-level `README.md` is planning-stage and partly describes an earlier, simpler 4-tool design; `chatGPT-task/README.md` and `docs/spec01` are the source of truth for the current implementation.

## Commands

The virtualenv is `chatGPT-task/.venv` (Python 3.11 — the code uses 3.11-only stdlib like `datetime.UTC`). Run everything from inside `chatGPT-task/`.

```bash
cd chatGPT-task
source .venv/bin/activate           # or call .venv/bin/<tool> directly

pytest                              # full suite
pytest tests/test_services.py       # one file
pytest tests/test_services.py::test_delete_is_idempotent   # one test
pytest -k modify                    # by keyword

python -m app.mcp.server            # MCP server (also starts watcher+worker threads)
uvicorn app.api:app --reload        # REST API on /v1/jobs
npx @modelcontextprotocol/inspector python -m app.mcp.server   # manual tool inspection
```

To add a dependency, update `chatGPT-task/requirements.txt` and `pip install` it into `.venv`.

## Architecture

The code is organized into packages under `app/`: `app/core/` (database, errors, ids, time utilities, deterministic clock), `app/jobs/` (models, schemas, service logic, action allow-list), `app/scheduler/` (watcher, worker, queue), `app/mcp/` (registry + FastMCP server), `app/api/` (FastAPI app, routes, deps).

The central rule: **`app/jobs/service.py` is the single source of truth.** Two surfaces call into it and must stay behavior-identical:

- `app/api/routes.py` — FastAPI REST routes (`POST/GET/PATCH/DELETE /v1/jobs`); `app/api/server.py` builds the app (importable as `app.api:app`).
- `app/mcp/registry.py` — the internal `TOOL_REGISTRY` and `dispatch()` contract. `app/mcp/server.py` is a thin FastMCP layer that exposes Claude-compatible names like `task_create_v1`, builds an `args` dict, and routes through `dispatch()`.

When changing a flow, edit the service function once; do not fork logic into a route or a tool handler.

Data model (`app/jobs/models.py`): two tables — `jobs` (durable definition: type `immediate|one_time|recurring`, optional `time`/`schedule`, `timezone`, status) and `job_runs` (each execution attempt, with `scheduled_bucket` and run status). A job has many runs.

Execution pipeline (`app/scheduler/`): `watcher_loop` (in `watcher.py`) scans only the current + previous hourly `scheduled_bucket` for due `pending` runs (time-bucket partitioning — never a full `WHERE scheduled_at <= now` scan), marks them `queued`, and pushes ids onto an in-memory `queue.Queue` (`queue.py`). `worker_loop` (in `worker.py`) calls `process_run`, which executes the placeholder action, updates the parent job, and for recurring jobs creates the next pending run. `process_run` is deliberately factored out so it can be unit-tested synchronously without threads.

## Conventions that span files

- **All datetimes are stored naive UTC.** Presentation differs by field and is handled only in `app/core/timeutils.py`: job `created_at`/`updated_at` render as `...Z`; run `scheduled_at` and a one-time job's `time` render in the job's IANA timezone with an offset (e.g. `2026-06-10T08:00:00-07:00`). Recurring next-run math evaluates cron in the job's local timezone, then converts back to UTC.
- **One error contract.** Service code raises `AppError(code, message, field=?, expected=?)` from `app/core/errors.py`; both surfaces convert it to `{"ok": false, "error": {...}}`. Codes: `VALIDATION_ERROR`, `NOT_FOUND`, `PERMISSION_DENIED`, `CONFLICT`, `UNSUPPORTED_ACTION`, `INTERNAL_ERROR`. Add new failure modes as an `AppError`, not an ad-hoc dict.
- **Pydantic models use `extra="forbid"`** (`app/jobs/schemas.py`) — unknown fields are a `VALIDATION_ERROR`, matching the spec's "extra fields are rejected".
- **`action` must be in the allow-list** in `app/jobs/actions.py`, else `UNSUPPORTED_ACTION`. Execution is a placeholder; Spec 04 (traceable action execution) adds an executor interface over simple/mock actions — real external calls are out of scope.
- **Public MCP tool names must be Claude-compatible** (e.g. `task_create_v1`). Claude Desktop rejects names containing `.` or `@`, so `app/mcp/server.py` exposes safe names while dispatching through the internal registry keys.
- **MCP stdio uses stdout for protocol**, so `app/mcp/server.py` must never `print()` to stdout — diagnostics go to stderr.

## Testing notes

Tests isolate the database with an in-memory SQLite engine. Because several modules capture `SessionLocal`/`engine` at import time, the `isolated_db` fixture in `tests/conftest.py` monkeypatches those names in **every** module that imported them — `SessionLocal` in `app.core.database`, `app.api.deps`, `app.mcp.registry`, `app.scheduler.watcher`, `app.scheduler.worker`, and `engine` in `app.core.database`, `app.api.server`. Replicate that pattern (add the module to the relevant tuple in conftest) if a new module opens its own session. Call services directly with the `db` fixture; use the `client` fixture (FastAPI `TestClient` with a `get_session` dependency override) for REST tests. The MCP protocol surface is tested in `tests/test_mcp_server.py` by driving the real server over an in-memory MCP client session.
