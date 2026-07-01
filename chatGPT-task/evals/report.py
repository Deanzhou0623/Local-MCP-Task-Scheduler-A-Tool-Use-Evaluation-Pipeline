"""Eval report writers (spec 07 section 11).

Produces four artifacts per run:

- ``traces.jsonl``           full model/tool-call traces (JSONL preserves detail)
- ``results.csv``            flat rows for pandas / spec 08 analysis
- ``summary.json``           aggregate pass rate by category and grader
- ``openai_evals_payload.jsonl`` flattened items for a hosted LLM-judge pass
"""

from __future__ import annotations

import csv
import json
import os
from collections import defaultdict

CSV_COLUMNS = [
    "case_id", "category", "model", "passed", "score",
    "expected_tool", "actual_tool", "expected_action", "actual_action",
    "expected_time", "actual_time", "error_code", "latency_ms",
    "input_tokens", "output_tokens", "failure_reason",
]


def write_reports(out_dir: str, run_meta: dict, records: list[dict]) -> dict:
    os.makedirs(out_dir, exist_ok=True)
    _write_traces(out_dir, records)
    _write_csv(out_dir, records)
    _write_openai_payload(out_dir, records)
    summary = _summary(run_meta, records)
    with open(os.path.join(out_dir, "summary.json"), "w") as f:
        json.dump(summary, f, indent=2)
    return summary


def _write_traces(out_dir: str, records: list[dict]) -> None:
    with open(os.path.join(out_dir, "traces.jsonl"), "w") as f:
        for r in records:
            f.write(json.dumps(r["trace"]) + "\n")


def _write_csv(out_dir: str, records: list[dict]) -> None:
    with open(os.path.join(out_dir, "results.csv"), "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=CSV_COLUMNS)
        w.writeheader()
        for r in records:
            w.writerow({k: r.get(k, "") for k in CSV_COLUMNS})


def _write_openai_payload(out_dir: str, records: list[dict]) -> None:
    with open(os.path.join(out_dir, "openai_evals_payload.jsonl"), "w") as f:
        for r in records:
            t = r["trace"]
            f.write(json.dumps({
                "case_id": r["case_id"],
                "prompt": t["prompt"],
                "expected_tool": r.get("expected_tool"),
                "expected_args": r.get("expected_args", {}),
                "actual_tool_calls": [{"name": tc["name"], "arguments": tc["arguments"]}
                                      for tc in t["tool_calls"]],
                "tool_result": t["tool_calls"][0]["result"] if t["tool_calls"] else None,
                "final_answer": t["final_answer"],
                "local_passed": r["passed"],
            }) + "\n")


def _summary(run_meta: dict, records: list[dict]) -> dict:
    total = len(records)
    passed = sum(1 for r in records if r["passed"])
    by_cat: dict[str, list[bool]] = defaultdict(list)
    by_grader_pass: dict[str, int] = defaultdict(int)
    by_grader_total: dict[str, int] = defaultdict(int)
    for r in records:
        by_cat[r.get("category", "uncategorized")].append(r["passed"])
        for g in r["graders"]:
            if g["applicable"]:
                by_grader_total[g["name"]] += 1
                if g["passed"]:
                    by_grader_pass[g["name"]] += 1
    return {
        "run": run_meta,
        "total": total,
        "passed": passed,
        "pass_rate": round(passed / total, 4) if total else 0.0,
        "mean_score": round(sum(r["score"] for r in records) / total, 4) if total else 0.0,
        "by_category": {c: {"total": len(v), "passed": sum(v),
                            "pass_rate": round(sum(v) / len(v), 4)}
                        for c, v in sorted(by_cat.items())},
        "by_grader": {g: {"total": by_grader_total[g], "passed": by_grader_pass[g],
                          "pass_rate": round(by_grader_pass[g] / by_grader_total[g], 4)}
                      for g in sorted(by_grader_total)},
    }
