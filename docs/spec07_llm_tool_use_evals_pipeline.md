# Spec 07: OpenAI Evals Tool-Use Evaluation Pipeline

## 1. Goal

Use OpenAI eval tooling to measure whether an LLM can correctly use the task
scheduler tools.

Spec 07 evaluates the model/tool layer:

```txt
natural language prompt
  -> OpenAI model call
  -> tool selection
  -> tool arguments
  -> local scheduler tool execution
  -> final answer
  -> OpenAI/local grading
  -> eval report
```

This is different from Claude Desktop testing. Claude Desktop remains manual
smoke testing only. Spec 07 uses API-based runs so the project can record the
model output, tool call, tool args, tool result, final answer, latency, and
token usage.

## 2. Why This Spec Exists

The project needs repeatable answers to these questions:

- Did the model choose the right tool?
- Did it produce valid tool arguments?
- Did it resolve time and timezone correctly?
- Did it avoid unsupported actions?
- Did it recover from structured tool errors?
- Did its final answer match the actual scheduler result?
- Did schema/prompt changes improve pass rate?

Spec 04 records scheduler execution traces. Spec 07 records model/tool-call
traces around those scheduler traces.

## 3. OpenAI Evals Direction

Use OpenAI's evaluation surfaces in this order:

1. local trace capture for every eval case;
2. deterministic local graders for exact tool correctness;
3. OpenAI Evals / graders for repeatable dataset scoring and subjective final
   answer quality;
4. CSV/JSONL reports for local analysis.

Official OpenAI docs recommend using traces and graders for agent workflow
issues such as tool choice, then moving to datasets and eval runs for
repeatable benchmarking. The hosted Evals guide also supports creating eval
runs from datasets and templated model calls.

Important compatibility note: as of June 15, 2026, OpenAI's Evals docs list the
older Evals section under "Legacy APIs", and the archived cookbook points users
to the hosted Evals product instead of the old open-source `openai/evals`
library. Build Spec 07 with a thin adapter so the local dataset/trace format can
be used with either hosted OpenAI Evals or a local fallback runner.

References:

- https://developers.openai.com/api/docs/guides/agent-evals
- https://developers.openai.com/api/docs/guides/evals
- https://developers.openai.com/api/docs/guides/graders

## 4. What To Evaluate

Start with a small, manually designed dataset.

Categories:

```txt
create immediate task
create one-time task
create recurring task
list tasks
get task details
modify task
delete task
read execution trace
recover from invalid tool args
reject unsupported action
```

Do not start with hundreds of cases. Start with 20-40 high-quality cases that
cover the real failure modes seen in Specs 02-04.

## 5. Dataset Format

Create:

```txt
evals/datasets/scheduler_tool_use_v1.jsonl
```

Each row:

```json
{
  "id": "create_one_time_vancouver_001",
  "prompt": "Remind me to review the project at 4:30 PM today.",
  "now": "2026-06-15T12:00:00-07:00",
  "user_id": "eval-user",
  "expected": {
    "tool": "task_create_v1",
    "action": "send_reminder",
    "type": "one_time",
    "time": "2026-06-15T16:30:00-07:00",
    "timezone": "America/Vancouver",
    "action_params": {
      "text": "review the project"
    }
  },
  "grading": {
    "requires_tool_call": true,
    "allow_clarifying_question": false
  }
}
```

Dataset rules:

- every case has a fixed `now`;
- every case has an explicit expected tool;
- time-sensitive cases include expected absolute datetime and timezone;
- negative cases define expected error behavior;
- do not rely on hidden model reasoning.

## 6. Model Runner

Add:

```txt
evals/run_openai_eval.py
```

Responsibilities:

```txt
load dataset
pin clock to item.now
reset isolated SQLite eval DB
call OpenAI model with scheduler tool schemas
execute returned tool calls against local MCP registry/service
send tool results back to the model
capture final answer
write model trace
run deterministic local graders
optionally send results to OpenAI Evals/graders
write report files
```

The runner should call local Python handlers directly through the MCP registry
or service layer. Do not use Claude Desktop. Do not require the MCP stdio server
for eval correctness.

## 7. Tool Schema Source

Use the same public tool schemas as the MCP server:

```txt
task_create_v1
task_list_v1
task_get_v1
task_modify_v1
task_delete_v1
task_trace_get_v1
task_runs_list_v1   # after Spec06
```

Why:

- evals must test the same tool surface exposed to real clients;
- schema changes should show up as eval regressions or improvements.

The eval runner may need a small adapter from MCP-style tool schemas to OpenAI
Responses API tool definitions.

## 8. Model Trace Format

Create:

```txt
evals/results/runs/<run_id>/traces.jsonl
```

Each row:

```json
{
  "case_id": "create_one_time_vancouver_001",
  "model": "gpt-...",
  "prompt": "Remind me to review the project at 4:30 PM today.",
  "now": "2026-06-15T12:00:00-07:00",
  "tool_calls": [
    {
      "name": "task_create_v1",
      "arguments": {
        "user_id": "eval-user",
        "action": "send_reminder",
        "type": "one_time",
        "time": "2026-06-15T16:30:00-07:00",
        "timezone": "America/Vancouver",
        "action_params": {
          "text": "review the project"
        }
      },
      "result": {
        "ok": true,
        "job": {
          "job_id": "job_..."
        }
      }
    }
  ],
  "scheduler_trace_ids": [],
  "final_answer": "Scheduled the reminder for 4:30 PM today.",
  "usage": {
    "input_tokens": 0,
    "output_tokens": 0
  },
  "latency_ms": 0
}
```

This is the model/tool-call trace. Scheduler execution traces from Spec04 stay
in the scheduler DB and are referenced by `trace_id` when a job actually runs.

## 9. Local Deterministic Graders

Add:

```txt
evals/graders.py
```

Required graders:

```txt
tool_name_match
required_tool_called
no_unexpected_tool
json_args_valid
action_match
job_type_match
time_match
timezone_match
action_params_match
tool_result_success
final_answer_consistent
```

Why local graders:

- exact tool correctness is structured and deterministic;
- tool args should not need subjective model grading;
- local graders can inspect internal trace fields.

Each grader returns:

```json
{
  "name": "tool_name_match",
  "passed": true,
  "score": 1.0,
  "reason": "Expected task_create_v1 and got task_create_v1."
}
```

## 10. OpenAI Evals / Graders Integration

Use OpenAI Evals for repeatable hosted grading where it fits.

Best fit:

- final answer quality;
- whether the assistant answered consistently with tool result;
- rubric-style checks;
- prompt/schema comparison over the same dataset.

Less ideal for hosted-only grading:

- direct local scheduler DB inspection;
- local MCP tool execution;
- internal trace validation.

Therefore Spec 07 should produce a flattened grading item for OpenAI Evals:

```json
{
  "case_id": "create_one_time_vancouver_001",
  "prompt": "Remind me to review the project at 4:30 PM today.",
  "expected_tool": "task_create_v1",
  "expected_args": {
    "action": "send_reminder",
    "type": "one_time",
    "time": "2026-06-15T16:30:00-07:00"
  },
  "actual_tool_calls": [
    {
      "name": "task_create_v1",
      "arguments": {
        "action": "send_reminder",
        "type": "one_time",
        "time": "2026-06-15T16:30:00-07:00"
      }
    }
  ],
  "tool_result": {
    "ok": true
  },
  "final_answer": "Scheduled the reminder for 4:30 PM today.",
  "local_passed": true
}
```

The OpenAI grader can then judge:

```txt
Does the final answer accurately summarize the tool result?
Does the response avoid claiming execution that has not happened yet?
Does the assistant ask for clarification only when needed?
```

## 11. Reports

Write:

```txt
evals/results/runs/<run_id>/summary.json
evals/results/runs/<run_id>/results.csv
evals/results/runs/<run_id>/traces.jsonl
evals/results/runs/<run_id>/openai_evals_payload.jsonl
```

`results.csv` columns:

```txt
case_id
category
model
passed
score
expected_tool
actual_tool
expected_action
actual_action
expected_time
actual_time
error_code
latency_ms
input_tokens
output_tokens
failure_reason
```

Why:

- CSV is easy to inspect;
- JSONL preserves full trace;
- OpenAI payload can be uploaded/rerun.

## 12. Prompt And Schema Variants

Spec 07 should support comparing variants:

```txt
baseline_prompt
spec03_strict_schema_prompt
short_tool_instruction
long_tool_instruction
```

Run command:

```txt
python evals/run_openai_eval.py \
  --dataset evals/datasets/scheduler_tool_use_v1.jsonl \
  --model <model> \
  --prompt-version spec03 \
  --out evals/results/runs/<run_id>
```

The exact model should be configurable. Do not hardcode one model in the spec.

## 13. Implementation Order

1. Add `evals/datasets/scheduler_tool_use_v1.jsonl` with 20-40 cases.
2. Add isolated eval DB reset helper.
3. Add OpenAI runner skeleton.
4. Add tool schema adapter for scheduler tools.
5. Add tool-call execution through local registry/service.
6. Add model trace writer.
7. Add local deterministic graders.
8. Add CSV/JSONL report writer.
9. Add OpenAI Evals payload export.
10. Add optional hosted OpenAI Evals run integration.
11. Add README instructions and required environment variables.

## 14. Success Criteria

- Eval dataset has at least 20 cases.
- Runner can execute the dataset against an OpenAI model.
- Runner captures tool name, tool args, tool result, final answer, latency, and
  token usage.
- Local deterministic graders score every case.
- Results are written to CSV and JSONL.
- OpenAI Evals payload is generated.
- Hosted OpenAI Evals integration can grade final-answer consistency when API
  credentials are available.
- Eval run is repeatable with a fixed dataset and pinned `now`.
- Claude Desktop is not required for evals.
- Spec04 scheduler traces can be referenced when scheduled jobs are executed.

## 15. Non-Goals

Spec 07 does not implement:

- production scheduler scaling;
- durable queue or retries;
- real email/news integrations;
- hidden chain-of-thought capture;
- Claude Desktop automation;
- full cross-provider evals.

Cross-provider comparisons and failure analysis belong in Spec08.
