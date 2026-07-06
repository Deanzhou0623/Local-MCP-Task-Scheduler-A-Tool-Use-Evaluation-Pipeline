"""Re-run the LLM-as-judge over existing Spec07 run folders (spec 07/08).

Deterministic tool grading is fixed once a run exists, but the final-answer judge
can be swapped or re-run cheaply — it only needs the recorded final answers and
tool results, not another (expensive) model call. This rewrites ``traces.jsonl``
and the ``llm_judge_*`` columns of ``results.csv`` in place; re-run
``analyze_results`` afterward to refresh the leaderboard.

    python -m evals.rejudge --runs evals/results/runs/flagship-openai \
        --judge-model anthropic:claude-opus-4-8
"""

from __future__ import annotations

import argparse
import csv
import json
from pathlib import Path

from dotenv import find_dotenv, load_dotenv

from evals.judge import make_judge
from evals.report import CSV_COLUMNS
from evals.run_openai_eval import load_dataset


def rejudge_run(run_dir: str | Path, judge_spec: str, dataset_path: str) -> dict:
    """Re-judge one run folder in place; return {rejudged, judge}."""
    run = Path(run_dir)
    judge = make_judge(judge_spec)
    dataset = {c["id"]: c for c in load_dataset(dataset_path)}

    traces = [json.loads(l) for l in open(run / "traces.jsonl") if l.strip()]
    with open(run / "results.csv", newline="") as f:
        rows = list(csv.DictReader(f))
    rows_by_id = {r["case_id"]: r for r in rows}

    rejudged = 0
    for t in traces:
        if not t.get("llm_judge", {}).get("applicable"):
            continue
        case = dataset.get(t["case_id"], {})
        payload = {
            "case_id": t["case_id"],
            "prompt": t["prompt"],
            "expected": case.get("expected", {}),
            "actual_tool_calls": [
                {"name": tc["name"], "arguments": tc.get("arguments", {})}
                for tc in t["tool_calls"]
            ],
            "tool_results": [tc.get("result", {}) for tc in t["tool_calls"]],
            "final_answer": t["final_answer"],
            "deterministic_passed": rows_by_id[t["case_id"]]["passed"] == "True",
        }
        verdict = judge.grade(payload)
        t["llm_judge"] = verdict
        row = rows_by_id[t["case_id"]]
        row["llm_judge_passed"] = verdict["passed"] if verdict["applicable"] else ""
        row["llm_judge_score"] = verdict["score"] if verdict["applicable"] else ""
        rejudged += 1

    with open(run / "traces.jsonl", "w") as f:
        for t in traces:
            f.write(json.dumps(t) + "\n")
    with open(run / "results.csv", "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=CSV_COLUMNS)
        w.writeheader()
        w.writerows(rows)

    # Record the judge used, for provenance.
    summary_path = run / "summary.json"
    if summary_path.exists():
        summary = json.load(open(summary_path))
        summary.setdefault("run", {})["judge"] = judge.name
        json.dump(summary, open(summary_path, "w"), indent=2)

    return {"run": run.name, "rejudged": rejudged, "judge": judge.name}


def main() -> None:
    p = argparse.ArgumentParser(description="Re-judge existing eval runs.")
    p.add_argument("--runs", nargs="+", required=True)
    p.add_argument("--judge-model", required=True,
                   help="'local', 'openai:<id>', 'anthropic:<id>', or 'gemini:<id>'.")
    p.add_argument("--dataset", default="evals/datasets/scheduler_tool_use_v1.jsonl")
    args = p.parse_args()
    load_dotenv(find_dotenv(usecwd=True))
    for run in args.runs:
        info = rejudge_run(run, args.judge_model, args.dataset)
        print(f"{info['run']}: rejudged {info['rejudged']} cases with {info['judge']}")


if __name__ == "__main__":
    main()
