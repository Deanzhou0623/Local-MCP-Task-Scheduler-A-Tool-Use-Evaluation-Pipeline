"""Spec 08 offline analysis for Spec 07 eval run artifacts.

The analyzer never calls a model or the scheduler. It loads one or more run
directories produced by ``evals.run_openai_eval`` and writes comparison reports
that explain where the evals failed and what to improve next.
"""

from __future__ import annotations

import argparse
import csv
import json
import os
from collections import Counter, defaultdict
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from statistics import mean, median
from typing import Any

REQUIRED_FILES = {
    "summary.json",
    "results.csv",
    "traces.jsonl",
    "openai_evals_payload.jsonl",
}

REQUIRED_RESULT_COLUMNS = {
    "case_id", "category", "model", "passed", "score",
    "expected_tool", "actual_tool", "expected_action", "actual_action",
    "expected_time", "actual_time", "error_code", "latency_ms",
    "input_tokens", "output_tokens", "failure_reason",
    "llm_judge_passed", "llm_judge_score", "trace_track", "safety_case",
}

REQUIRED_TRACE_FIELDS = {
    "case_id", "model", "prompt_version", "tool_schema_version", "prompt",
    "now", "tool_calls", "created_job_ids", "created_run_ids",
    "scheduler_trace_ids", "final_answer", "deterministic_graders",
    "llm_judge", "trace_track", "safety_case", "usage", "latency_ms",
}

TOOL_GRADERS = [
    "tool_name_match",
    "required_tool_called",
    "no_unexpected_tool",
    "json_args_valid",
    "action_match",
    "job_type_match",
    "time_match",
    "timezone_match",
    "action_params_match",
    "tool_result_success",
]

SAFETY_GRADERS = [
    "expected_user_id_used",
    "forbidden_user_id_not_used",
    "no_secret_leak",
    "no_unsupported_action",
    "no_false_execution_claim",
    "safe_refusal_present",
]

FINAL_ANSWER_GRADERS = [
    "final_answer_consistent",
    "no_false_success_claim",
    "past_time_acknowledged",
]


@dataclass(frozen=True)
class RunArtifacts:
    run_id: str
    path: Path
    summary: dict[str, Any]
    rows: list[dict[str, str]]
    traces: list[dict[str, Any]]

    @property
    def model(self) -> str:
        return (
            self.summary.get("run", {}).get("model")
            or (self.rows[0].get("model") if self.rows else "")
            or self.run_id
        )

    @property
    def prompt_version(self) -> str:
        return self.summary.get("run", {}).get("prompt_version", "")


def analyze_runs(run_dirs: list[str | Path], out_dir: str | Path) -> dict[str, Any]:
    """Load run folders, compute Spec08 reports, and write them to ``out_dir``."""
    runs = [load_run(Path(p)) for p in run_dirs]
    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)

    run_metrics = [_run_metrics(r) for r in runs]
    _add_relative_efficiency_scores(run_metrics)
    leaderboard = _leaderboard(run_metrics, runs)
    category_rows = _category_breakdown(runs)
    grader_rows = _grader_breakdown(runs)
    trace_report = _trace_track_report(runs)
    clusters = _failure_clusters(runs)
    recommendations = _recommendations(clusters)

    analysis = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "runs": run_metrics,
        "leader": leaderboard[0]["model"] if leaderboard else None,
        "model_count": len({m["model"] for m in run_metrics}),
        "case_count": sum(m["total_cases"] for m in run_metrics),
        "top_failure_types": [c["failure_type"] for c in clusters[:5]],
        "recommendations": recommendations,
    }

    _write_json(out / "analysis_summary.json", analysis)
    _write_csv(out / "model_leaderboard.csv", leaderboard)
    _write_csv(out / "category_breakdown.csv", category_rows)
    _write_csv(out / "grader_breakdown.csv", grader_rows)
    _write_json(out / "failure_clusters.json", clusters)
    _write_csv(out / "failure_clusters.csv", _cluster_csv_rows(clusters))
    _write_json(out / "trace_track_report.json", trace_report)
    failures = _all_failures(runs)
    _write_jsonl(out / "all_failures.jsonl", failures)
    _write_csv(out / "all_failures.csv", [_failure_csv_row(f) for f in failures])
    _write_recommendations(out / "recommendations.md", leaderboard, clusters, recommendations)
    analysis["failure_count"] = len(failures)
    return analysis


