# Spec 01: Real MCP Scheduler Core

## 1. Overview

Spec 01 defines a strict local MCP task scheduler. The scheduler stores jobs,
creates concrete run records, watches for due runs, and executes placeholder
actions. It is intentionally dumb: it validates explicit input and does not
infer user intent, parse natural language, search the internet, resolve
locations, or summarize content.

The LLM or eval harness is responsible for converting a user request into
strict tool arguments before calling the scheduler.

Core flows:

1. Create a job.
2. List jobs.
3. Get one job.
4. Modify a job.
5. Delete a job.
6. Execute due job runs through watcher and worker loops.

## 2. Scope

Included in Spec 01:

- FastMCP server exposing versioned task tools.
- FastAPI REST API for the same job operations.
- Pydantic validation with unknown fields rejected.
- SQLite persistence through SQLAlchemy.
- `jobs` and `job_runs` tables.
- Immediate, one-time, and recurring job types.
- Cron validation and IANA timezone validation.
- Hourly UTC `scheduled_bucket` for watcher queries.
- In-memory queue between watcher and worker.
- Placeholder action execution.
- Deterministic clock support for tests and evals.

Not included in Spec 01:

- Natural-language date parsing.
- Timezone or location inference.
- Internet search.
- Finance/news API calls.
- Article summarization.
- Email or notification delivery.
- Durable production queue.
- Retry policy, heartbeat, DLQ, or crash recovery.

Those belong to later features.

## 3. Design Rules

- The scheduler is strict and deterministic.
- Every scheduled `one_time` or `recurring` job must include an explicit IANA
  timezone.
- Relative phrases such as "tomorrow at 8" must be resolved by the LLM or eval
  harness before calling `task.create@v1`.
- City names such as "Vancouver" must be resolved outside the scheduler.
- The scheduler validates `America/Vancouver`; it does not infer it.
- The scheduler accepts supported action names; it does not decide what action
  the user meant.
- Current action execution is a placeholder.

## 4. Tool Contract

The MCP layer exposes stable, versioned tool names:

| Tool | Purpose |
| --- | --- |
| `task.create@v1` | Create a job and first run. |
| `task.list@v1` | List jobs for a user. |
| `task.get@v1` | View one job and recent runs. |
| `task.modify@v1` | Modify an existing job. |
| `task.delete@v1` | Soft-delete a job and cancel pending runs. |

Tool names use `namespace.verb@version` so tool catalogs stay consistent and
eval traces can compare model behavior across versions.

## 5. Job Types

### `immediate`

Runs as soon as possible.

Rules:

- `time` must be omitted.
- `schedule` must be omitted.
- `timezone` may be omitted and defaults internally to `UTC`.

### `one_time`

Runs once at a specific timestamp.

Rules:

- `time` is required as an ISO 8601 timestamp.
- `timezone` is required as an IANA timezone.
- `schedule` must be omitted.
- If `time` is naive, it is interpreted as wall-clock time in `timezone`.
- If `time` has an offset, the offset is respected and the value is normalized
  to naive UTC for storage.

### `recurring`

Runs repeatedly from a cron expression.

Rules:

- `schedule` is required as a valid cron expression.
- `timezone` is required as an IANA timezone.
- `time` must be omitted.
- Cron is evaluated in the job timezone, then converted to naive UTC for
  storage.

## 6. Create Job

MCP tool:

```txt
task.create@v1
```

REST endpoint:

```http
POST /v1/jobs
```

Request:

```json
{
  "user_id": "user_123",
  "action": "summarize_financial_news",
  "job_params": {
    "type": "one_time",
    "time": "2026-06-10T08:00:00",
    "timezone": "America/Vancouver"
  }
}
```

Success response:

```json
{
  "ok": true,
  "job": {
    "job_id": "job_...",
    "user_id": "user_123",
    "action": "summarize_financial_news",
    "type": "one_time",
    "time": "2026-06-10T08:00:00-07:00",
    "schedule": null,
    "timezone": "America/Vancouver",
    "status": "scheduled",
    "created_at": "2026-06-09T12:00:00Z",
    "updated_at": "2026-06-09T12:00:00Z"
  },
  "next_run": {
    "run_id": "run_...",
    "job_id": "job_...",
    "scheduled_at": "2026-06-10T08:00:00-07:00",
    "status": "pending"
  }
}
```

## 7. List Jobs

MCP tool:

```txt
task.list@v1
```

REST endpoint:

```http
GET /v1/jobs
```

Query parameters:

- `user_id` is required.
- `status` is optional.
- `start_time` is optional.
- `end_time` is optional.
- `page_size` defaults to `20`.
- `page` defaults to `1`.

Default list results hide deleted jobs.

## 8. Get Job

MCP tool:

```txt
task.get@v1
```

REST endpoint:

```http
GET /v1/jobs/{job_id}?user_id=user_123
```

