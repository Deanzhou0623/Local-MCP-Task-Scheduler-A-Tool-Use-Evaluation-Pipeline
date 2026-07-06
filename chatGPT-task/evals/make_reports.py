"""Generate the 4 human-readable eval reports (spec 08 deliverable).

Produces one markdown report per model plus one final comparison report, from
existing Spec07 run folders and the Spec08 analysis folder:

    python -m evals.make_reports \
        --runs evals/results/runs/flagship-openai evals/results/runs/flagship-anthropic evals/results/runs/flagship-gemini \
        --analysis evals/results/analysis/flagship \
        --out evals/results/reports
"""

from __future__ import annotations

import argparse
import csv
import json
from pathlib import Path


def _load(run: Path) -> dict:
    summary = json.load(open(run / "summary.json"))
    rows = list(csv.DictReader(open(run / "results.csv")))
    return {"summary": summary, "rows": rows, "name": run.name}


def _judge_mean(rows: list[dict]) -> float:
    vals = [float(r["llm_judge_score"]) for r in rows if r["llm_judge_score"] not in ("", None)]
    return round(sum(vals) / len(vals), 4) if vals else 0.0


def _per_model_report(run: Path) -> tuple[str, str]:
    d = _load(run)
    s, rows = d["summary"], d["rows"]
    traces = {t["case_id"]: t for t in
              (json.loads(l) for l in open(run / "traces.jsonl") if l.strip())}
    model = s.get("run", {}).get("model", d["name"])
    judge = s.get("run", {}).get("judge", "?")
    fails = [r for r in rows if r["passed"] == "False"]
    lines = [
        f"# Eval Report — {model}", "",
        f"- Dataset: {s['total']} cases ({s.get('safety_cases', 0)} safety, "
        f"{s.get('trace_track_cases', 0)} trace-track) · prompt "
        f"`{s.get('run', {}).get('prompt_version', '?')}` · judge `{judge}`",
        f"- **Pass rate: {s['passed']}/{s['total']} ({s['pass_rate']})** · "
        f"mean score {s['mean_score']} · final-answer judge {_judge_mean(rows)}",
        "", "## Pass rate by category", "",
        "| category | passed / total |", "| --- | --- |",
    ]
    for cat, v in sorted(s["by_category"].items(), key=lambda x: x[1]["pass_rate"]):
        lines.append(f"| {cat} | {v['passed']}/{v['total']} ({v['pass_rate']}) |")
    lines += ["", "## Weakest deterministic graders", "",
              "| grader | pass rate |", "| --- | --- |"]
    for g, v in sorted(s["by_grader"].items(), key=lambda x: x[1]["pass_rate"])[:6]:
        lines.append(f"| {g} | {v['pass_rate']} ({v['passed']}/{v['total']}) |")
    lines += ["", f"## Failures ({len(fails)}) — detail", ""]
    for r in fails:
        t = traces.get(r["case_id"], {})
        failed_g = [g["name"] for g in t.get("deterministic_graders", [])
                    if g.get("applicable") and not g.get("passed")]
        answer = (t.get("final_answer") or "").replace("\n", " ")[:180]
        err = f" · error=`{r['error_code']}`" if r["error_code"] else ""
        lines += [
            f"### `{r['case_id']}` — {r['category']}",
            f"- Failed graders: {', '.join(failed_g) or r['failure_reason'][:80]}",
            f"- Expected: tool=`{r['expected_tool'] or '—'}` "
            f"action=`{r['expected_action'] or '—'}` time=`{r['expected_time'] or '—'}`",
            f"- Actual: tool=`{r['actual_tool'] or '—'}` "
            f"action=`{r['actual_action'] or '—'}` time=`{r['actual_time'] or '—'}`{err}",
            f"- Final answer: {answer!r}",
            "",
        ]
    return f"report-{model}.md", "\n".join(lines) + "\n"


def _fail_sets(runs: list[Path]) -> dict[str, tuple[set, dict]]:
    """Per model: (set of failed case ids, {case_id: category})."""
    out = {}
    for run in runs:
        rows = list(csv.DictReader(open(run / "results.csv")))
        model = json.load(open(run / "summary.json")).get("run", {}).get("model", run.name)
        out[model] = ({r["case_id"] for r in rows if r["passed"] == "False"},
                      {r["case_id"]: r["category"] for r in rows})
    return out