def _all_failures(runs: list[RunArtifacts]) -> list[dict[str, Any]]:
    """Every failing case across all runs, with full detail for review."""
    out = []
    for run in runs:
        trace_by_case = {t["case_id"]: t for t in run.traces}
        for row in run.rows:
            if _bool(row["passed"]):
                continue
            trace = trace_by_case[row["case_id"]]
            failed = [g["name"] for g in trace["deterministic_graders"]
                      if g.get("applicable") and not g.get("passed")]
            out.append({
                "run_id": run.run_id,
                "model": run.model,
                "case_id": row["case_id"],
                "category": row["category"],
                "safety_case": _bool(row["safety_case"]),
                "trace_track": _bool(row["trace_track"]),
                "failed_graders": failed,
                "expected_tool": row["expected_tool"],
                "actual_tool": row["actual_tool"],
                "expected_action": row["expected_action"],
                "actual_action": row["actual_action"],
                "expected_time": row["expected_time"],
                "actual_time": row["actual_time"],
                "error_code": row["error_code"],
                "failure_reason": row["failure_reason"],
                "prompt": trace["prompt"],
                "final_answer": trace["final_answer"],
                "tool_calls": trace["tool_calls"],
            })
    return out


def _failure_csv_row(f: dict[str, Any]) -> dict[str, Any]:
    return {
        "run_id": f["run_id"], "model": f["model"], "case_id": f["case_id"],
        "category": f["category"], "safety_case": f["safety_case"],
        "trace_track": f["trace_track"], "failed_graders": ",".join(f["failed_graders"]),
        "expected_tool": f["expected_tool"], "actual_tool": f["actual_tool"],
        "expected_time": f["expected_time"], "actual_time": f["actual_time"],
        "error_code": f["error_code"], "failure_reason": f["failure_reason"],
    }


def load_run(path: Path) -> RunArtifacts:
    missing = sorted(name for name in REQUIRED_FILES if not (path / name).exists())
    if missing:
        raise ValueError(f"{path}: missing required artifact(s): {', '.join(missing)}")

    with open(path / "summary.json") as f:
        summary = json.load(f)

    with open(path / "results.csv", newline="") as f:
        reader = csv.DictReader(f)
        if reader.fieldnames is None:
            raise ValueError(f"{path}/results.csv: missing header")
        missing_cols = sorted(REQUIRED_RESULT_COLUMNS - set(reader.fieldnames))
        if missing_cols:
            raise ValueError(
                f"{path}/results.csv: missing required column(s): {', '.join(missing_cols)}"
            )
        rows = list(reader)

    traces: list[dict[str, Any]] = []
    with open(path / "traces.jsonl") as f:
        for line_no, line in enumerate(f, 1):
            line = line.strip()
            if not line:
                continue
            trace = json.loads(line)
            missing_fields = sorted(REQUIRED_TRACE_FIELDS - set(trace))
            if missing_fields:
                raise ValueError(
                    f"{path}/traces.jsonl:{line_no}: missing required field(s): "
                    f"{', '.join(missing_fields)}"
                )
            traces.append(trace)

    row_ids = {r["case_id"] for r in rows}
    trace_ids = {t["case_id"] for t in traces}
    if row_ids != trace_ids:
        raise ValueError(
            f"{path}: results.csv and traces.jsonl case ids differ "
            f"(rows={len(row_ids)}, traces={len(trace_ids)})"
        )
    return RunArtifacts(path.name, path, summary, rows, traces)


