"""Spec 07: eval pipeline — runner, graders, model, and reports.

These do not touch the app DB via the ``isolated_db`` fixture; each eval case
builds and disposes its own ``EvalEnv`` (which restores global session state),
so the pipeline is exercised exactly as it runs standalone.
"""

from __future__ import annotations

import csv
import json
import os
from datetime import datetime

from evals.graders import grade_case, overall
from evals.models import HeuristicModel, make_model
from evals.report import CSV_COLUMNS
from evals.run_openai_eval import load_dataset, run_case, run_eval
from evals.tool_schemas import PUBLIC_TOOLS, openai_tool_defs

DATASET = "evals/datasets/scheduler_tool_use_v1.jsonl"


# --- dataset + schema surface ---------------------------------------------
def test_dataset_has_40_cases_with_required_fields_and_spec07_flags():
    cases = load_dataset(DATASET)
    assert len(cases) == 40
    assert sum(1 for c in cases if c["grading"].get("safety_case")) == 10
    assert sum(1 for c in cases if c["grading"].get("trace_track")) == 10
    assert sum(1 for c in cases if c["grading"].get("use_llm_judge")) == 30
    for c in cases:
        assert c["id"] and c["prompt"] and c["now"]
        assert "expected" in c and "grading" in c


def test_openai_tool_defs_cover_public_surface():
    names = {d["name"] for d in openai_tool_defs()}
    assert names == set(PUBLIC_TOOLS)
    for d in openai_tool_defs():
        assert d["type"] == "function" and d["parameters"]["type"] == "object"


# --- end-to-end run --------------------------------------------------------
def test_run_eval_produces_all_artifacts_and_high_pass_rate(tmp_path):
    out = str(tmp_path / "run")
    summary = run_eval(DATASET, "local", "spec03", out)

    for name in ("summary.json", "results.csv", "traces.jsonl",
                 "openai_evals_payload.jsonl"):
        assert os.path.exists(os.path.join(out, name)), name

    assert summary["total"] >= 20
    assert summary["safety_cases"] == 10
    assert summary["trace_track_cases"] == 10
    # Genuine heuristic solver: strong but not perfect (the hard cron case fails).
    assert 0.8 <= summary["pass_rate"] < 1.0

    with open(os.path.join(out, "results.csv")) as f:
        rows = list(csv.DictReader(f))
    assert list(rows[0].keys()) == CSV_COLUMNS
    assert len(rows) == summary["total"]
    assert rows[0]["llm_judge_passed"] in ("True", "False")
    assert rows[0]["trace_track"] in ("True", "False")


def test_reports_capture_tool_result_and_usage(tmp_path):
    out = str(tmp_path / "run")
    run_eval(DATASET, "local", "spec03", out)
    traces = [json.loads(l) for l in open(os.path.join(out, "traces.jsonl"))]
    created = next(t for t in traces if t["case_id"] == "create_immediate_report_001")
    assert created["tool_calls"][0]["result"]["ok"] is True
    assert created["scheduler_trace_ids"], "immediate job should produce a trace"
    assert "input_tokens" in created["usage"]
    assert created["trace_track"] is True
    assert created["llm_judge"]["applicable"] is True
    assert created["created_job_ids"]


def test_hard_case_fails_and_is_reported(tmp_path):
    out = str(tmp_path / "run")
    run_eval(DATASET, "local", "spec03", out)
    with open(os.path.join(out, "results.csv")) as f:
        rows = {r["case_id"]: r for r in csv.DictReader(f)}
    hard = rows["create_recurring_hard_biweekly_009"]
    assert hard["passed"] == "False"
    assert "time_match" in hard["failure_reason"]


# --- model behavior --------------------------------------------------------
def _run_one(case_id):
    case = next(c for c in load_dataset(DATASET) if c["id"] == case_id)
    return run_case(HeuristicModel(), case, "spec03")


def test_heuristic_resolves_relative_time_and_timezone():
    rec = _run_one("create_one_time_reminder_vancouver_004")
    call = rec["trace"]["tool_calls"][0]
    assert call["name"] == "task_create_v1"
    assert call["arguments"]["time"] == "2026-06-15T16:30:00-07:00"
    assert call["arguments"]["timezone"] == "America/Vancouver"
    assert rec["passed"] is True


def test_heuristic_asks_for_missing_timezone_instead_of_guessing():
    rec = _run_one("clarify_missing_timezone_018")
    assert rec["trace"]["tool_calls"] == []
    assert rec["passed"] is True


def test_not_found_error_is_graded_from_real_outcome():
    rec = _run_one("error_not_found_020")
    result = rec["trace"]["tool_calls"][0]["result"]
    assert result["ok"] is False
    assert result["error"]["code"] == "NOT_FOUND"


def test_false_success_claim_on_error_is_caught():
    # The heuristic model says "Cancelled task X" though the delete returned
    # NOT_FOUND — a false success claim, which must fail the case.
    rec = _run_one("error_not_found_020")
    assert rec["passed"] is False
    failed = {g["name"] for g in rec["graders"] if g["applicable"] and not g["passed"]}
    assert "no_false_success_claim" in failed


def test_no_false_success_claim_passes_when_failure_acknowledged():
    from evals.graders import grade_case
    case = {"id": "x", "expected": {"tool": "task_delete_v1",
            "error": {"code": "NOT_FOUND"}}, "grading": {"requires_tool_call": True}}
    calls = [{"name": "task_delete_v1", "arguments": {"user_id": "u", "job_id": "job_x"},
              "result": {"ok": False, "error": {"code": "NOT_FOUND", "field": "job_id"}}}]
    g = next(x for x in grade_case(case, calls, "I couldn't find that task.")
             if x["name"] == "no_false_success_claim")
    assert g["passed"] is True


# --- graders catch a bad model --------------------------------------------
def test_graders_fail_a_wrong_tool_choice():
    case = {"id": "x", "expected": {"tool": "task_create_v1", "action": "send_reminder",
            "type": "immediate"}, "grading": {"requires_tool_call": True}}
    bad = [{"name": "task_delete_v1",
            "arguments": {"user_id": "u", "job_id": "job_x"},
            "result": {"ok": False, "error": {"code": "NOT_FOUND"}}}]
    graders = grade_case(case, bad, "Deleted it.")
    passed, _ = overall(graders)
    assert passed is False
    names = {g["name"] for g in graders if g["applicable"] and not g["passed"]}
    assert "tool_name_match" in names


def test_final_answer_overclaim_is_flagged():
    case = {"id": "x", "expected": {"tool": "task_create_v1"},
            "grading": {"requires_tool_call": True}}
    calls = [{"name": "task_create_v1", "arguments": {"user_id": "u"},
              "result": {"ok": True, "job": {"job_id": "job_1", "type": "one_time"}}}]
    graders = grade_case(case, calls, "Your email has been sent.")
    fa = next(g for g in graders if g["name"] == "final_answer_consistent")
    assert fa["passed"] is False


def test_make_model_rejects_unknown_spec():
    import pytest
    with pytest.raises(ValueError):
        make_model("gemini:pro", "sys")
