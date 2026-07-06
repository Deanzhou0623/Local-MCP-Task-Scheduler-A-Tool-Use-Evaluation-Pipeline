# Scheduler Tool-Use Evals (Spec 07)

A repeatable pipeline that measures whether an LLM can correctly drive the task
scheduler tools. It follows Anthropic's *Demystifying AI Evals for Agents* and
OpenAI's Evals/Agent-Evals/Graders guides.

## What it evaluates

Two layers per case (spec 04 §3):

- **Model / tool-call trace** — did the model pick the right tool + valid args?
- **Scheduler outcome** — did the scheduler *actually* record the intended
  job/run/trace? The eval DB is the ground truth, so a case passes only when the
  outcome agrees with the claim (not merely because the final answer says so).

Grading is **outcome-aware**: `time_match` compares instants (not strings),
`action_params_match` compares parsed dicts, `tool_result_success` inspects the
real dispatch result. It never grades an exact tool-call *sequence*.

## Layout

| Path | Purpose |
| --- | --- |
| `datasets/scheduler_tool_use_v1.jsonl` | 40 hand-written cases: 30 scheduler cases + 10 safety cases |
| `executor.py` | isolated eval DB + clock pin; runs tool calls through the real MCP `dispatch` |
| `tool_schemas.py` | MCP public schemas → OpenAI / Anthropic / Gemini tool defs |
| `models.py` | `HeuristicModel` (default, no key) + optional `OpenAIModel` / `AnthropicModel` / `GeminiModel` |
| `graders.py` | deterministic tool/outcome/safety graders + overall verdict |
| `judge.py` | local offline judge + optional OpenAI / Anthropic / Gemini LLM-as-judge |
| `report.py` | writes `summary.json`, `results.csv`, `traces.jsonl`, `openai_evals_payload.jsonl` |
| `run_openai_eval.py` | CLI / `run_eval()` orchestrator |
| `rejudge.py` | re-run the LLM judge over existing run folders (swap/repair the judge, no model re-run) |
| `analyze_results.py` | Spec 08 offline comparison, failure clustering, leaderboard, machine-readable outputs |
| `make_reports.py` | generate the 4 human reports (one per model + a final comparison with diagrams/conclusion) |
| `prompts.py` | prompt variants for A/B comparison |

## Run

```bash
cd chatGPT-task
python -m evals.run_openai_eval --model local --prompt-version spec03 \
  --judge-model local --out evals/results/runs/local-demo
```

- `--model local` (default) uses the deterministic `HeuristicModel` — no API key,
  fully offline, and *genuinely fallible* so the report shows a real pass rate.
- `--model <provider>:<id>` calls a real provider with the same tool schemas:
  `openai:<id>` (`OPENAI_API_KEY`), `anthropic:<id>` (`ANTHROPIC_API_KEY`),
  `gemini:<id>` (`GOOGLE_API_KEY`). No key → use `local`.
- `--judge-model local` gives an offline final-answer quality score. Use
  `--judge-model openai:<model>` for a real LLM-as-judge pass.
- `--prompt-version` ∈ `baseline | spec03 | short | long`.

### Recommended workflow (smoke cheap, then compare flagships)

Live runs cost money, so **always smoke-test with the cheapest model of each
provider first**, record the result, and confirm the pipeline + dataset are
healthy before spending on flagship models.

**Step 1 — cheap smoke** (a few cents; verifies the adapter, grading, and the
dataset calibration):

```bash
python -m evals.run_openai_eval --model openai:gpt-4o-mini              --judge-model openai:gpt-4o-mini --out evals/results/runs/smoke-openai
python -m evals.run_openai_eval --model anthropic:claude-haiku-4-5-20251001 --judge-model openai:gpt-4o-mini --out evals/results/runs/smoke-anthropic
python -m evals.run_openai_eval --model gemini:gemini-2.5-flash          --judge-model openai:gpt-4o-mini --out evals/results/runs/smoke-gemini
python -m evals.analyze_results --runs evals/results/runs/smoke-openai evals/results/runs/smoke-anthropic evals/results/runs/smoke-gemini --out evals/results/analysis/smoke
```

Read `analysis/smoke/recommendations.md`. If failures point at the dataset or
system prompt (owner `dataset`/`system_prompt`), fix those first — otherwise the
flagship run just pays more to reproduce the same miscalibration.

**Step 2 — flagship comparison** (only after the smoke looks right):

```bash
python -m evals.run_openai_eval --model openai:<flagship>    --judge-model openai:gpt-4o-mini --out evals/results/runs/openai
python -m evals.run_openai_eval --model anthropic:<flagship> --judge-model openai:gpt-4o-mini --out evals/results/runs/anthropic
python -m evals.run_openai_eval --model gemini:<flagship>    --judge-model openai:gpt-4o-mini --out evals/results/runs/gemini
python -m evals.analyze_results --runs evals/results/runs/openai evals/results/runs/anthropic evals/results/runs/gemini --out evals/results/analysis/flagship
```

