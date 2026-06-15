# Spec 05: Durable Queue, Worker Pool, Retry, and Checkpointing

## 1. Goal

Make execution reliable after a `JobRun` becomes due.

Spec 06 finds due runs. Spec 05 executes them safely:

```txt
due JobRun
  -> durable queue row
  -> worker claim with lease
  -> executor
  -> trace/checkpoint
  -> success, permanent failure, or retry
```

This replaces the prototype's in-memory queue as the source of truth.

## 2. Why This Spec Exists

The current watcher/worker flow can lose or strand work:

- a run can be marked `queued` but never reach the in-memory queue;
- a run can stay `running` forever if the worker crashes;
- queued work disappears on process restart;
- failures have no retry classification;
- expensive multi-step work cannot resume from a checkpoint;
- there is no queue depth or scheduler health view.

Spec 05 fixes execution reliability. It does not change how due runs are found;
that is Spec 06.

**Build order.** Specs 05 and 06 share schema changes and both touch the watcher.
Land a shared prerequisite first — the `job_runs` columns from Spec 06 §4.5
(attempt metadata) plus the `job_run_queue` table below — then Spec 05
(execution), then Spec 06 (sharding/history). Spec 05's retry creation (§3.6)
depends on the Spec 06 attempt columns, so they cannot be built strictly in
number order.

## 3. Features To Implement

### 3.1 Durable Queue Table

Add `job_run_queue`.

```txt
queue_id          string primary key
run_id            string, FK to job_runs.run_id
user_id           string
priority          integer, lower number runs first
status            ready | leased | done | failed | cancelled
available_at      datetime
locked_by         string nullable
locked_until      datetime nullable
attempt_number    integer
error_message     text nullable
created_at        datetime
updated_at        datetime
```

Idempotency: a partial unique index on `run_id` for active statuses
(`ready`, `leased`) guarantees one live queue row per run.

Status invariant (keep the two state machines in lockstep): `run=queued ↔
queue=ready`, `run=running ↔ queue=leased`. Recovery (§3.9) detects drift.

Why:

- queued work survives process restart;
- watcher enqueue becomes idempotent;
- workers can poll durable state instead of relying on memory.

### 3.2 Queue Repository

Add helpers:

```txt
enqueue_run_once(db, run, priority)
claim_next_queue_item(db, worker_id, lease_seconds)
complete_queue_item(db, queue_item)
fail_queue_item(db, queue_item, error)
cancel_queue_items_for_run(db, run_id)
recover_expired_leases(db, now)
```

`claim_next_queue_item` must claim atomically — a single conditional
`UPDATE … SET status='leased' WHERE status='ready' AND queue_id = (SELECT …
ORDER BY priority, available_at LIMIT 1)`, then check rowcount. SQLite has no
`FOR UPDATE`/`SKIP LOCKED`; select-then-update races and double-claims.

Why:

- keeps queue state transitions centralized;
- makes watcher and worker easier to test;
- prevents duplicate queue rows for the same active run.

### 3.3 Watcher Writes Durable Queue Rows

Update watcher behavior:

```txt
find due pending JobRuns
for each run:
  enqueue_run_once(...)
  mark JobRun.status = queued
commit
```

Why:

- the DB becomes the source of truth;
- process crashes no longer lose queued run IDs.

### 3.4 Worker Lease

Workers claim queue items with a lease:

```txt
status = leased
locked_by = worker_id
locked_until = now + lease_seconds
```

The lease makes N workers safe; the MVP may start a small fixed pool (e.g. 2
threads). A DB-polled worker needs a poll interval — trade wakeup latency for
load; default ~1s locally.

Why:

- prevents two workers from executing the same queue item;
- expired leases let another worker recover crashed work.

### 3.5 Failure Classes

Extend executor outcomes:

```txt
succeeded
skipped
failed_retryable
failed_permanent
```

Why:

- validation/configuration errors should not retry;
- transient downstream failures should retry with backoff;
- retry policy needs a clear signal from the executor.

Examples:

```txt
missing action_params.body -> failed_permanent
temporary timeout -> failed_retryable
rate limit -> failed_retryable
unsupported action -> failed_permanent
```

