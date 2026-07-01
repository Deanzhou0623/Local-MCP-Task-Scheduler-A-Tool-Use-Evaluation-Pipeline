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
| `datasets/scheduler_tool_use_v1.jsonl` | 22 hand-written cases across every category |
| `executor.py` | isolated eval DB + clock pin; runs tool calls through the real MCP `dispatch` |
| `tool_schemas.py` | MCP public schemas → OpenAI Responses tool defs |
| `models.py` | `HeuristicModel` (default, no key) and optional `OpenAIModel` |
| `graders.py` | 11 deterministic graders + overall verdict |
| `report.py` | writes `summary.json`, `results.csv`, `traces.jsonl`, `openai_evals_payload.jsonl` |
| `run_openai_eval.py` | CLI / `run_eval()` orchestrator |
| `prompts.py` | prompt variants for A/B comparison |

## Run

```bash
cd chatGPT-task
python -m evals.run_openai_eval --model local --prompt-version spec03 \
  --out evals/results/runs/local-demo
```

- `--model local` (default) uses the deterministic `HeuristicModel` — no API key,
  fully offline, and *genuinely fallible* so the report shows a real pass rate.
- `--model openai:<model>` calls the real Responses API (needs `OPENAI_API_KEY`)
  with the same tool schemas.
- `--prompt-version` ∈ `baseline | spec03 | short | long`.

Outputs land in the `--out` run directory:

- `results.csv` — flat rows for pandas / Spec 08 analysis.
- `traces.jsonl` — full model traces (tool calls, results, `scheduler_trace_ids`).
- `summary.json` — pass rate by category and by grader.
- `openai_evals_payload.jsonl` — flattened items for a hosted OpenAI LLM-judge
  pass over final-answer quality (wired, run separately when a key is available).

## Dataset format

Each JSONL row pins `now`, gives an explicit `expected` outcome, and (for
local-time cases) an ambient `context.timezone` the way a real client injects it.
Stateful categories (`get`/`modify`/`delete`/`read_runs`/`read_trace`) use a
`seed` list of oracle calls run before the graded turn; `{{job_id}}` /
`{{trace_id}}` placeholders in the prompt/expected are substituted from the seed.

Positive **and** negative cases are included (ask for a missing timezone; decline
out-of-scope; surface `NOT_FOUND`), per the article's balance guidance.

## Environment

```txt
OPENAI_API_KEY=      # only for --model openai:<model> and the hosted judge
```

## Not in scope (Spec 08)

Failure clustering, prompt/schema A/B comparison across runs, and cross-provider
(Claude/Gemini) comparisons.