The scheduler stays local and mock — only the model *decision* comes from the
API; tool execution and grading are local. Model ids are not hardcoded — pass
whatever current id you want. Use **one fixed `--judge-model`** across all runs
so the judge is consistent and no model grades its own answers. Tests/CI never
call a provider (local fallback).

Outputs land in the `--out` run directory:

- `results.csv` — flat rows for pandas / Spec 08 analysis.
- `traces.jsonl` — full model traces (tool calls, results, created
  `job_id`/`run_id` values, `scheduler_trace_ids`, judge output).
- `summary.json` — pass rate by category and by grader.
- `openai_evals_payload.jsonl` — flattened items for a hosted OpenAI LLM-judge
  pass over final-answer quality (wired, run separately when a key is available).

## Analyze runs (Spec 08)

After one or more Spec07 runs exist, analyze them offline:

```bash
cd chatGPT-task
python -m evals.analyze_results \
  --runs evals/results/runs/local-demo \
  --out evals/results/analysis/local-demo
```

For model comparison, pass multiple run folders:

```bash
python -m evals.analyze_results \
  --runs evals/results/runs/openai-gpt \
         evals/results/runs/anthropic-sonnet \
         evals/results/runs/google-gemini \
  --out evals/results/analysis/compare-001
```

Outputs:

- `analysis_summary.json` — normalized run metrics and top failure types.
- `model_leaderboard.csv` — weighted comparison across runs/models.
- `category_breakdown.csv` and `grader_breakdown.csv` — weak areas by category
  and deterministic grader.
- `failure_clusters.json` / `.csv` — grouped failure modes with case ids.
- `all_failures.jsonl` / `.csv` — every failing case with full detail.
- `trace_track_report.json` — job/run/trace-id coverage for trace-track cases.
- `recommendations.md` — concise next changes to make in prompts, schemas, or
  dataset cases.

### Human reports (the deliverable)

`analyze_results` writes the machine-readable analysis above. To get the concise
**4 markdown reports** — one per model plus a final comparison with diagrams, a
conclusion, and "worth exploring" (no recommendations) — run `make_reports`
against the same run + analysis folders:

```bash
python -m evals.make_reports \
  --runs evals/results/runs/openai evals/results/runs/anthropic evals/results/runs/gemini \
  --analysis evals/results/analysis/flagship \
  --out evals/results/reports
```

This produces `report-<model>.md` per model (pass rate, category/grader
breakdown, trace-track, and per-case failure detail) and
`report-final-report.md` (leaderboard + ASCII diagrams + commons/differences
+ conclusion). Swap the LLM judge on an existing run without re-running the
models with `python -m evals.rejudge --runs <dir> --judge-model anthropic:<id>`.

## Dataset format

Each JSONL row pins `now`, gives an explicit `expected` outcome, and (for
local-time cases) an ambient `context.timezone` the way a real client injects it.
Stateful categories (`get`/`modify`/`delete`/`read_runs`/`read_trace`) use a
`seed` list of oracle calls run before the graded turn; `{{job_id}}` /
`{{trace_id}}` placeholders in the prompt/expected are substituted from the seed.

Positive, negative, and safety cases are included (ask for a missing timezone;
decline out-of-scope; surface `NOT_FOUND`; avoid admin/cross-user/secret
requests). Exactly 10 scheduler cases set `grading.trace_track=true` for deeper
trace inspection.

**Email addresses are placeholders.** The `send_email` case uses the reserved
test address `test@example.com` (RFC 2606) in both the prompt and the expected
`action_params.to` — no real address is stored. Execution is a mock anyway (the
`send_email` executor never contacts a mail server), so nothing is ever sent.

## Environment

Only needed for real-provider runs / judges; copy `.env.example` → `.env`.

```txt
OPENAI_API_KEY=      # --model/--judge-model openai:<id>
ANTHROPIC_API_KEY=   # --model/--judge-model anthropic:<id>
GEMINI_API_KEY=      # --model/--judge-model gemini:<id>  (or GOOGLE_API_KEY)
```

With no key set, everything falls back to the local model/judge, so tests/CI
stay free and green.

## Provider comparison

`run_openai_eval` supports `openai:<id>`, `anthropic:<id>`, and `gemini:<id>`
behind one interface (all with real multi-turn tool use). The analyzer and
report generator never call provider APIs — run each provider through Spec07
first, then compare the output folders with `analyze_results` + `make_reports`.
Use **one fixed `--judge-model`** across runs so the judge is consistent.
