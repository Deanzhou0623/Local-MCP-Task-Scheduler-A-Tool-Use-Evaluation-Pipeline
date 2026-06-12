# ChatGPT Task — Real MCP Task Scheduler (Spec 01)

A local MCP task scheduler that lets a user or LLM client manage scheduled jobs
through stable, versioned tools backed by a FastAPI service and durable SQLite
storage. This implements [Spec 01](../docs/spec01_real_mcp_scheduler_core.md):
the four core flows are **create**, **view**, **modify**, and **delete** a job.

## What it does

- Exposes MCP tools (`task_create_v1`, `task_list_v1`, `task_get_v1`,
  `task_modify_v1`, `task_delete_v1`) that route through a dictionary registry.
- Mirrors those tools as a REST API under `/v1/jobs` (FastAPI + Pydantic).
- Supports **immediate**, **one_time**, and **recurring** (cron) jobs with IANA
  timezones.
- Persists job definitions in `jobs` and execution attempts in `job_runs`
  (SQLAlchemy 2.0).
- Runs a background **watcher** that scans for due runs by hourly UTC time
  bucket, pushes them onto an in-memory queue, and a **worker** that executes
  each run and (for recurring jobs) schedules the next one.
- Returns one structured error envelope for every failure across both surfaces.

```txt
MCP client / REST client
        |
   tool registry  /  FastAPI routes
        |
   service layer (create/list/get/modify/delete)
        |
   SQLite: jobs + job_runs
        ^
        |
watcher (due runs by time bucket) -> in-memory queue -> worker -> next run
```

## Layout

| Path | Responsibility |
| --- | --- |
| `app/core/` | database, errors, ids, time utilities, deterministic clock |
| `app/jobs/` | job models, schemas, service logic, action allow-list |
| `app/scheduler/` | watcher, worker, queue, and testable scheduler helpers |
| `app/mcp/` | `TOOL_REGISTRY`, `dispatch()`, and FastMCP server wiring |
| `app/api/` | FastAPI app, routes, exception handlers, dependencies |

## Setup

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

Copy `.env.example` to `.env` to override `DATABASE_URL` / `WATCHER_INTERVAL`.

## Run

MCP server (starts the watcher + worker threads):

```bash
python -m app.mcp.server
```

REST API:

```bash
uvicorn app.api:app --reload
```

Inspect the MCP tools manually:

```bash
npx @modelcontextprotocol/inspector python -m app.mcp.server
```

## Tools

| Tool | API endpoint | Purpose |
| --- | --- | --- |
| `task_create_v1` | `POST /v1/jobs` | Create a job and its first run |
| `task_list_v1` | `GET /v1/jobs` | List a user's jobs |
| `task_get_v1` | `GET /v1/jobs/{job_id}` | View one job and recent runs |
| `task_modify_v1` | `PATCH /v1/jobs/{job_id}` | Modify a job, recompute runs |
| `task_delete_v1` | `DELETE /v1/jobs/{job_id}` | Soft-delete a job |

Every request requires `user_id` to enforce the ownership boundary. Example
create arguments:

```json
{
  "user_id": "user_123",
  "action": "summarize_financial_news",
  "job_params": { "type": "recurring", "schedule": "0 8 * * *", "timezone": "America/Vancouver" }
}
```

If a `one_time` or `recurring` job omits `job_params.timezone`, `task_create_v1`
returns a structured validation error. The scheduler does not infer a timezone;
the LLM or eval harness must provide an explicit IANA timezone. Immediate jobs
may omit timezone.

## Error contract

```json
{ "ok": false, "error": { "code": "VALIDATION_ERROR", "message": "...", "field": "job_params.schedule", "expected": "cron expression" } }
```

Codes: `VALIDATION_ERROR`, `NOT_FOUND`, `PERMISSION_DENIED`, `CONFLICT`,
`UNSUPPORTED_ACTION`, `INTERNAL_ERROR`.

## Tests

```bash
pytest
```

## MCP stdio caveat

MCP stdio uses **stdout** for protocol messages, so `app/mcp/server.py` never
prints to stdout — diagnostics go to stderr. Stray stdout output corrupts the
protocol stream.
