# Scaffold Prompt: Real MCP ChatGPT Task Scheduler

Use this prompt with a coding agent to generate the first working scaffold for a real MCP task scheduler.

The generated root folder should be named:

```txt
chatGPT-task/
```

Build a real MCP server first. Do not build a FastAPI API in this scaffold. Do not build the eval pipeline yet. Focus on the basic job execution flow and job review flow.

## Role

You are a senior backend engineer building a small, working MCP prototype.

Create a local Python project that implements a ChatGPT Tasks-inspired scheduler with:

- A real MCP server using the Python MCP SDK and `FastMCP`
- SQLite persistence through SQLAlchemy
- A simple watcher that scans for due jobs
- An in-memory queue that simulates a production queue
- A worker that executes queued jobs
- MCP tools for creating, listing, checking, and cancelling scheduled tasks

Prioritize a runnable prototype over production-scale architecture.

## Product Goal

Build a local MCP server that lets an MCP client, such as Claude Desktop, Claude Code, or an MCP inspector, schedule and review tasks through standardized tool calls.

Example user request:

```txt
Schedule a task to review PR #123 tomorrow at 9am.
```

Expected MCP behavior:

```txt
Model selects task_create -> MCP server creates a job -> returns job_id and status.
```

Then:

```txt
What's the status of that task?
```

Expected MCP behavior:

```txt
Model selects task_status -> MCP server returns job status and result.
```

## Current Scope

Build only these flows:

1. Job execution flow
   - MCP client calls `task_create`.
   - MCP server validates `description` and `scheduled_at`.
   - Server writes a Job record to SQLite.
   - Watcher periodically scans for due pending jobs.
   - Watcher marks due jobs as `queued` and pushes job IDs into an in-memory queue.
   - Worker consumes job IDs from the queue.
   - Worker marks jobs as `running`, executes a placeholder action, then marks jobs as `completed` or `failed`.

2. Job review flow
   - MCP client calls `task_list` to review scheduled jobs.
   - MCP client calls `task_status` to inspect one job.
   - MCP client can call `task_cancel` to cancel a job that has not completed.

Do not implement recurring jobs, cron parsing, JobRun tables, FastAPI routes, auth, external API calls, evals, or distributed infrastructure in this scaffold.

## Reference Design Notes

Use the basic parts of the "Design ChatGPT Tasks" notes:

- ChatGPT or another MCP client connects to an MCP server.
- MCP exposes tools with names, descriptions, and schemas.
- The model selects a tool and fills arguments.
- The MCP server executes the tool handler and returns structured JSON.
- A scheduler stores job metadata.
- A watcher scans the DB for due jobs.
- A worker executes queued jobs.

High-level MVP design:

```txt
MCP Client
    |
    v
FastMCP Server
    |
    v
Tool handlers
    |
    v
SQLite Jobs DB
    ^
    |
Watcher scans due jobs -> in-memory queue -> Worker executes jobs
```

## Project Structure To Generate

Generate this structure:

```txt
chatGPT-task/
|-- README.md
|-- requirements.txt
|-- .env.example
|-- app/
|   |-- __init__.py
|   |-- database.py
|   |-- models.py
|   |-- scheduler.py
|   `-- mcp_server.py
`-- tests/
    |-- __init__.py
    |-- test_models.py
    |-- test_scheduler.py
    `-- test_tool_handlers.py
