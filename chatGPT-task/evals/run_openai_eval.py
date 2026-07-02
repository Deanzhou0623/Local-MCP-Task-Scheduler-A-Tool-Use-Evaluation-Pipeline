"""Eval runner (spec 07 section 6).

Loads the dataset, and for each case: pins the clock to ``now``, resets an
isolated eval DB, runs the model with the scheduler tool surface, executes the
returned tool calls through the real dispatch registry, runs any fired immediate
job so a spec 04 scheduler trace exists to reference, grades both the tool call
and the real outcome, and writes the report artifacts.

    python -m evals.run_openai_eval \
        --dataset evals/datasets/scheduler_tool_use_v1.jsonl \
        --model local --prompt-version spec03 \
        --out evals/results/runs/local-demo
"""

from __future__ import annotations

import argparse
import json
import os
from datetime import datetime
from pathlib import Path

from evals.executor import (
    EvalEnv,
    call_public_tool,
    pin_clock,
    run_pending_run,
    run_seed,
    substitute,
    unpin_clock,
)
from evals.graders import failure_reason, grade_case, overall
from evals.judge import Judge, make_judge
from evals.models import make_model
from evals.prompts import system_prompt
from evals.report import write_reports


def load_dataset(path: str) -> list[dict]:
    cases = []
    dataset_path = Path(path)
    if not dataset_path.exists() and path.startswith("evals/"):
        dataset_path = Path(__file__).resolve().parents[1] / path
    with open(dataset_path) as f:
        for line in f:
            line = line.strip()
            if line:
                cases.append(json.loads(line))
    return cases


def _execute_tool_calls(env: EvalEnv, tool_calls: list[dict]) -> tuple[list[dict], list[str]]:
    """Run each model tool call; run fired immediate jobs to produce traces."""
    executed = []
    trace_ids: list[str] = []
    for tc in tool_calls:
        result = call_public_tool(tc["name"], dict(tc.get("arguments", {})))
        executed.append({"name": tc["name"], "arguments": tc.get("arguments", {}),
                         "result": result})
        if (isinstance(result, dict) and result.get("ok")
                and result.get("job", {}).get("type") == "immediate"
                and result.get("next_run", {}).get("run_id")):
            tid = run_pending_run(env, result["next_run"]["run_id"])
            if tid:
                trace_ids.append(tid)
    return executed, trace_ids


def run_case(model, case: dict, prompt_version: str, judge: Judge | None = None) -> dict:
    env = EvalEnv()
    try:
        pin_clock(case["now"])
        user_id = case.get("user_id", "eval-user")
        vars = run_seed(env, case.get("seed", []), user_id)
        prompt = substitute(case["prompt"], vars)
        expected = substitute(case.get("expected", {}), vars)
        case = {**case, "prompt": prompt, "expected": expected}

        now_dt = datetime.fromisoformat(case["now"])
        context = {"user_id": user_id, **case.get("context", {})}
        resp = model.run(prompt, now_dt, context)
        executed, trace_ids = _execute_tool_calls(env, resp.tool_calls)
        resp = model.finalize(prompt, now_dt, context, resp, executed)

        graders = grade_case(case, executed, resp.final_answer)
        passed, score = overall(graders)
        llm_judge = _judge_case(judge, case, expected, executed, resp.final_answer, passed)

        primary = executed[0] if executed else None
        ids = _extract_ids(executed)
        trace = {
            "case_id": case["id"],
            "model": model.name,
            "prompt_version": prompt_version,
            "tool_schema_version": "scheduler_tools_v1",
            "prompt": prompt,
            "now": case["now"],
            "tool_calls": executed,
            "created_job_ids": ids["job_ids"],
            "created_run_ids": ids["run_ids"],
            "scheduler_trace_ids": trace_ids,
            "final_answer": resp.final_answer,
            "deterministic_graders": graders,
            "llm_judge": llm_judge,
            "trace_track": bool(case.get("grading", {}).get("trace_track")),
            "safety_case": bool(case.get("grading", {}).get("safety_case")),
            "usage": resp.usage,
            "latency_ms": resp.latency_ms,
        }
        return {
            "case_id": case["id"],
            "category": case.get("category", "uncategorized"),
            "model": model.name,
            "passed": passed,
            "score": score,
            "graders": graders,
            "trace": trace,
            "expected_tool": expected.get("tool"),
            "actual_tool": primary["name"] if primary else "",
            "expected_action": expected.get("action", ""),
            "actual_action": (primary["arguments"].get("action", "") if primary else ""),
            "expected_time": expected.get("time", expected.get("schedule", "")),
            "actual_time": (primary["arguments"].get("time",
                            primary["arguments"].get("schedule", "")) if primary else ""),
            "error_code": (primary["result"].get("error", {}).get("code", "")
                           if primary and isinstance(primary["result"], dict)
                           and not primary["result"].get("ok") else ""),
            "latency_ms": resp.latency_ms,
            "input_tokens": resp.usage.get("input_tokens", 0),
            "output_tokens": resp.usage.get("output_tokens", 0),
            "failure_reason": failure_reason(graders),
            # Blank when the judge did not actually run (e.g. safety cases), so
            # a non-applicable row is not misread as "judge passed".
            "llm_judge_passed": llm_judge.get("passed") if llm_judge.get("applicable") else "",
            "llm_judge_score": llm_judge.get("score") if llm_judge.get("applicable") else "",
            "trace_track": bool(case.get("grading", {}).get("trace_track")),
            "safety_case": bool(case.get("grading", {}).get("safety_case")),
            "expected_args": {k: expected[k] for k in
                              ("action", "type", "time", "schedule", "timezone")
                              if k in expected},
        }
    finally:
        unpin_clock()
        env.dispose()


