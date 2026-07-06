# Spec 08: Eval Results Analysis And Iteration

## 1. Goal

Analyze Spec07 eval outputs so the project can compare models, find failure
patterns, and decide what to improve next.

Spec07 answers:

```txt
Can one model run the scheduler tool-use dataset and produce traces/reports?
```

Spec08 answers:

```txt
Which run is better?
Why did cases fail?
What prompt/schema/dataset change should we try next?
```

Spec08 is offline analysis only:

```txt
Spec07 run artifacts
  -> validate output files
  -> compute metrics
  -> compare models/prompts/providers
  -> cluster failures
  -> inspect trace-track cases
  -> write analysis reports
  -> recommend next iteration
```

It should not call Claude Desktop, call provider APIs, execute scheduler jobs,
or require hidden chain-of-thought.

## 2. Why This Spec Exists

Raw pass rate is not enough.

The project needs repeatable answers to these questions:

- Which model chooses the correct scheduler tool most often?
- Which model handles time and timezone reasoning best?
- Which model is safest on cross-user, secret, and unsupported-action prompts?
- Which failures come from tool schema, system prompt, dataset ambiguity, model
  behavior, scheduler backend, or judge quality?
- Did a prompt/schema change improve the weak categories?
- Which cases should be rewritten, added, or removed?

Spec04 records scheduler execution traces. Spec07 records model/tool-call eval
traces. Spec08 analyzes those traces across one or more runs.

## 3. Input Artifacts

Read one or more Spec07 run directories:

```txt
evals/results/runs/<run_id>/
  summary.json
  results.csv
  traces.jsonl
  openai_evals_payload.jsonl
```

The analyzer should fail fast if a required file is missing.

Required `results.csv` columns:

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
llm_judge_passed
llm_judge_score
trace_track
safety_case
```

Required `traces.jsonl` fields:

```txt
case_id
model
prompt_version
tool_schema_version
prompt
now
tool_calls
created_job_ids
created_run_ids
scheduler_trace_ids
final_answer
deterministic_graders
llm_judge
trace_track
safety_case
usage
latency_ms
```

`results.csv` and `traces.jsonl` must contain the same `case_id` set. If they
do not, the analyzer should stop with a clear validation error.

## 4. Analyzer CLI

Add:

```txt
evals/analyze_results.py
```

Single-run command:

```bash
python -m evals.analyze_results \
  --runs evals/results/runs/local-demo \
  --out evals/results/analysis/local-demo
```

Multi-run comparison command:

```bash
python -m evals.analyze_results \
  --runs evals/results/runs/openai-gpt \
         evals/results/runs/anthropic-sonnet \
         evals/results/runs/google-gemini \
  --out evals/results/analysis/compare-001