def _run_metrics(run: RunArtifacts) -> dict[str, Any]:
    rows = run.rows
    traces = run.traces
    total = len(rows)
    passed = sum(_bool(r["passed"]) for r in rows)
    latencies = [_float(r["latency_ms"]) for r in rows]
    input_tokens = sum(_int(r["input_tokens"]) for r in rows)
    output_tokens = sum(_int(r["output_tokens"]) for r in rows)
    llm_scores = [_float(r["llm_judge_score"]) for r in rows if r["llm_judge_score"] != ""]
    safety_rows = [r for r in rows if _bool(r["safety_case"])]
    trace_rows = [r for r in rows if _bool(r["trace_track"])]
    grader_rates = _grader_rates(traces)

    metrics: dict[str, Any] = {
        "run_id": run.run_id,
        "model": run.model,
        "prompt_version": run.prompt_version,
        "path": str(run.path),
        "total_cases": total,
        "passed_cases": passed,
        "pass_rate": _rate(passed, total),
        "mean_score": _avg(_float(r["score"]) for r in rows),
        "mean_llm_judge_score": _avg(llm_scores),
        "llm_judge_pass_rate": _rate(
            sum(_bool(r["llm_judge_passed"]) for r in rows if r["llm_judge_passed"] != ""),
            sum(1 for r in rows if r["llm_judge_passed"] != ""),
        ),
        "mean_latency_ms": round(_avg(latencies), 4),
        "p50_latency_ms": round(median(latencies), 4) if latencies else 0.0,
        "p95_latency_ms": _percentile(latencies, 95),
        "input_tokens_total": input_tokens,
        "output_tokens_total": output_tokens,
        "safety_cases": len(safety_rows),
        "safety_pass_rate": _rate(sum(_bool(r["passed"]) for r in safety_rows), len(safety_rows)),
        "trace_track_total": len(trace_rows),
        "trace_track_pass_rate": _rate(sum(_bool(r["passed"]) for r in trace_rows), len(trace_rows)),
        "time_accuracy": grader_rates.get("time_match", 1.0),
    }
    for name in TOOL_GRADERS:
        metrics[f"{name}_rate"] = grader_rates.get(name, 1.0)
    for name in SAFETY_GRADERS:
        metrics[f"{name}_rate"] = grader_rates.get(name, 1.0)
    for name in FINAL_ANSWER_GRADERS:
        metrics[f"{name}_rate"] = grader_rates.get(name, 1.0)
    return metrics


def _category_breakdown(runs: list[RunArtifacts]) -> list[dict[str, Any]]:
    grouped: dict[tuple[str, str, str], list[dict[str, str]]] = defaultdict(list)
    for run in runs:
        for row in run.rows:
            grouped[(run.run_id, run.model, row["category"])].append(row)
    out = []
    for (run_id, model, category), rows in sorted(grouped.items()):
        out.append({
            "run_id": run_id,
            "model": model,
            "category": category,
            "total": len(rows),
            "passed": sum(_bool(r["passed"]) for r in rows),
            "pass_rate": _rate(sum(_bool(r["passed"]) for r in rows), len(rows)),
            "mean_score": _avg(_float(r["score"]) for r in rows),
        })
    return out


def _grader_breakdown(runs: list[RunArtifacts]) -> list[dict[str, Any]]:
    grouped: dict[tuple[str, str, str], list[dict[str, Any]]] = defaultdict(list)
    for run in runs:
        for trace in run.traces:
            for grader in trace["deterministic_graders"]:
                if grader.get("applicable"):
                    grouped[(run.run_id, run.model, grader["name"])].append(grader)
    out = []
    for (run_id, model, grader), items in sorted(grouped.items()):
        passed = sum(bool(g.get("passed")) for g in items)
        out.append({
            "run_id": run_id,
            "model": model,
            "grader": grader,
            "total": len(items),
            "passed": passed,
            "pass_rate": _rate(passed, len(items)),
        })
    return out


def _trace_track_report(runs: list[RunArtifacts]) -> dict[str, Any]:
    by_run = []
    cases = []
    for run in runs:
        tracked = [t for t in run.traces if t["trace_track"]]
        for trace in tracked:
            row = _row_by_id(run, trace["case_id"])
            cases.append({
                "run_id": run.run_id,
                "model": run.model,
                "case_id": trace["case_id"],
                "passed": _bool(row["passed"]),
                "tool_call_count": len(trace["tool_calls"]),
                "created_job_ids": trace["created_job_ids"],
                "created_run_ids": trace["created_run_ids"],
                "scheduler_trace_ids": trace["scheduler_trace_ids"],
                "failure_reason": row["failure_reason"],
            })
        by_run.append({
            "run_id": run.run_id,
            "model": run.model,
            "trace_track_total": len(tracked),
            "trace_track_pass_rate": _rate(
                sum(_bool(_row_by_id(run, t["case_id"])["passed"]) for t in tracked),
                len(tracked),
            ),
            "avg_tool_calls_per_case": _avg(len(t["tool_calls"]) for t in tracked),
            "cases_with_job_id": sum(bool(t["created_job_ids"]) for t in tracked),
            "cases_with_run_id": sum(bool(t["created_run_ids"]) for t in tracked),
            "cases_with_scheduler_trace_id": sum(bool(t["scheduler_trace_ids"]) for t in tracked),
            "trace_track_failure_cases": [
                t["case_id"] for t in tracked
                if not _bool(_row_by_id(run, t["case_id"])["passed"])
            ],
        })
    return {"runs": by_run, "cases": cases}


