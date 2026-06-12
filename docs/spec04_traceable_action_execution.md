# Spec 04: Traceable Action Execution

## 1. Overview

Spec 04 makes scheduled job execution traceable.

The scheduler should not depend on the LLM to honestly describe what happened.
Instead, the scheduler records every execution step itself and returns a
`trace_id` / `run_id` that the LLM can summarize for the user.

Spec 04 answers:

- What action did the scheduler try to run?
- Which run executed it?
- Which steps happened?
- Which inputs were used?
- What output/artifact was produced?
- Did the action succeed, fail, or get skipped?
- If the user asks "what happened?", can we show the trace?

## 2. Core Rule

The LLM does not create the authoritative trace.

The LLM can:

- choose a tool,
- fill tool arguments,
- summarize returned JSON,
- ask for a trace by `job_id`, `run_id`, or `trace_id`.

The scheduler must:

- create the trace record,
- append trace events during execution,
- return trace identifiers in tool/API responses,
- expose a read tool/API for trace inspection.

This matters because evals need ground truth. If the model says "email sent"
but the executor failed, the trace must reveal that mismatch.

## 3. Trace Types

There are two related trace layers:

| Layer | Owner | Spec | Purpose |
| --- | --- | --- | --- |
| Model/tool-call trace | Eval harness | Spec 07 | Records prompt, selected tool, tool args, tool result, final response, tokens, latency, cost. |
| Scheduler execution trace | Scheduler | Spec 04 | Records what the worker/executor actually did when a run fired. |

Spec 04 implements the scheduler execution trace. Spec 07 later wraps model
runs around it.

This means Claude Desktop is enough to smoke-test that the MCP tools work, but
it is not enough for full model evals. Desktop hides the complete model loop.
The local MCP server can still log tool calls it receives, but it cannot see
hidden reasoning, token usage, model latency, or the final response outside the
tool result. The API eval harness in Spec 07 is where those model-level fields
are captured.

Spec 04 should therefore stay focused on the part this project owns directly:
what the scheduler executed after a job was created.

## 4. Data Model

MVP trace model. Add these tables beside `jobs` and `job_runs`.

### `action_traces`

One row per `JobRun` execution attempt.

```txt
trace_id            string primary key, e.g. trace_...
run_id              string, FK to job_runs.run_id
job_id              string
user_id             string
action              string
status              pending | running | succeeded | failed | skipped
started_at          datetime nullable
finished_at         datetime nullable
summary             text nullable
error_message       text nullable
created_at          datetime
updated_at          datetime
```

### `action_trace_events`

Append-only step log for the trace.

```txt
event_id            string primary key, e.g. evt_...
trace_id            string, FK to action_traces.trace_id
sequence            integer
stage               string, e.g. validate_input, render_email, mock_send_email
status              started | succeeded | failed | skipped
input_json          text nullable
output_json         text nullable
error_message       text nullable
created_at          datetime
```

Keep JSON as text for the SQLite MVP. A later production DB can move this to
native JSON columns.

### Job `action_params`

Add an optional `action_params_json` column to `jobs`.

```txt
action_params_json  text nullable
```

The job schedule fields answer "when should this run?". `action_params_json`
answers "what should this action do?". For example, an email action needs
recipient, subject, and body. A reminder action may only need text.

Keep this as JSON text for the MVP. Service code should expose it as a dict in
tool/API responses.

## 5. Executor Interface

Replace the current placeholder `execute_action(action, job_id=...)` with an
executor that receives context and writes trace events.

Conceptual shape:

```python
class ActionExecutor(Protocol):
    def execute(self, ctx: ActionContext) -> ActionResult:
        ...

@dataclass
class ActionContext:
    trace_id: str
    run_id: str
    job_id: str
    user_id: str
    action: str
    params: dict
    db: Session

@dataclass
class ActionResult:
    status: Literal["succeeded", "failed", "skipped"]
    summary: str
    artifact: dict | None = None
```

The worker should:

1. Create `ActionTrace`.
2. Mark trace `running`.
3. Call the registered executor.
4. Append trace events inside the executor.
5. Mark trace `succeeded` or `failed`.
6. Update `JobRun` status.
7. Return trace data from `task_get_v1` / trace read tools.

The executor registry should be explicit:

```python
EXECUTORS = {
    "send_email": SendEmailMockExecutor(),
    "send_reminder": SendReminderMockExecutor(),
    "generate_report": PlaceholderExecutor("generate_report"),
    "summarize_financial_news": PlaceholderExecutor("summarize_financial_news"),
    "fetch_news": PlaceholderExecutor("fetch_news"),
    "review_pr": PlaceholderExecutor("review_pr"),
}
```

Unknown actions should fail before execution, using the existing
`UNSUPPORTED_ACTION` behavior from Spec 03.

## 6. Action Params

Spec 04 should add optional action-specific params.

For strict scheduling, `type`, `time`, `schedule`, and `timezone` describe when
the job runs. `action_params` describes what the action should do.

Example create call for an email-like placeholder action:

```json
{
  "user_id": "current_user",
  "action": "send_email",
  "type": "one_time",
  "time": "2026-06-12T17:10:00-07:00",
  "timezone": "America/Vancouver",
  "action_params": {
    "to": "dean@example.com",
    "subject": "Project reminder",
    "body": "Review the MCP scheduler project."
  }
}
```

For Spec 04, this is still a mock action. It should not send a real email
unless an explicit external email integration is added later.

Public `task_create_v1` should accept `action_params` as an optional object.
Public `task_modify_v1` should also accept it so a user can fix missing action
details before the run fires.

MCP-facing schema rule:

- schedule fields stay flat: `type`, `time`, `schedule`, `timezone`
- action-specific fields go under `action_params`
- do not put schedule fields inside `action_params`

Example reminder:

```json
{
  "user_id": "current_user",
  "action": "send_reminder",
  "type": "one_time",
  "time": "2026-06-12T17:10:00-07:00",
  "timezone": "America/Vancouver",
  "action_params": {
    "text": "Review the MCP scheduler project."
  }
}
```

## 7. Email Example

User asks:

```txt
Email me the AI evals report at 5:10 PM today.
```

Expected model behavior:

1. Resolve date/time/timezone.
2. If recipient or content is missing, ask one clarification question.
3. Call `task_create_v1`.

Possible tool call:

```json
{
  "user_id": "current_user",
  "action": "send_email",
  "type": "one_time",
  "time": "2026-06-12T17:10:00-07:00",
  "timezone": "America/Vancouver",
  "action_params": {
    "to": "current_user",
    "subject": "AI evals report",
    "body": "Placeholder AI evals report."
  }
}
```

At 5:10 PM, the worker executes `send_email`.

Trace produced by the scheduler:

```json
{
  "trace_id": "trace_abc123",
  "run_id": "run_456",
  "job_id": "job_789",
  "action": "send_email",
  "status": "succeeded",
  "summary": "Mock email prepared for current_user.",
  "events": [
    {
      "sequence": 1,
      "stage": "validate_action_params",
      "status": "succeeded",
      "input_json": {
        "required": ["to", "subject", "body"]
      }
    },
    {
      "sequence": 2,
      "stage": "render_email",
      "status": "succeeded",
      "output_json": {
        "to": "current_user",
        "subject": "AI evals report"
      }
    },
    {
      "sequence": 3,
      "stage": "mock_send_email",
      "status": "succeeded",
      "output_json": {
        "provider": "mock",
        "message_id": "mock_msg_123"
      }
    }
  ]
}
```

The LLM can say:

```txt
The scheduled email action ran successfully. Trace trace_abc123 shows the email
was validated, rendered, and mock-sent by the local scheduler.
```

But the LLM should not invent that trace. It should summarize the returned trace
object.

## 8. New Read Surface

Add one trace read tool:

```txt
task_trace_get_v1
```

Arguments:

```json
{
  "user_id": "current_user",
  "trace_id": "trace_abc123"
}
```

Response:

```json
{
  "ok": true,
  "trace": {
    "trace_id": "trace_abc123",
    "run_id": "run_456",
    "job_id": "job_789",
    "action": "send_email",
    "status": "succeeded",
    "summary": "Mock email prepared for current_user.",
    "events": []
  }
}
```

Also include recent trace summary in `task_get_v1` so users can inspect a job
without knowing the trace id first.

Do not require the user to know database internals. Natural language should
work through Claude/API:

```txt
What happened to my 5:10 PM email task?
```

Expected tool path:

1. call `task_list_v1` or `task_get_v1` to find the job/run,
2. read the latest `trace_id`,
3. call `task_trace_get_v1`,
4. summarize the returned trace object.

## 9. Error Behavior

Trace failures should be structured and fixable.

Example missing email body:

```json
{
  "ok": false,
  "error": {
    "code": "VALIDATION_ERROR",
    "message": "send_email requires action_params.body.",
    "field": "action_params.body",
    "expected": "non-empty email body"
  }
}
```

If execution fails after the job is already scheduled, the trace should still be
saved:

```json
{
  "trace_id": "trace_abc123",
  "status": "failed",
  "error_message": "send_email requires action_params.body",
  "events": [
    {
      "stage": "validate_action_params",
      "status": "failed",
      "error_message": "Missing body"
    }
  ]
}
```

## 10. What Spec 04 Does Not Do

Spec 04 does not need to:

- call real Gmail/SMTP/SendGrid,
- fetch real finance news,
- send push notifications,
- run LLM-generated email content,
- compare GPT/Claude/Gemini,
- calculate token cost.

Those belong to later integrations or Spec 07 evals.

Spec 04 only makes execution inspectable.

## 11. Implementation Checklist

Build Spec 04 in this order:

1. Add IDs:
   - `new_trace_id()`
   - `new_trace_event_id()`

2. Add ORM models:
   - `ActionTrace`
   - `ActionTraceEvent`
   - `Job.action_params_json`

3. Add serializer helpers:
   - `job_action_params(job) -> dict`
   - `trace_to_dict(trace, events=False)`

4. Update create/modify schemas and services:
   - accept optional `action_params`
   - store as JSON text
   - return as dict
   - keep `additionalProperties: false` at the tool schema level where possible

5. Add trace repository helpers:
   - `create_trace_for_run(db, run, job)`
   - `append_trace_event(db, trace_id, stage, status, input=None, output=None, error=None)`
   - `finish_trace(db, trace, status, summary=None, error=None)`

6. Replace placeholder execution:
   - call executor registry from the worker
   - pass `ActionContext`
   - mark trace failed when executor raises
   - always save trace before updating final run/job status

7. Add mock executors:
   - `send_email`: validate params, render email, mock send
   - `send_reminder`: validate text, mock remind
   - placeholder executor for the other allowed actions

8. Add read surfaces:
   - API/service function for trace lookup by `trace_id`
   - MCP tool `task_trace_get_v1`
   - latest trace summary in `task_get_v1`

9. Add tests:
   - create job with `action_params`
   - modify job `action_params`
   - worker creates one trace per run
   - trace events are ordered
   - `send_email` success records validate/render/mock-send
   - missing email body fails with trace event and structured error
   - `task_trace_get_v1` rejects wrong `user_id`
   - Claude-facing tool list includes `task_trace_get_v1`

## 12. Example API Eval Boundary

Spec 04 creates scheduler traces. Spec 07 later creates model traces around
those scheduler traces.

When using Claude/API, the eval harness can record:

```json
{
  "prompt": "Email me the AI evals report at 5:10 PM today.",
  "model": "claude-sonnet-...",
  "actual_tool": "task_create_v1",
  "tool_args": {
    "action": "send_email",
    "type": "one_time",
    "time": "2026-06-12T17:10:00-07:00",
    "timezone": "America/Vancouver",
    "action_params": {
      "to": "current_user",
      "subject": "AI evals report",
      "body": "Placeholder AI evals report."
    }
  },
  "tool_result": {
    "ok": true,
    "job_id": "job_789"
  },
  "final_answer": "Scheduled the email task for 5:10 PM today."
}
```

Later, when the scheduled job fires, Spec 04 records:

```json
{
  "trace_id": "trace_abc123",
  "run_id": "run_456",
  "job_id": "job_789",
  "action": "send_email",
  "status": "succeeded",
  "summary": "Mock email prepared for current_user."
}
```

The eval can then check both layers:

- Did the model choose the right tool and arguments?
- Did the scheduler actually execute the requested action?
- Did the final answer match the real execution state?

## 13. Acceptance Criteria

- Every executed `JobRun` creates one `ActionTrace`.
- Every executor writes ordered `ActionTraceEvent` rows.
- `Job` can store and return optional `action_params`.
- `send_email` has a mock executor that records validate/render/mock-send steps.
- `send_reminder` has a mock executor that records validate/mock-remind steps.
- `task_get_v1` shows trace summary for recent runs.
- `task_trace_get_v1` returns the full trace and events.
- Failed execution still creates a trace.
- Structured errors identify `field` and `expected` whenever action params are
  missing or invalid.
- No LLM-authored trace is trusted as source of truth.
- Claude Desktop remains a manual integration test path.
- API-based evals are deferred to Spec 07.
