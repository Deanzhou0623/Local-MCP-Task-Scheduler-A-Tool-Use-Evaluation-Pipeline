"""Deterministic, outcome-aware graders (spec 07 section 9).

Per Anthropic's evals guidance, these grade the *outcome* (normalized fields and
the real tool result), not brittle raw-string args or an exact tool-call
sequence: ``time_match`` compares instants, ``action_params_match`` compares
parsed dicts, ``tool_result_success`` inspects what the scheduler actually did.

Each grader returns ``{name, passed, score, reason, applicable}``. A case's
overall verdict is the AND of all *applicable required* graders (everything
except the subjective ``final_answer_consistent``, which only affects score).
"""

from __future__ import annotations

import json
import re
from datetime import datetime, timezone

from app.mcp.server import _normalize_action

REQUIRED_TOOL_KEYS = {
    "task_create_v1": ("user_id", "action", "type"),
    "task_list_v1": ("user_id",),
    "task_get_v1": ("user_id", "job_id"),
    "task_modify_v1": ("user_id", "job_id"),
    "task_delete_v1": ("user_id", "job_id"),
    "task_trace_get_v1": ("user_id", "trace_id"),
    "task_runs_list_v1": ("user_id", "job_id"),
}

# Graders that gate the overall verdict (final_answer_consistent is scored only).
REQUIRED_GRADERS = {
    "tool_name_match", "required_tool_called", "no_unexpected_tool",
    "json_args_valid", "action_match", "job_type_match", "time_match",
    "timezone_match", "action_params_match", "tool_result_success",
}


def _g(name, passed, reason, applicable=True):
    return {"name": name, "passed": bool(passed), "score": 1.0 if passed else 0.0,
            "reason": reason, "applicable": applicable}


def _na(name):
    return {"name": name, "passed": True, "score": 1.0, "reason": "n/a",
            "applicable": False}


def _instant(value: str) -> datetime:
    dt = datetime.fromisoformat(value)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def _norm_text(s) -> str:
    return re.sub(r"[^a-z0-9 ]", "", str(s).lower()).strip()