def _failure_clusters(runs: list[RunArtifacts]) -> list[dict[str, Any]]:
    buckets: dict[tuple[Any, ...], dict[str, Any]] = {}
    for run in runs:
        trace_by_case = {t["case_id"]: t for t in run.traces}
        for row in run.rows:
            if _bool(row["passed"]):
                continue
            trace = trace_by_case[row["case_id"]]
            failed_graders = [
                g for g in trace["deterministic_graders"]
                if g.get("applicable") and not g.get("passed")
            ] or [{"name": "unknown", "reason": row["failure_reason"]}]
            for grader in failed_graders:
                key = (
                    row["category"],
                    grader["name"],
                    row["expected_tool"],
                    row["actual_tool"],
                    row["expected_action"],
                    row["actual_action"],
                    row["expected_time"],
                    row["actual_time"],
                    row["error_code"],
                    row["safety_case"],
                    row["trace_track"],
                )
                cluster = buckets.setdefault(key, {
                    "failure_type": f"{row['category']}::{grader['name']}",
                    "category": row["category"],
                    "grader": grader["name"],
                    "expected_tool": row["expected_tool"],
                    "actual_tool": row["actual_tool"],
                    "expected_action": row["expected_action"],
                    "actual_action": row["actual_action"],
                    "expected_time": row["expected_time"],
                    "actual_time": row["actual_time"],
                    "error_code": row["error_code"],
                    "safety_case": _bool(row["safety_case"]),
                    "trace_track": _bool(row["trace_track"]),
                    "affected_models": set(),
                    "affected_categories": set(),
                    "case_ids": [],
                    "example_prompt": trace["prompt"],
                    "example_failure_reason": grader.get("reason") or row["failure_reason"],
                    "suggested_owner": _suggest_owner(grader["name"], row),
                })
                cluster["affected_models"].add(run.model)
                cluster["affected_categories"].add(row["category"])
                cluster["case_ids"].append(row["case_id"])

    clusters = []
    for index, cluster in enumerate(
        sorted(buckets.values(), key=lambda c: (-len(c["case_ids"]), c["failure_type"])),
        1,
    ):
        cluster = dict(cluster)
        cluster["cluster_id"] = f"cluster_{index:03d}"
        cluster["count"] = len(cluster["case_ids"])
        cluster["affected_models"] = sorted(cluster["affected_models"])
        cluster["affected_categories"] = sorted(cluster["affected_categories"])
        cluster["case_ids"] = sorted(set(cluster["case_ids"]))
        clusters.append(cluster)
    return clusters


def _leaderboard(run_metrics: list[dict[str, Any]], runs: list[RunArtifacts]) -> list[dict[str, Any]]:
    top_failure = _top_failure_by_run(runs)
    rows = []
    for m in run_metrics:
        weighted = (
            m["pass_rate"] * 0.50
            + m["safety_pass_rate"] * 0.20
            + m["time_accuracy"] * 0.10
            + m["mean_llm_judge_score"] * 0.10
            + m["latency_score"] * 0.05
            + m["token_efficiency_score"] * 0.05
        )
        rows.append({
            "rank": 0,
            "model": m["model"],
            "run_id": m["run_id"],
            "weighted_score": round(weighted, 4),
            "pass_rate": m["pass_rate"],
            "safety_pass_rate": m["safety_pass_rate"],
            "time_match_rate": m["time_match_rate"],
            "mean_llm_judge_score": m["mean_llm_judge_score"],
            "p95_latency_ms": m["p95_latency_ms"],
            "input_tokens_total": m["input_tokens_total"],
            "output_tokens_total": m["output_tokens_total"],
            "top_failure_type": top_failure.get(m["run_id"], ""),
        })
    rows.sort(key=lambda r: (-r["weighted_score"], -r["pass_rate"], r["p95_latency_ms"]))
    for idx, row in enumerate(rows, 1):
        row["rank"] = idx
    return rows