```

Responsibilities:

```txt
load Spec07 run artifacts
validate schemas
compute per-run metrics
compute per-category metrics
compute per-grader metrics
compute safety metrics
compute trace-track metrics
compute latency/token metrics
cluster failures
rank runs/models
write JSON/CSV/Markdown reports
```

The analyzer should work with one local run, one OpenAI run, or multiple
provider runs.

## 5. Model Comparison Scope

Spec08 should be ready to compare:

```txt
openai:<model>
anthropic:<model>
gemini:<model>
```

Provider API adapters belong in Spec07's model runner, not in Spec08. Spec08
only consumes the run folders after those runs exist.

The exact model ids must be configurable. Do not hardcode names like:

```txt
Sonnet 5
GPT 5.5
Gemini 3.1 Pro
```

Those names should appear only as run metadata or run folder labels.

Deterministic graders remain the source of truth for tool correctness. The
LLM-as-judge score contributes only final-answer quality.

## 6. Metrics

Per-grader, safety, and final-answer rates are computed from
`traces.jsonl.deterministic_graders` (the flat `results.csv` has no per-grader
columns), counting only graders marked `applicable`. Compute a rate for every
grader present so the set stays in sync as graders are added.

For each run/model, compute:

```txt
total_cases
passed_cases
pass_rate
mean_score
mean_llm_judge_score
llm_judge_pass_rate
mean_latency_ms
p50_latency_ms
p95_latency_ms
input_tokens_total
output_tokens_total
```

Tool-use rates from deterministic graders:

```txt
tool_name_match_rate
required_tool_called_rate
no_unexpected_tool_rate
json_args_valid_rate
action_match_rate
job_type_match_rate
time_match_rate
timezone_match_rate
action_params_match_rate
tool_result_success_rate
```

Safety rates:

```txt
safety_pass_rate
expected_user_id_used_rate
forbidden_user_id_not_used_rate
no_secret_leak_rate
no_unsupported_action_rate
no_false_execution_claim_rate
safe_refusal_present_rate
```

Final-answer rates:

```txt
final_answer_consistent_rate
no_false_success_claim_rate
past_time_acknowledged_rate
llm_judge_pass_rate
mean_llm_judge_score
```

Trace-track metrics:

```txt
trace_track_total
trace_track_pass_rate
avg_tool_calls_per_case
cases_with_job_id
cases_with_run_id
cases_with_scheduler_trace_id
trace_track_failure_cases
```

`cases_with_scheduler_trace_id` is expected to be low: Spec07 only fires (and so
produces a Spec04 trace for) **immediate** jobs during the eval; one-time and
recurring jobs are future-scheduled and never run. Read it as "how many tracked
cases actually executed," not as a coverage gap.

## 7. Failure Clustering

Group failed cases by observable fields:

```txt
category
failed grader name
expected_tool vs actual_tool
expected_action vs actual_action
expected_time vs actual_time
error_code
safety_case
trace_track
```

Each cluster should include:

```txt
cluster_id
failure_type
count
affected_models
affected_categories
case_ids
example_prompt
example_failure_reason
suggested_owner
```

Suggested owner values:

```txt
dataset
tool_schema
system_prompt
model_behavior
scheduler_backend
judge_quality
```

Ownership examples:

- `time_match` or `timezone_match` failures -> system prompt or dataset wording.
- `tool_name_match`, `action_match`, or `json_args_valid` failures -> tool
  schema or model behavior.
- `tool_result_success` failures after correct args -> scheduler backend or
  seed setup.
- safety grader failures -> system prompt or safety dataset.
- judge-only failures -> final-answer rubric or judge quality.

Do not cluster by hidden reasoning. Only use prompts, tool calls, tool results,
grader outputs, and final answers.

## 8. Leaderboard

Generate:

```txt
evals/results/analysis/<analysis_id>/model_leaderboard.csv
```

Default weighted score:

```txt
deterministic_pass_rate: 0.50
safety_pass_rate:        0.20
time_accuracy:           0.10
llm_judge_score:         0.10
latency_score:           0.05
token_efficiency_score:  0.05
```

`latency_score` and `token_efficiency_score` are **relative across the compared
runs**: `score = max(0, 1 - value / max_value)` over `p95_latency_ms` and total
tokens (lower is better). For a **single run** there is nothing to compare, so
both default to `1.0` and the weighted score reduces to the quality/safety
terms. `time_accuracy` is `time_match_rate`.

Rules:

- deterministic correctness should dominate;
- safety should be separate and visible;
- LLM-as-judge should never outweigh tool correctness;
- latency and token usage should not hide correctness failures.

Leaderboard columns:

```txt
rank
model
run_id
weighted_score
pass_rate
safety_pass_rate
time_match_rate
mean_llm_judge_score
p95_latency_ms
input_tokens_total
output_tokens_total
top_failure_type
```

Single-run analysis should still produce a leaderboard with one row.

## 9. Trace-Track Report

Generate:

```txt
evals/results/analysis/<analysis_id>/trace_track_report.json
```

For every case where `trace_track=true`, include:

```txt
case_id
model
passed
tool_call_count
created_job_ids
created_run_ids
scheduler_trace_ids
failure_reason
```

Trace-track analysis answers:

```txt
Did the model call the expected tool?
Did the scheduler create a job?
Did the scheduler create a run?
Did Spec04 produce a scheduler trace id?
What final outcome did the user see?
Which grader failed?
```

Again, do not require hidden chain-of-thought. Observable traces are enough for
the eval.

## 10. Recommendations

Generate:

```txt
evals/results/analysis/<analysis_id>/recommendations.md
```

Recommendations should be derived from failure clusters, not hand-written
guesses. Each is generated by mapping the cluster's failed grader (and its
`suggested_owner` from §7) to a templated `proposed_change` + the metric it
should move — e.g. `time_match` → tighten time/timezone wording
(`time_match_rate`); `no_false_success_claim`/`past_time_acknowledged` → instruct
the model to acknowledge tool errors and past times honestly; safety graders →
strengthen the current-user instruction (`safety_pass_rate`).

Each recommendation should include:

```txt
source_cluster
affected_cases
proposed_change
expected_metric_to_improve
```

Example recommendations:

```txt
- Time failures are concentrated in one-time reminder cases. Tighten the system
  prompt around "today/tomorrow" and IANA timezone requirements.

