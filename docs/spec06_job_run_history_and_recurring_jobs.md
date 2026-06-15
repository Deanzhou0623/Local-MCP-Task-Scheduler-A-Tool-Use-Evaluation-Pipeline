# Spec 06: Sharded Scheduling, Run History, and Recurring Scale

## 1. Goal

Make scheduled run creation and due-run discovery scale when many jobs share the
same execution time.

Spec 06 owns scheduling:

```txt
Job
  -> JobRun
  -> sharded scheduled bucket
  -> watcher scans due shards
  -> due runs passed to Spec05 durable queue
```

Spec 05 owns execution after a due run is queued.

## 2. Why This Spec Exists

The current scheduler uses one hourly `scheduled_bucket`.

That bounds scans, but it can still create a hot bucket when many jobs are due
in the same hour. Recurring jobs make this worse because many users can schedule
the same cron time, such as every day at 8:00.

Spec 06 fixes scheduling scale and run history:

- spread due runs across bucket shards;
- scan due work in bounded shard batches;
- represent retries and recurring executions clearly;
- expose paginated run history;
- keep timezone and recurring behavior testable.

## 3. Boundary With Spec 05

End-to-end flow:

```txt
task_create_v1
  -> Job
  -> JobRun with sharded scheduled bucket       [Spec 06]
  -> watcher scans due bucket shards            [Spec 06]
  -> durable queue row created                  [Spec 05]
  -> worker claims queue item with lease        [Spec 05]
  -> executor runs action                       [Spec 04/05]
  -> trace/checkpoint written                   [Spec 04/05]
  -> retry attempt created if needed            [Spec 05/06]
  -> recurring next run created if needed       [Spec 06]
```

Short rule:

```txt
Spec 06 finds due work.
Spec 05 executes due work.
```

