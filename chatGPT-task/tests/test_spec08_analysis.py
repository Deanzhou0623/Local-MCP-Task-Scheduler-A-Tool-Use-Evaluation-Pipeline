from __future__ import annotations

import csv
import json
import os

import pytest

from evals.analyze_results import analyze_runs, load_run
from evals.run_openai_eval import run_eval

DATASET = "evals/datasets/scheduler_tool_use_v1.jsonl"


def test_spec08_analyzer_writes_comparison_artifacts(tmp_path):
    run_dir = tmp_path / "runs" / "local-demo"
    out_dir = tmp_path / "analysis" / "compare-001"
    run_eval(DATASET, "local", "spec03", str(run_dir))

    summary = analyze_runs([run_dir], out_dir)

    for name in (
        "analysis_summary.json",
        "model_leaderboard.csv",
        "category_breakdown.csv",
        "grader_breakdown.csv",
        "failure_clusters.json",
        "failure_clusters.csv",
        "trace_track_report.json",
        "recommendations.md",
    ):
        assert os.path.exists(out_dir / name), name

    assert summary["leader"] == "local-heuristic"
    assert summary["case_count"] == 40
    assert summary["top_failure_types"]

    with open(out_dir / "model_leaderboard.csv") as f:
        leaderboard = list(csv.DictReader(f))
    assert leaderboard[0]["rank"] == "1"
    assert leaderboard[0]["model"] == "local-heuristic"
    assert float(leaderboard[0]["weighted_score"]) > 0

    with open(out_dir / "failure_clusters.json") as f:
        clusters = json.load(f)
    assert any("create_recurring_hard_biweekly_009" in c["case_ids"] for c in clusters)

    with open(out_dir / "trace_track_report.json") as f:
        trace_report = json.load(f)
    assert trace_report["runs"][0]["trace_track_total"] == 10
    assert trace_report["runs"][0]["cases_with_job_id"] > 0

    text = (out_dir / "recommendations.md").read_text()
    assert "# Eval Analysis" in text
    assert "Recommended Next Changes" in text


def test_spec08_analyzer_validates_required_result_columns(tmp_path):
    run_dir = tmp_path / "runs" / "bad-run"
    run_eval(DATASET, "local", "spec03", str(run_dir))

    with open(run_dir / "results.csv") as f:
        rows = list(csv.DictReader(f))

    # Rewrite with one required column removed so validation catches it before
    # any partial analysis is produced.
    fieldnames = [name.strip() for name in rows[0].keys() if name != "actual_tool"]
    with open(run_dir / "results.csv", "w", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        for row in rows:
            writer.writerow({k: row[k] for k in fieldnames})

    with pytest.raises(ValueError, match="missing required column.*actual_tool"):
        load_run(run_dir)
