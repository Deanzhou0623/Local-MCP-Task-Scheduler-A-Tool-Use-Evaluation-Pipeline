# Q&A

## Spec 01: Real MCP Scheduler Core

### 1. Time Bucket Partitioning

**Question:** Instead of `SELECT * WHERE scheduled_at <= now()`, why partition jobs by time bucket, for example by hour? What happens to query performance at 1M+ jobs without partitioning?

**Answer:** A broad due-job query can work for a small MVP, but it does not scale well. At 1M+ jobs, repeatedly polling all pending jobs with `scheduled_at <= now()` puts constant pressure on a large index or table range. Even with an index on `(status, scheduled_at)`, the scheduler may repeatedly scan many old, pending, or overdue rows.

Time bucket partitioning narrows each watcher query to the current bucket and a small lookback window. For example, each run can store a UTC `scheduled_bucket` such as `2026-06-10T08`. The watcher then queries only relevant buckets:

```sql
SELECT *
FROM job_runs
WHERE scheduled_bucket IN ('2026-06-10T07', '2026-06-10T08')
AND status = 'pending'
AND scheduled_at <= :now
ORDER BY scheduled_at
LIMIT :batch_size;
```

This keeps polling bounded by due-time locality instead of total table size and creates a path to future database partitioning or sharding.

### 2. Tool Naming

**Question:** Why `task.create` instead of `createTask`? How does naming convention affect LLM tool selection accuracy?

**Answer:** `task.create@v1` follows a `namespace.verb@version` pattern. The namespace groups related tools, the verb names the user intent, and the version keeps schemas stable for evals.

This is clearer for LLM tool selection than inconsistent names like `createTask`, `list_jobs`, and `delete_schedule`. Consistent naming reduces ambiguity because the model can compare tools by domain and verb:

- `task.create@v1`
- `task.list@v1`
- `task.get@v1`
- `task.modify@v1`
- `task.delete@v1`

The `@v1` suffix also makes tool behavior stable when the schema changes later.

### 3. Registry vs If-Else

**Question:** Why use a dictionary registry to route tool calls instead of if-else chains? What happens when you need to add the 20th tool?

**Answer:** A dictionary registry keeps routing data-driven and easy to extend:

```python
TOOL_REGISTRY = {
    "task.create@v1": create_task,
    "task.list@v1": list_tasks,
    "task.get@v1": get_task,
    "task.modify@v1": modify_task,
    "task.delete@v1": delete_task,
}
```

Dispatch then becomes:

```python
handler = TOOL_REGISTRY.get(tool_name)
if handler is None:
    return unsupported_tool_error(tool_name)
return handler(args)
```

An `if/elif` chain is acceptable for a few tools, but by the 20th tool it becomes harder to scan, harder to test, and more likely to collect inconsistent validation or logging behavior. A registry lets tests assert that every declared tool has a handler and every handler has schema metadata.

### 4. Missing Timezone Behavior

**Question:** Review `timeutils.py`: when the user does not provide the timezone, will the LLM use a tool to search for it?

**Answer:** No. The scheduler backend does not call an LLM, network search, or timezone lookup tool. It is dumb and strict: `one_time` and `recurring` jobs require an explicit IANA timezone in `job_params.timezone`.

The strict flow is:

1. User asks for a local-time schedule, for example "every morning at 8 in Vancouver."
2. LLM or eval harness resolves "Vancouver" to `America/Vancouver` outside the scheduler.
3. LLM calls `task.create@v1` with `job_params.timezone = "America/Vancouver"`.
4. The scheduler validates that timezone and stores the job.

If a scheduled job omits timezone, `task.create@v1` returns a structured validation error on `job_params.timezone`. Immediate jobs may still use `UTC` internally because they do not have user-local schedule semantics.

Design rule:

- Backend stays deterministic and does not call an LLM directly.
- Missing required timezone becomes an observable model behavior instead of a hidden backend default.

### 5. Trace For "Send Me The Finance News At 8:00 Tomorrow"

**Question:** If the user says "send me the finance news at 8:00 tomorrow", will the LLM connect to the internet to search finance news, check what time it is, and summarize it? What tools should the trace show?

**Answer:** Split the trace into two phases: scheduling time and execution time.

At scheduling time, the LLM should not fetch or summarize finance news yet. The job is for tomorrow at 8:00, so the immediate task is to create a scheduled job with the correct action, date, time, and timezone.

Expected scheduling-time trace:

1. LLM interprets the user intent as `summarize_financial_news`.
2. LLM or eval harness resolves "tomorrow at 8:00" and the user's timezone outside the scheduler.
3. LLM creates a one-time job. If today is June 9, 2026 and the timezone is `America/Vancouver`, then "tomorrow at 8:00" means June 10, 2026 at 08:00 in that timezone:

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

At execution time, the scheduler/worker should run the job. In the current Spec 01 implementation, this execution is still a placeholder. The code supports the `summarize_financial_news` action name, but it does not yet call the internet, fetch finance news, summarize articles, or send a message.

Future execution-time trace after external API tools are added:

1. Scheduler queues the due `job_run`.
2. Worker starts the run.
3. Action handler calls a finance/news search tool, for example `news.search@v1`.
4. Action handler calls a summarization tool or LLM step.
5. Action handler calls a delivery tool, for example `notification.send@v1` or `email.send@v1`.
6. Worker marks the run as `succeeded` or `failed`.

So the current verified behavior is: the project can trace strict job creation, but timezone inference, real internet news search, summarization, and delivery are outside Spec 01.