**Build order.** Despite the numbering, land the shared schema first (the §4.5
`job_runs` columns + Spec 05's `job_run_queue` table), then Spec 05 execution,
then Spec 06 sharding/history. Spec 05's retry helper needs §4.5 columns. See
Spec 05 "Build order".

## 4. Features To Implement

### 4.1 Sharded Scheduled Buckets

Add these fields to `job_runs`:

```txt
scheduled_bucket_hour   string, e.g. 2026061508
scheduled_bucket_shard  integer, e.g. 3
scheduled_bucket        string, e.g. 2026061508#S003
```

Keep `scheduled_bucket` as the combined query field for compatibility.

This changes the bucket format (today `2026-06-10T08` → `2026061508#S003`), so
widen `JobRun.scheduled_bucket` past `String(13)` and update `time_bucket` /
`hot_buckets` in lockstep. Old-format rows do not match the new query; fine
locally (SQLite is recreated), but note it as a format break, not a migration.

Why:

- spreads a hot due hour across multiple logical partitions;
- keeps watcher scans bounded;
- prepares the model for production-style partitioning.

### 4.2 Stable Shard Assignment

Add helper functions:

```txt
bucket_hour(dt) -> "YYYYMMDDHH"
bucket_shard(key, shard_count) -> int
scheduled_bucket(dt, key, shard_count) -> "YYYYMMDDHH#SNNN"
```

`key` is the `run_id` (unique per attempt → even spread). Use a stable hash such
as SHA-256. Do not use Python's randomized `hash()`.

Local default:

```txt
DEFAULT_BUCKET_SHARDS = 64
```

Why:

- deterministic tests;
- stable distribution;
- no accidental shard movement between process restarts.

### 4.3 Watcher Scans Bucket Shards

Default scan is **per hour**, not per shard — `scheduled_bucket_hour` exists so
the common (low-load) case stays one bounded query:

```txt
for bucket_hour in hot_bucket_hours(now):
  find due pending runs where scheduled_bucket_hour = bucket_hour
    order by scheduled_at limit WATCH_BATCH_SIZE
  enqueue due runs through Spec05
```

Per-shard iteration is the **fallback under load** (or for parallel watchers):
only when an hour's batch saturates do you walk `#S000..#SNNN` to bound each
query. A static 64-shard loop on every idle tick is 64× query amplification for
no benefit, so do not make it the default.

```txt
WATCH_BATCH_SIZE_PER_SHARD = 100
MAX_SHARDS_PER_TICK = configurable
```

Why:

- one cheap query when idle; bounded shard batches only when hot.

**Downtime catch-up.** `hot_bucket_hours` is a short window, so a run left
`pending` with a `scheduled_at` older than that window (scheduler was down) is
stranded in a cold bucket. Add a bounded startup/periodic sweep that scans
`status=pending AND scheduled_at <= now` across older hours (capped batch) and
enqueues them. Pairs with the missed-run policy in §4.8.

### 4.4 Dynamic Sharding Design Hook

MVP can use static 64 shards. Add a design hook for future dynamic sharding.

Optional future table:

```txt
schedule_buckets
- bucket_hour
- shard_count
- estimated_jobs
- created_at
- updated_at
```

Example rule:

```txt
target jobs per shard = 2,000
shard_count = ceil(estimated_jobs / 2,000)
round up to nearest power of two
cap at 256 or 512
```

Why:

- normal hours do not need many shards;
- high-load hours can get more shards.

### 4.5 Attempt Metadata

Add attempt fields to `job_runs`:

```txt
attempt_group_id   string
attempt_number     integer, starts at 1
parent_run_id      string nullable
trigger_reason     scheduled | immediate | retry | manual
priority           integer
deadline_at        datetime nullable
```

Recommended rule:

```txt
one JobRun = one execution attempt
one ActionTrace = one JobRun
```

Why:

- retries do not break Spec04 trace uniqueness;
- each attempt is independently inspectable;
- recurring job history stays clear.

### 4.6 Retry Attempt Creation

Spec05 decides whether a failure should retry. Spec06 provides the data model
and helper for creating retry attempts.

Retry attempt behavior:

```txt
attempt_group_id = original group
attempt_number = previous + 1
parent_run_id = previous run_id
trigger_reason = retry
scheduled_at = now + backoff
new sharded scheduled_bucket
status = pending
```

Why:

- retry attempts use the same watcher/queue pipeline as normal runs;
- no special hidden execution path is needed.

### 4.7 Recurring Next-Run Generation

Keep the existing rule:

```txt
after a recurring run succeeds:
  compute next cron time in job timezone
  convert to naive UTC
  create next pending JobRun
```

Update next-run creation to include:

```txt
new attempt_group_id
attempt_number = 1
trigger_reason = scheduled
sharded scheduled_bucket
priority/deadline fields
```

Why:

- recurring runs remain durable and inspectable;
- future recurring runs use the same sharded watcher path.

### 4.8 Missed-Run Policy

Document and test one policy.

MVP policy:

```txt
if the scheduler was down, create only the next future recurring run;
do not backfill every missed recurrence.
```

Why:

- prevents runaway catch-up after downtime;
- keeps local behavior simple and predictable.

### 4.9 Timezone And DST Tests

Add tests for recurring schedules around timezone offset changes.

Cover:

```txt
daily recurring job around DST spring-forward
daily recurring job around DST fall-back
timezone offset changes
```

Why:

- recurring jobs are user-facing time semantics;
- bugs here are hard to detect from generic scheduler tests.

### 4.10 Paginated Run History

Add MCP tool:

```txt
task_runs_list_v1
```

Add REST endpoint:

```txt
GET /v1/jobs/{job_id}/runs?user_id=...&page=1&page_size=20
```

Return:

```txt
run_id
attempt_group_id
attempt_number
trigger_reason
scheduled_at
started_at
finished_at
status
trace summary
pagination
```

Enforce ownership via the existing `_load_owned_job` convention (`NOT_FOUND` /
`PERMISSION_DENIED`) before returning runs. Registry key `task.runs.list@v1`.

Why:

- `task_get_v1` should stay compact;
- users and evals need full execution history when debugging.

### 4.11 Lateness Metrics

Add derived fields to run serializers:

```txt
queue_delay_seconds = started_at - scheduled_at
execution_seconds = finished_at - started_at
lateness_seconds = finished_at - scheduled_at
```

A retry's `scheduled_at` already includes its backoff, so its lateness is not an
SLA miss — treat `trigger_reason=retry` rows separately when reading these.

Why:

- shows whether scheduled work is meeting its SLA window;
- helps compare scheduler behavior under load.

## 5. Implementation Order

1. Add stable bucket helper functions.
2. Add `JobRun` sharding fields.
3. Add `JobRun` attempt metadata fields.
4. Update all run creation helpers.
5. Update watcher to scan sharded buckets.
6. Add retry-attempt creation helper for Spec05 to call.
7. Update recurring next-run creation.
8. Add paginated run history service.
9. Add `task_runs_list_v1`.
10. Add REST run-history endpoint.
11. Add lateness metrics to serializers.
12. Add DST, sharding, retry-history, and pagination tests.

## 6. Success Criteria

- New runs receive `scheduled_bucket` values like `2026061508#S003`.
- Shard assignment is deterministic.
- Watcher scans due bucket shards in bounded batches.
- Existing immediate, one-time, and recurring jobs still execute.
- Recurring next runs use sharded buckets.
- Retry attempts can be represented without multiple traces on one run.
- Run history is paginated.
- Run history includes trace summaries.
- Lateness metrics are available.
- DST/timezone tests cover recurring schedules around offset changes.
- Missed-run policy is documented and tested.
- Spec05 durable queue receives due runs from the sharded watcher.

## 7. Non-Goals

Spec 06 does not implement:

- real cloud database partitioning;
- distributed scheduler leader election;
- real worker auto-scaling;
- billing or real SLA enforcement;
- real LLM/API calls;
- the Spec07 eval harness.