```

## Dependencies

Use Python 3.11+.

`requirements.txt` should include:

```txt
mcp>=1.0.0
sqlalchemy>=2.0.0
python-dotenv>=1.0.0
pytest>=8.0.0
```

Do not add FastAPI, APScheduler, croniter, pandas, or provider SDKs in this scaffold.

## Data Model

Use one `Job` table for the first scaffold.

File: `app/models.py`

Fields:

```txt
id: integer primary key autoincrement
time_bucket: string, indexed
description: text
scheduled_at: datetime
status: string
result: nullable text
created_at: datetime
updated_at: datetime
```

Status values:

```txt
pending
queued
running
completed
failed
cancelled
```

Indexes:

```txt
idx_bucket_status on time_bucket, status
```

Implementation notes:

- Store datetimes as naive UTC in SQLite for simplicity.
- Add a small `_utcnow()` helper.
- Use `datetime.now(UTC).replace(tzinfo=None)` instead of deprecated `datetime.utcnow()`.
- `time_bucket` should be derived from `scheduled_at` as `YYYYMMDDHH`.

Do not create a separate JobRun table yet.

## Database Layer

File: `app/database.py`

Implement:

```txt
DATABASE_URL = "sqlite:///./chatgpt_task.db"
engine
SessionLocal
Base
get_db()
```

Use SQLAlchemy 2.0 style:

```txt
DeclarativeBase
Mapped
mapped_column
```

Set SQLite `check_same_thread=False` because the watcher and worker run in background threads.

## Scheduler

File: `app/scheduler.py`

Implement:

```txt
job_queue
get_time_bucket(scheduled_at: datetime) -> str
find_due_jobs(current_time: datetime, db: Session) -> list[Job]
watcher_loop(interval: int = 10)
worker_loop()
start_scheduler()
```

### get_time_bucket

Convert a datetime to an hourly bucket:

```txt
2026-06-08 11:20 -> "2026060811"
```

### find_due_jobs

Find jobs where:

```txt
time_bucket == get_time_bucket(current_time)
scheduled_at <= current_time
status == "pending"
```

This demonstrates the time-bucket partitioning idea without implementing a production NoSQL store.

### watcher_loop

Every `interval` seconds:

1. Open a DB session.
2. Find due pending jobs.
3. Mark each due job as `queued`.
4. Commit the update.
5. Push the job ID into `job_queue`.
6. Close the DB session.

### worker_loop

Continuously:

1. Read `job_id` from `job_queue`.
2. Load the job.
3. Skip missing or cancelled jobs.
4. Mark the job as `running`.
5. Execute a placeholder action.
6. Set `result = "Executed: {description}"`.
7. Mark the job as `completed`.
8. On exception, mark the job as `failed` and store the error in `result`.
9. Close the DB session and call `job_queue.task_done()`.

### start_scheduler

Start one daemon watcher thread and one daemon worker thread.

Keep this simple. Do not implement retries, heartbeats, DLQs, multiple workers, distributed queues, or recurring scheduling yet.

## MCP Server

File: `app/mcp_server.py`

Use:

```python
from mcp.server.fastmcp import FastMCP
```

The MCP server must:

- Load `.env` with `python-dotenv`.
- Create DB tables on startup.
- Start scheduler background threads on startup.
- Register tools with `FastMCP`.
- Run with `python -m app.mcp_server`.

Use this shape:

```txt
mcp = FastMCP("task-scheduler")
```

## Tool Handlers

Keep business logic in plain functions that accept a SQLAlchemy `Session`.

Implement:

```txt
handle_create_task(db: Session, *, description: str, scheduled_at: str) -> dict
handle_get_status(db: Session, *, job_id: int) -> dict
handle_list_tasks(db: Session) -> dict
handle_cancel_task(db: Session, *, job_id: int) -> dict
```

### handle_create_task

Rules:

- `description` is required and must be non-empty.
- `scheduled_at` is required and must be ISO 8601 parseable.
- Convert `scheduled_at` with `datetime.fromisoformat`.
- Create a `Job` with:
  - `description`
  - `scheduled_at`
  - `time_bucket = get_time_bucket(scheduled_at)`
  - default `status = "pending"`
- Return:

```json
{
  "job_id": 1,
  "status": "pending",
  "scheduled_at": "2026-06-08 11:20:00"
}
```

### handle_get_status

If found, return:

```json
{
  "job_id": 1,
  "description": "review PR #123",
  "status": "completed",
  "scheduled_at": "2026-06-08 11:20:00",
  "result": "Executed: review PR #123"
}
```

If not found, return:

```json
{
  "error": "Job 1 not found"
}
```

### handle_list_tasks

Return all jobs ordered by `scheduled_at` descending:

```json
{
  "jobs": [
    {
      "job_id": 1,
      "description": "review PR #123",
      "status": "pending",
      "scheduled_at": "2026-06-08 11:20:00"
    }
  ]
}
```

### handle_cancel_task

Rules:

- If job does not exist, return an error.
- If job is `completed` or `failed`, return an error.
- Otherwise set status to `cancelled`.

Return:

```json
{
  "job_id": 1,
  "status": "cancelled"
}
```

## MCP Tools

Register these FastMCP tools:

```txt
task_create
task_list
task_status
task_cancel
```

Use Python-safe names with underscores for the actual FastMCP tool names.

Tool descriptions:

```txt
task_create: Schedule a new task for future execution.
task_list: List all scheduled tasks.
task_status: Get the status of a scheduled task by job_id.
task_cancel: Cancel a scheduled task that has not completed yet.
```

Each tool should call a shared session wrapper:

```txt
_with_session(handler, **kwargs)
```

The wrapper should open `SessionLocal()`, call the handler, and close the session.

## MCP Runtime Rules

MCP stdio uses stdout for protocol messages.

Important:

- Do not use `print()` in the MCP server.
- If logging is needed, write to stderr or a file.
- The server entry point must be:

```python
if __name__ == "__main__":
    mcp.run()
```

## README Requirements

Generated `README.md` should explain:

- What the MCP task scheduler does.
- Project structure.
- Setup commands.
- How to run with MCP inspector.
- How to connect to Claude Desktop.
- How to try the tools.
- Troubleshooting notes about stdout and MCP stdio.

Include these commands:

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
python -m app.mcp_server
```

For MCP inspector, include:

```bash
npx @modelcontextprotocol/inspector python -m app.mcp_server
```

## Test Requirements

Add focused tests for:

- `get_time_bucket`
- `find_due_jobs`
- `handle_create_task`
- `handle_get_status`
- `handle_list_tasks`
- `handle_cancel_task`
- Cannot cancel completed jobs
- Missing job status returns an error

Tests should not require Claude Desktop or the MCP inspector.

## Output Expectations

When generating files:

- Create working code, not pseudocode.
- Keep the scaffold small and coherent.
- Use type hints where helpful.
- Prefer simple functions over abstractions.
- Keep the MCP server importable.
- Keep handler logic testable without starting the MCP server.
- Include `.env.example`.
- Make tests pass.

After implementation, run:

```bash
pytest
```

If dependencies are not installed, document:

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
pytest
```

## Acceptance Criteria

The scaffold is complete when:

- `python -m app.mcp_server` starts a real MCP server.
- MCP tools are registered through `FastMCP`.
- `task_create` creates a SQLite Job.
- Watcher can find due jobs by time bucket.
- Worker can execute queued jobs and mark them completed.
- `task_list` returns scheduled jobs.
- `task_status` returns one job status.
- `task_cancel` cancels a non-terminal job.
- No FastAPI app or HTTP routes are generated.
- Tests cover the scheduler and tool handler logic.

## Explicitly Out Of Scope For This Scaffold

Do not implement:

- FastAPI
- REST endpoints
- Recurring jobs
- Cron parsing
- JobRun table
- Retry policies
- Dead letter queues
- Heartbeats or visibility timeouts
- Multiple workers
- External API calls
- OpenAI, Anthropic, or Gemini SDK integration
- Evals pipeline
- Docker deployment
- Auth
- PostgreSQL