This extends Spec 04's `ActionResult.status`. The failure **class** drives the
retry decision only; the **trace status stays `failed`** (don't add new values
to the trace enum). Today's executors raise `AppError` → `failed_permanent`; add
one mock flaky executor that returns `failed_retryable` N times then succeeds,
so retry/backoff is testable.

### 3.6 Retry Policy

Use capped backoff:

```txt
max_retries = 3
backoff = 30s, 2m, 10m
```

Recommended model:

```txt
one JobRun = one execution attempt
one ActionTrace = one JobRun
```

On retryable failure:

```txt
current run -> failed
current queue item -> failed
new retry JobRun -> pending at now + backoff
  (with Spec 06 §4.5 attempt metadata: same attempt_group_id,
   attempt_number + 1, parent_run_id, trigger_reason=retry)
```

Backoff is enforced once, by the retry run's `scheduled_at` (the watcher won't
enqueue it early); `available_at` on the eventual queue row just mirrors it — do
not delay twice.

Why:

- keeps Spec04 trace uniqueness simple;
- makes each attempt independently inspectable.

### 3.7 Checkpointing

Use trace events as checkpoints.

Example executor steps:

```txt
validate_input
mock_llm_call
render_result
send_notification
```

Why:

- if a late step fails, retry should not redo expensive completed steps;
- traces become both observability and resume metadata.

Because a retry is a *new* run with its own trace, completed-step checkpoints
live in the **previous** attempt's trace. The resuming executor reads prior
events by `attempt_group_id` — `completed_stages(db, attempt_group_id)` — and
skips any stage already `succeeded`, recording it as `skipped` (resumed) in the
new trace. Stage names are the idempotency keys.

MVP:

- record step outputs in trace events;
- implement checkpoint resume for one mock multi-step executor.

### 3.8 Priority

Add `priority` to queue items.

```txt
0   high priority
10  normal
100 background
```

Claim order:

```txt
priority asc, available_at asc
```

Why:

- when many jobs are due, higher-priority work should run first;
- this prepares for SLA tiers without implementing billing.

### 3.9 Recovery

Add recovery functions for:

```txt
queued run with no active queue row
leased queue item whose locked_until expired
running run whose started_at is too old
```

A maintenance tick must call these on a timer (fold into the watcher loop or a
dedicated janitor thread). Each recovery helper takes an explicit `db` so it is
testable synchronously, like `process_run`.

Why:

- prevents permanent stuck states after process crashes;
- makes local restart behavior closer to production execution.

### 3.10 Scheduler Status

Add:

```txt
GET /v1/scheduler/status
```

Return counts:

```json
{
  "ok": true,
  "runs": {
    "pending": 10,
    "queued": 4,
    "running": 2,
    "succeeded": 20,
    "failed": 1
  },
  "queue": {
    "ready": 4,
    "leased": 2,
    "done": 20,
    "failed": 1,
    "expired_leases": 0
  }
}
```

Why:

- shows queue depth;
- shows stuck work;
- gives a simple operational check for local testing.

## 4. Implementation Order

1. Add `new_queue_id()`.
2. Add `JobRunQueue` ORM model.
3. Add queue repository helpers.
4. Update watcher to enqueue durably.
5. Update worker to claim queue items with lease.
6. Add failure classes to `ActionResult`.
7. Add retry creation with capped backoff.
8. Add checkpoint-resume support for one mock executor.
9. Add recovery helpers.
10. Add `/v1/scheduler/status`.
11. Update tests.

## 5. Success Criteria

- Due runs are stored in `job_run_queue`.
- Watcher enqueue is idempotent.
- A process restart does not lose queued work.
- Worker claims one queue item with a lease.
- Expired leases can be reclaimed.
- Stale `queued` and stale `running` runs can be recovered.
- Retryable failures create retry attempts with backoff.
- Permanent failures do not retry.
- Every execution attempt has one `ActionTrace`.
- At least one mock multi-step executor can resume from a checkpoint.
- Queue priority affects claim order.
- Scheduler status reports run and queue counts.
- Existing MCP task tools remain backward compatible.

## 6. Non-Goals

Spec 05 does not implement:

- real AWS SQS/Redis/Kafka;
- real LLM provider calls;
- real email delivery;
- billing or real user plan management;
- the Spec07 eval harness.