def _recommendations(clusters: list[dict[str, Any]]) -> list[dict[str, Any]]:
    out = []
    for cluster in clusters[:10]:
        grader = cluster["grader"]
        if grader == "time_match":
            change = "Tighten relative-time, timezone, and recurrence wording in the system prompt or dataset."
            metric = "time_match_rate"
        elif grader in {"no_false_success_claim", "past_time_acknowledged"}:
            change = ("Instruct the model to acknowledge tool errors and past/invalid "
                      "times honestly instead of asserting the action succeeded.")
            metric = f"{grader}_rate"
        elif grader in SAFETY_GRADERS:
            change = "Strengthen the current-user safety instruction and keep cross-user cases in the dataset."
            metric = "safety_pass_rate"
        elif grader in {"tool_name_match", "action_match", "json_args_valid"}:
            change = "Clarify tool/action descriptions and keep action enums strict."
            metric = f"{grader}_rate"
        elif grader == "tool_result_success":
            change = "Inspect scheduler backend behavior or seed setup for this case."
            metric = "tool_result_success_rate"
        elif grader == "final_answer_consistent":
            change = "Improve final-answer rubric or prompt the model to report only completed external effects."
            metric = "mean_llm_judge_score"
        else:
            change = "Review the example prompt and expected outcome, then update dataset or prompt wording."
            metric = f"{grader}_rate"
        out.append({
            "source_cluster": cluster["cluster_id"],
            "affected_cases": cluster["case_ids"],
            "proposed_change": change,
            "expected_metric_to_improve": metric,
        })
    return out


def _write_recommendations(
    path: Path,
    leaderboard: list[dict[str, Any]],
    clusters: list[dict[str, Any]],
    recommendations: list[dict[str, Any]],
) -> None:
    lines = ["# Eval Analysis", "", "## Leaderboard", ""]
    if leaderboard:
        lines.append("| Rank | Model | Weighted | Pass Rate | Safety | Time |")
        lines.append("| --- | --- | ---: | ---: | ---: | ---: |")
        for row in leaderboard:
            lines.append(
                f"| {row['rank']} | {row['model']} | {row['weighted_score']} | "
                f"{row['pass_rate']} | {row['safety_pass_rate']} | {row['time_match_rate']} |"
            )
    else:
        lines.append("No runs analyzed.")
    lines.extend(["", "## Main Failure Clusters", ""])
    for cluster in clusters[:8]:
        lines.append(
            f"- `{cluster['cluster_id']}` `{cluster['failure_type']}`: "
            f"{cluster['count']} case(s), owner `{cluster['suggested_owner']}`, "
            f"cases {', '.join(cluster['case_ids'])}."
        )
    if not clusters:
        lines.append("- No deterministic failures found.")
    lines.extend(["", "## Recommended Next Changes", ""])
    for rec in recommendations:
        lines.append(
            f"- From `{rec['source_cluster']}`: {rec['proposed_change']} "
            f"Expected metric: `{rec['expected_metric_to_improve']}`. "
            f"Cases: {', '.join(rec['affected_cases'])}."
        )
    if not recommendations:
        lines.append("- Keep this run as the current baseline and compare future prompt/schema changes against it.")
    path.write_text("\n".join(lines) + "\n")


def _cluster_csv_rows(clusters: list[dict[str, Any]]) -> list[dict[str, Any]]:
    rows = []
    for c in clusters:
        rows.append({
            "cluster_id": c["cluster_id"],
            "failure_type": c["failure_type"],
            "count": c["count"],
            "affected_models": ",".join(c["affected_models"]),
            "affected_categories": ",".join(c["affected_categories"]),
            "case_ids": ",".join(c["case_ids"]),
            "example_failure_reason": c["example_failure_reason"],
            "suggested_owner": c["suggested_owner"],
        })
    return rows


def _top_failure_by_run(runs: list[RunArtifacts]) -> dict[str, str]:
    out = {}
    for run in runs:
        failures = [
            g["name"]
            for trace in run.traces
            for g in trace["deterministic_graders"]
            if g.get("applicable") and not g.get("passed")
        ]
        if failures:
            out[run.run_id] = Counter(failures).most_common(1)[0][0]
    return out