- The model chose send_reminder for explicit email prompts. Clarify tool action
  descriptions so explicit email intent maps to send_email.

- Cross-user safety failures appear in admin/user_id prompts. Strengthen the
  current-user instruction and keep deterministic safety graders strict.
```

The analyzer can recommend changes. It should not automatically edit prompts,
schemas, or dataset rows.

## 11. Reports

Write:

```txt
evals/results/analysis/<analysis_id>/analysis_summary.json
evals/results/analysis/<analysis_id>/model_leaderboard.csv
evals/results/analysis/<analysis_id>/category_breakdown.csv
evals/results/analysis/<analysis_id>/grader_breakdown.csv
evals/results/analysis/<analysis_id>/failure_clusters.json
evals/results/analysis/<analysis_id>/failure_clusters.csv
evals/results/analysis/<analysis_id>/trace_track_report.json
evals/results/analysis/<analysis_id>/recommendations.md
```

`analysis_summary.json` should include:

```json
{
  "generated_at": "2026-06-15T00:00:00+00:00",
  "runs": [],
  "leader": "local-heuristic",
  "model_count": 1,
  "case_count": 40,
  "top_failure_types": [],
  "recommendations": []
}
```

Why:

- CSV files are easy to inspect and sort;
- JSON files preserve structured details;
- Markdown gives the project writeup a concise human-readable summary.

## 12. Implementation Order

1. Add `evals/analyze_results.py`.
2. Validate required Spec07 artifacts and fields.
3. Load one or more run directories.
4. Compute run-level metrics.
5. Compute category and grader breakdowns.
6. Compute safety and final-answer metrics.
7. Compute trace-track report.
8. Cluster deterministic failures.
9. Generate weighted model leaderboard.
10. Generate recommendations from clusters.
11. Write JSON/CSV/Markdown reports.
12. Add tests using generated local Spec07 run artifacts.
13. Update eval README with analysis commands.

## 13. Success Criteria

- Analyzer accepts one or more Spec07 run directories.
- Analyzer validates required artifacts and fields.
- Analyzer stops with a clear error for malformed runs.
- Analyzer writes all Spec08 output files.
- Leaderboard ranks runs/models by weighted score.
- Category breakdown identifies weak task categories.
- Grader breakdown identifies weak correctness checks.
- Safety cases are summarized separately.
- Trace-track report shows job/run/trace-id coverage.
- Failure clusters include case ids and example reasons.
- Recommendations map failures to concrete next actions.
- Tests require no provider API keys.
- Claude Desktop is not required for analysis.

## 14. Non-Goals

Spec08 does not implement:

- new scheduler behavior;
- new MCP tools;
- real email/news integrations;
- provider API adapters;
- hosted OpenAI Evals execution;
- hidden chain-of-thought capture;
- Claude Desktop automation;
- automatic prompt/schema edits.

Provider adapters belong in Spec07. Scheduler scaling belongs in Specs05-06.
Spec08 only analyzes completed eval run outputs.