The response includes the job and recent run history.

## 9. Modify Job

MCP tool:

```txt
task.modify@v1
```

REST endpoint:

```http
PATCH /v1/jobs/{job_id}
```

Editable fields:

- `action`
- `status`, only `scheduled` or `paused`
- `job_params.type`
- `job_params.time`
- `job_params.schedule`
- `job_params.timezone`

Rules:

- Deleted jobs cannot be modified.
- Completed immediate jobs cannot be modified.
- Type-specific validation is re-run after applying the patch.
- Schedule-affecting changes cancel pending/queued runs and create a replacement
  run unless the job is paused.
- Running runs are not interrupted.

## 10. Delete Job

MCP tool:

```txt
task.delete@v1
```

REST endpoint:

```http
DELETE /v1/jobs/{job_id}
```

Rules:

- Delete is a soft delete.
- `jobs.status` becomes `deleted`.
- Pending and queued runs become `cancelled`.
- Running runs may finish.
- Repeated delete is idempotent.

## 11. Data Model

### `jobs`

| Column | Purpose |
| --- | --- |
| `job_id` | Primary key. |
| `user_id` | Ownership boundary. |
| `action` | Supported action name. |
| `type` | `immediate`, `one_time`, or `recurring`. |
| `time` | One-time fire instant, stored as naive UTC. |
| `schedule` | Cron expression for recurring jobs. |
| `timezone` | IANA timezone. |
| `status` | Job lifecycle status. |
| `created_at` | Naive UTC timestamp. |
| `updated_at` | Naive UTC timestamp. |

### `job_runs`

| Column | Purpose |
| --- | --- |
| `run_id` | Primary key. |
| `job_id` | Parent job. |
| `user_id` | Copied from job for filtering. |
| `scheduled_at` | Due instant, stored as naive UTC. |
| `scheduled_bucket` | Hourly UTC bucket, for example `2026-06-10T08`. |
| `started_at` | Run start timestamp. |
| `finished_at` | Run finish timestamp. |
| `status` | Run lifecycle status. |
| `retry_count` | Placeholder for future retry behavior. |
| `error_message` | Last failure message. |
| `created_at` | Naive UTC timestamp. |
| `updated_at` | Naive UTC timestamp. |

Indexes:

- `jobs(user_id, status, created_at)`
- `job_runs(scheduled_bucket, status, scheduled_at)`
- `job_runs(job_id, scheduled_at)`

## 12. Scheduler Behavior

The watcher:

1. Computes current and lookback UTC buckets.
2. Selects pending runs where `scheduled_bucket` is hot and `scheduled_at <= now`.
3. Marks them `queued`.
4. Pushes run IDs onto the in-memory queue.

The worker:

1. Pulls a run ID from the queue.
2. Marks the run `running`.
3. Executes the placeholder action.
4. Marks the run `succeeded` or `failed`.
5. Completes one-time/immediate jobs.
6. Creates the next pending run for recurring jobs.

Known MVP limitations:

- The queue is in-memory.
- Queued runs can be stranded if the process crashes after DB status update but
  before queue handoff.
- Runs older than the configured bucket lookback may be missed.
- There is no retry policy or heartbeat.

These are acceptable for Spec 01 and belong to production hardening later.

## 13. Error Contract

All API and MCP tool failures use:

```json
{
  "ok": false,
  "error": {
    "code": "VALIDATION_ERROR",
    "message": "One-time jobs require a time.",
    "field": "job_params.time",
    "expected": "ISO 8601 timestamp"
  }
}
```

Required error codes:

- `VALIDATION_ERROR`
- `NOT_FOUND`
- `PERMISSION_DENIED`
- `CONFLICT`
- `UNSUPPORTED_ACTION`
- `INTERNAL_ERROR`

## 14. Strict Scheduling Example

User says:

```txt
send me the finance news at 8:00 tomorrow
```

The LLM/eval harness must convert that into explicit scheduler input. If the
current date is June 9, 2026 and the user's timezone is `America/Vancouver`, the
scheduler call is:

```json
{
  "tool": "task.create@v1",
  "args": {
    "user_id": "user_123",
    "action": "summarize_financial_news",
    "job_params": {
      "type": "one_time",
      "time": "2026-06-10T08:00:00",
      "timezone": "America/Vancouver"
    }
  }
}
```

Spec 01 does not fetch finance news at scheduling time. At execution time the
worker only runs a placeholder action. Real news search, summarization, and
delivery are Feature 04 or later.

## 15. Tests

Spec 01 should have tests for:

- Pydantic validation and extra-field rejection.
- Cron validation.
- IANA timezone validation.
- Create/list/get/modify/delete service behavior.
- API error envelopes.
- MCP registry dispatch.
- Watcher bucket filtering.
- Worker processing.
- Deterministic clock behavior.

Current implementation verifies these paths with pytest.
