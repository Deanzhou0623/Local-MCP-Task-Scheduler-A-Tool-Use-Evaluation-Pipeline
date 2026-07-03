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
| `tool_schemas.py` | MCP public schemas → OpenAI Responses tool defs |
| `models.py` | `HeuristicModel` (default, no key) and optional `OpenAIModel` |
| `graders.py` | deterministic tool/outcome/safety graders + overall verdict |
| `judge.py` | local offline final-answer judge + optional OpenAI LLM-as-judge |
| `report.py` | writes `summary.json`, `results.csv`, `traces.jsonl`, `openai_evals_payload.jsonl` |
| `run_openai_eval.py` | CLI / `run_eval()` orchestrator |
| `analyze_results.py` | Spec 08 offline comparison, failure clustering, leaderboard, recommendations |
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

### Compare three real models

Run the same dataset once per provider into its own run folder, then let Spec 08
compare them. The scheduler stays local and mock — only the model *decision* comes
from the API; tool execution and grading are local.

```bash
python -m evals.run_openai_eval --model openai:<id>    --out evals/results/runs/openai
python -m evals.run_openai_eval --model anthropic:<id> --out evals/results/runs/anthropic
python -m evals.run_openai_eval --model gemini:<id>    --out evals/results/runs/gemini
# then: python -m evals.analyze_results --runs evals/results/runs/{openai,anthropic,gemini}
```

Model ids are not hardcoded — pass whatever current id you want. Live runs cost
money and need the matching key; tests/CI never call a provider (local fallback).

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
- `trace_track_report.json` — job/run/trace-id coverage for trace-track cases.
- `recommendations.md` — concise next changes to make in prompts, schemas, or
  dataset cases.

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

## Environment

```txt
OPENAI_API_KEY=      # only for --model openai:<model> and the hosted judge
```

## Provider comparison

The analyzer does not call provider APIs. Run providers through Spec07 first
(`openai:<model>` now, Claude/Gemini adapters later), then compare the output
folders with `evals.analyze_results`.