def _final_report(runs: list[Path], analysis: Path) -> str:
    leaderboard = list(csv.DictReader(open(analysis / "model_leaderboard.csv")))
    clusters = json.load(open(analysis / "failure_clusters.json"))
    trace = json.load(open(analysis / "trace_track_report.json"))
    recs = (analysis / "recommendations.md").read_text() if (analysis / "recommendations.md").exists() else ""
    n_fail = sum(1 for _ in open(analysis / "all_failures.jsonl"))

    lines = [
        "# Flagship Model Comparison — Final Report", "",
        f"Dataset: 40 cases (30 scheduler tool-use + 10 safety). Judge: "
        f"`{leaderboard[0].get('model') and json.load(open(runs[0]/'summary.json')).get('run',{}).get('judge','?')}`. "
        "Deterministic graders are the source of truth; the judge scores "
        "final-answer quality only.", "",
        "## Leaderboard", "",
        "| Rank | Model | Weighted | Pass | Safety | Time | Judge | In tok | Out tok | p95 ms |",
        "| ---: | --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: |",
    ]
    for r in leaderboard:
        lines.append(
            f"| {r['rank']} | {r['model']} | {r['weighted_score']} | {r['pass_rate']} | "
            f"{r['safety_pass_rate']} | {r['time_match_rate']} | {r['mean_llm_judge_score']} | "
            f"{r['input_tokens_total']} | {r['output_tokens_total']} | {r['p95_latency_ms']} |")

    # --- Commonalities & differences (computed from the runs) --------------
    fs = _fail_sets(runs)
    all_fail = [s for s, _ in fs.values()]
    common = set.intersection(*all_fail) if all_fail else set()
    any_cats = {}
    for _, cats in fs.values():
        any_cats.update(cats)

    lines += ["", "## What all three share (common failures)", "",
              "Cases **every** model missed — the shared blind spots "
              "(honesty-under-pressure + a few ambiguous/hard cases):", ""]
    for c in sorted(common):
        lines.append(f"- `{c}` ({any_cats.get(c, '?')})")
    if not common:
        lines.append("- (none — no case was missed by all three)")

    lines += ["", "## Where they differ (unique failures per model)", ""]
    for model, (s, cats) in fs.items():
        others = set().union(*[o for m2, (o, _) in fs.items() if m2 != model])
        uniq = sorted(s - others)
        detail = ", ".join(f"`{c}` ({cats[c]})" for c in uniq) or "none"
        lines.append(f"- **{model}** — {len(s)} fails, {len(uniq)} unique: {detail}")

    lines += ["", "### Profile differences", "",
              "| model | pass | time acc | out tok/case | judge | p95 ms |",
              "| --- | ---: | ---: | ---: | ---: | ---: |"]
    for r in leaderboard:
        opc = int(r["output_tokens_total"]) // 40
        lines.append(
            f"| {r['model']} | {r['pass_rate']} | {r['time_match_rate']} | {opc} | "
            f"{r['mean_llm_judge_score']} | {r['p95_latency_ms']} |")
    lines += ["",
              "Read: shared weakness is honesty (past-time / false-claim) and "
              "safety; the models diverge most on **exact time/timezone** and on "
              "**verbosity/cost** (output tokens per case), and each has a "
              "*different* safety gap."]

    lines += ["", "## Trace-track (10 end-to-end cases per model)", "",
              "| model | pass | jobs | runs | sched-traces | fails |",
              "| --- | ---: | ---: | ---: | ---: | --- |"]
    for run in trace["runs"]:
        lines.append(
            f"| {run['model']} | {run['trace_track_pass_rate']} | {run['cases_with_job_id']} | "
            f"{run['cases_with_run_id']} | {run['cases_with_scheduler_trace_id']} | "
            f"{', '.join(run['trace_track_failure_cases']) or '—'} |")

    lines += ["", f"## Top failure clusters ({n_fail} total failures across models)", ""]
    for c in clusters[:8]:
        lines.append(
            f"- **{c['failure_type']}** ×{c['count']} — models {', '.join(c['affected_models'])}; "
            f"owner `{c['suggested_owner']}`; cases {', '.join(c['case_ids'])}")

    lines += ["", "## Recommendations", ""]
    lines += [l for l in recs.splitlines() if l.startswith("- From")]

    lines += ["", "## Caveats", "",
              "- Judge = `claude-opus-4-8`, which is one of the compared models → "
              "possible self-preference bias in its own judge column.",
              "- 40 cases, single run, no pass@k → small sample; 1-3 case gaps are "
              "not statistically strong.",
              "- Full per-case failures are in `all_failures.jsonl` next to the analysis."]
    return "\n".join(lines) + "\n"


def make_reports(run_dirs: list[str], analysis_dir: str, out_dir: str) -> list[str]:
    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)
    runs = [Path(r) for r in run_dirs]
    written = []
    for run in runs:
        fname, content = _per_model_report(run)
        (out / fname).write_text(content)
        written.append(fname)
    (out / "report-final-comparison.md").write_text(_final_report(runs, Path(analysis_dir)))
    written.append("report-final-comparison.md")
    return written


def main() -> None:
    p = argparse.ArgumentParser(description="Generate the 4 eval reports.")
    p.add_argument("--runs", nargs="+", required=True)
    p.add_argument("--analysis", required=True)
    p.add_argument("--out", default="evals/results/reports")
    args = p.parse_args()
    written = make_reports(args.runs, args.analysis, args.out)
    print(f"Wrote {len(written)} reports to {args.out}: {', '.join(written)}")


if __name__ == "__main__":
    main()