def _judge_case(
    judge: Judge | None,
    case: dict,
    expected: dict,
    executed: list[dict],
    final_answer: str,
    deterministic_passed: bool,
) -> dict:
    if not case.get("grading", {}).get("use_llm_judge"):
        return {
            "name": "llm_final_answer_judge",
            "passed": True,
            "score": 1.0,
            "reason": "Judge not configured for this case.",
            "applicable": False,
        }
    judge = judge or make_judge("local")
    return judge.grade(
        {
            "case_id": case["id"],
            "prompt": case["prompt"],
            "expected": expected,
            "actual_tool_calls": [
                {"name": tc["name"], "arguments": tc.get("arguments", {})}
                for tc in executed
            ],
            "tool_results": [tc.get("result", {}) for tc in executed],
            "final_answer": final_answer,
            "deterministic_passed": deterministic_passed,
        }
    )


def _extract_ids(executed: list[dict]) -> dict[str, list[str]]:
    job_ids: list[str] = []
    run_ids: list[str] = []
    for tc in executed:
        result = tc.get("result", {})
        if not isinstance(result, dict):
            continue
        job = result.get("job")
        if isinstance(job, dict) and job.get("job_id"):
            job_ids.append(job["job_id"])
        next_run = result.get("next_run")
        if isinstance(next_run, dict) and next_run.get("run_id"):
            run_ids.append(next_run["run_id"])
        runs = result.get("runs", [])
        if not isinstance(runs, list):
            runs = []
        for run in runs:
            if isinstance(run, dict) and run.get("run_id"):
                run_ids.append(run["run_id"])
    return {"job_ids": job_ids, "run_ids": run_ids}


def run_eval(dataset_path: str, model_spec: str, prompt_version: str,
             out_dir: str, judge_spec: str = "local") -> dict:
    cases = load_dataset(dataset_path)
    model = make_model(model_spec, system_prompt(prompt_version))
    judge = make_judge(judge_spec)
    records = [run_case(model, case, prompt_version, judge) for case in cases]
    run_meta = {
        "dataset": dataset_path,
        "model": model.name,
        "judge": judge.name,
        "prompt_version": prompt_version,
        "cases": len(cases),
    }
    return write_reports(out_dir, run_meta, records)


def main() -> None:
    p = argparse.ArgumentParser(description="Scheduler tool-use eval runner.")
    p.add_argument("--dataset", default="evals/datasets/scheduler_tool_use_v1.jsonl")
    p.add_argument("--model", default="local",
                   help="'local' or 'openai:<model>' (needs OPENAI_API_KEY).")
    p.add_argument("--judge-model", default="local",
                   help="'none', 'local', or 'openai:<model>' for final-answer judging.")
    p.add_argument("--prompt-version", default="spec03",
                   choices=["baseline", "spec03", "short", "long"])
    p.add_argument("--out", default=None, help="Output run directory.")
    args = p.parse_args()

    out_dir = args.out or os.path.join(
        "evals", "results", "runs",
        f"{args.model.replace(':', '-')}-{datetime.now().strftime('%Y%m%d-%H%M%S')}")
    summary = run_eval(args.dataset, args.model, args.prompt_version, out_dir, args.judge_model)
    print(f"Ran {summary['total']} cases | pass_rate={summary['pass_rate']} "
          f"| mean_score={summary['mean_score']}")
    print(f"Reports written to {out_dir}")


if __name__ == "__main__":
    main()