def grade_case(case: dict, tool_calls: list[dict], final_answer: str) -> list[dict]:
    expected = case.get("expected", {})
    grading = case.get("grading", {})
    exp_tool = expected.get("tool")
    primary = tool_calls[0] if tool_calls else None
    actual_tool = primary["name"] if primary else None
    args = primary["arguments"] if primary else {}
    result = primary.get("result", {}) if primary else {}
    out = []

    # 1. tool_name_match
    out.append(_g("tool_name_match", actual_tool == exp_tool,
                  f"expected {exp_tool!r}, got {actual_tool!r}"))

    # 2. required_tool_called
    if grading.get("requires_tool_call", exp_tool is not None):
        called = any(tc["name"] == exp_tool for tc in tool_calls)
        out.append(_g("required_tool_called", called,
                      f"{exp_tool!r} called: {called}"))
    else:
        ok = not tool_calls or bool(grading.get("allow_clarifying_question"))
        out.append(_g("required_tool_called", ok,
                      "no tool required" if ok else "unexpected tool call"))

    # 3. no_unexpected_tool
    if exp_tool is None:
        out.append(_g("no_unexpected_tool", not tool_calls,
                      f"{len(tool_calls)} tool call(s) when none expected"))
    else:
        extra = [tc["name"] for tc in tool_calls if tc["name"] != exp_tool]
        out.append(_g("no_unexpected_tool", not extra, f"unexpected: {extra}"))

    # 4. json_args_valid
    if primary is None:
        out.append(_na("json_args_valid"))
    else:
        try:
            json.dumps(args)
            missing = [k for k in REQUIRED_TOOL_KEYS.get(actual_tool, ())
                       if args.get(k) in (None, "")]
            out.append(_g("json_args_valid", not missing,
                          "valid" if not missing else f"missing {missing}"))
        except (TypeError, ValueError) as exc:
            out.append(_g("json_args_valid", False, f"unserializable: {exc}"))

    # 5. action_match
    if "action" in expected:
        actual_action = _normalize_action(args["action"]) if args.get("action") else None
        out.append(_g("action_match", actual_action == expected["action"],
                      f"expected {expected['action']!r}, got {actual_action!r}"))
    else:
        out.append(_na("action_match"))

    # 6. job_type_match
    if "type" in expected:
        out.append(_g("job_type_match", args.get("type") == expected["type"],
                      f"expected {expected['type']!r}, got {args.get('type')!r}"))
    else:
        out.append(_na("job_type_match"))

    # 7. time_match (compare instants, not strings)
    if "time" in expected:
        try:
            ok = args.get("time") is not None and _instant(args["time"]) == _instant(expected["time"])
            out.append(_g("time_match", ok,
                          f"expected {expected['time']}, got {args.get('time')}"))
        except (TypeError, ValueError) as exc:
            out.append(_g("time_match", False, f"bad time: {exc}"))
    elif "schedule" in expected:
        out.append(_g("time_match", args.get("schedule") == expected["schedule"],
                      f"expected cron {expected['schedule']!r}, got {args.get('schedule')!r}"))
    else:
        out.append(_na("time_match"))

    # 8. timezone_match
    if "timezone" in expected:
        out.append(_g("timezone_match", args.get("timezone") == expected["timezone"],
                      f"expected {expected['timezone']!r}, got {args.get('timezone')!r}"))
    else:
        out.append(_na("timezone_match"))

    # 9. action_params_match (parsed dict; text compared fuzzily)
    if "action_params" in expected:
        exp_ap = expected["action_params"]
        act_ap = args.get("action_params") or {}
        ok = True
        reason = "match"
        for k, v in exp_ap.items():
            if k not in act_ap:
                ok, reason = False, f"missing param {k!r}"
                break
            if k == "text":
                a, b = _norm_text(act_ap[k]), _norm_text(v)
                if not (a == b or a in b or b in a):
                    ok, reason = False, f"text {act_ap[k]!r} != {v!r}"
                    break
            elif act_ap[k] != v:
                ok, reason = False, f"param {k}: {act_ap[k]!r} != {v!r}"
                break
        out.append(_g("action_params_match", ok, reason))
    else:
        out.append(_na("action_params_match"))

    # 10. tool_result_success (the real scheduler outcome)
    if "error" in expected:
        err = result.get("error", {}) if isinstance(result, dict) else {}
        ok = result.get("ok") is False and err.get("code") == expected["error"].get("code")
        if ok and expected["error"].get("field"):
            ok = err.get("field") == expected["error"]["field"]
        out.append(_g("tool_result_success", ok,
                      f"expected error {expected['error']}, got {result.get('error')}"))
    elif "result_ok" in expected:
        out.append(_g("tool_result_success", result.get("ok") == expected["result_ok"],
                      f"expected ok={expected['result_ok']}, got ok={result.get('ok')}"))
    elif primary is not None:
        out.append(_g("tool_result_success", result.get("ok") is True,
                      f"ok={result.get('ok')}"))
    else:
        out.append(_na("tool_result_success"))

    # 11. final_answer_consistent (scored, not gating)
    out.append(_grade_final_answer(final_answer, expected, result))
    return out


def _grade_final_answer(final_answer: str, expected: dict, result: dict) -> dict:
    fa = (final_answer or "").strip()
    if not fa:
        return _g("final_answer_consistent", False, "empty final answer")
    low = fa.lower()
    created = isinstance(result, dict) and result.get("ok") and result.get("job")
    # Don't over-claim placeholder execution as done (spec02 rule).
    overclaim = created and any(w in low for w in ("sent", "emailed", "delivered",
                                                   "posted", "published"))
    if overclaim:
        return _g("final_answer_consistent", False,
                  "claims completed external work for a placeholder job")
    if expected.get("tool") is None:
        ok = fa.endswith("?") or any(w in low for w in ("can't", "cannot", "only",
                                                         "what timezone", "which"))
        return _g("final_answer_consistent", ok,
                  "clarify/decline" if ok else "should clarify or decline")
    return _g("final_answer_consistent", True, "consistent")


def overall(graders: list[dict]) -> tuple[bool, float]:
    gating = [g for g in graders
              if g["applicable"] and g["name"] in REQUIRED_GRADERS]
    scored = [g for g in graders if g["applicable"]]
    passed = all(g["passed"] for g in gating)
    score = (sum(g["score"] for g in scored) / len(scored)) if scored else 1.0
    return passed, round(score, 4)


def failure_reason(graders: list[dict]) -> str:
    fails = [f"{g['name']}: {g['reason']}" for g in graders
             if g["applicable"] and not g["passed"] and g["name"] in REQUIRED_GRADERS]
    return "; ".join(fails)