def _grader_rates(traces: list[dict[str, Any]]) -> dict[str, float]:
    totals: dict[str, int] = defaultdict(int)
    passed: dict[str, int] = defaultdict(int)
    for trace in traces:
        for grader in trace["deterministic_graders"]:
            if grader.get("applicable"):
                totals[grader["name"]] += 1
                if grader.get("passed"):
                    passed[grader["name"]] += 1
    return {name: _rate(passed[name], total) for name, total in totals.items()}


def _add_relative_efficiency_scores(metrics: list[dict[str, Any]]) -> None:
    if len(metrics) <= 1:
        for m in metrics:
            m["latency_score"] = 1.0
            m["token_efficiency_score"] = 1.0
        return
    max_latency = max((m["p95_latency_ms"] for m in metrics), default=0.0)
    max_tokens = max((m["input_tokens_total"] + m["output_tokens_total"] for m in metrics), default=0)
    for m in metrics:
        m["latency_score"] = _inverse_score(m["p95_latency_ms"], max_latency)
        token_total = m["input_tokens_total"] + m["output_tokens_total"]
        m["token_efficiency_score"] = _inverse_score(token_total, max_tokens)


def _suggest_owner(grader: str, row: dict[str, str]) -> str:
    if grader in SAFETY_GRADERS:
        return "system_prompt"
    if grader in {"tool_name_match", "action_match", "json_args_valid"}:
        return "tool_schema"
    if grader in {"time_match", "timezone_match"}:
        return "system_prompt"
    if grader == "tool_result_success":
        return "scheduler_backend" if not row["error_code"] else "dataset"
    # Deterministic honesty graders: the model claimed success on an error / a
    # past time. That is a prompt/behavior issue, not the judge's fault.
    if grader in {"no_false_success_claim", "past_time_acknowledged"}:
        return "system_prompt"
    if grader == "final_answer_consistent":
        return "judge_quality"
    return "model_behavior"


def _row_by_id(run: RunArtifacts, case_id: str) -> dict[str, str]:
    for row in run.rows:
        if row["case_id"] == case_id:
            return row
    raise KeyError(case_id)


def _write_json(path: Path, data: Any) -> None:
    with open(path, "w") as f:
        json.dump(data, f, indent=2)


def _write_jsonl(path: Path, rows: list[dict[str, Any]]) -> None:
    with open(path, "w") as f:
        for row in rows:
            f.write(json.dumps(row) + "\n")


def _write_csv(path: Path, rows: list[dict[str, Any]]) -> None:
    if not rows:
        path.write_text("")
        return
    # Preserve the intended column order (rows are built in spec order) rather
    # than alphabetizing; append any stray keys defensively.
    fieldnames = list(rows[0].keys())
    for row in rows:
        for key in row:
            if key not in fieldnames:
                fieldnames.append(key)
    with open(path, "w", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(rows)


def _bool(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    return str(value).strip().lower() == "true"


def _float(value: Any) -> float:
    if value in (None, ""):
        return 0.0
    return float(value)


def _int(value: Any) -> int:
    if value in (None, ""):
        return 0
    return int(float(value))


def _rate(passed: int, total: int) -> float:
    return round(passed / total, 4) if total else 1.0


def _avg(values) -> float:
    vals = list(values)
    return round(mean(vals), 4) if vals else 0.0


def _percentile(values: list[float], pct: int) -> float:
    if not values:
        return 0.0
    vals = sorted(values)
    idx = int(round((pct / 100) * (len(vals) - 1)))
    return round(vals[idx], 4)


def _inverse_score(value: float, max_value: float) -> float:
    if max_value <= 0:
        return 1.0
    return round(max(0.0, 1.0 - (value / max_value)), 4)


def main() -> None:
    parser = argparse.ArgumentParser(description="Analyze scheduler eval run artifacts.")
    parser.add_argument("--runs", nargs="+", required=True, help="Spec07 run directories.")
    parser.add_argument("--out", required=True, help="Directory for Spec08 analysis reports.")
    args = parser.parse_args()

    summary = analyze_runs(args.runs, args.out)
    print(
        f"Analyzed {len(summary['runs'])} run(s) | "
        f"leader={summary['leader']} | output={args.out}"
    )


if __name__ == "__main__":
    main()
