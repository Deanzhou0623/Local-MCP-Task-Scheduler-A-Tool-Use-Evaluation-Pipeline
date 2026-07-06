"""Spec 07 extension: multi-provider adapters (OpenAI / Anthropic / Gemini).

Live API calls need keys and can't run in CI, so these tests cover the parts
that are deterministic and verifiable offline: the provider-response parsing
(SDK response shape -> normalized ModelResponse tuple), the per-provider tool
schema adapters, and the make_model / run-folder dispatch.
"""

from __future__ import annotations

from types import SimpleNamespace as NS

import pytest

from evals.models import AnthropicModel, GeminiModel, OpenAIModel, make_model
from evals.tool_schemas import (
    PUBLIC_TOOLS,
    anthropic_tool_defs,
    gemini_tool_defs,
    _sanitize_gemini,
)


# --- make_model dispatch ---------------------------------------------------
def test_make_model_dispatches_each_provider():
    assert isinstance(make_model("openai:gpt-x", "s"), OpenAIModel)
    assert isinstance(make_model("anthropic:claude-x", "s"), AnthropicModel)
    assert isinstance(make_model("gemini:gemini-x", "s"), GeminiModel)
    assert make_model("openai:gpt-x", "s").name == "gpt-x"


def test_make_model_rejects_unknown_and_empty_id():
    for bad in ("mistral:x", "openai:", "anthropic"):
        with pytest.raises(ValueError):
            make_model(bad, "s")


# --- tool schema adapters --------------------------------------------------
def test_provider_tool_defs_cover_public_surface():
    for defs, key in ((anthropic_tool_defs(), "input_schema"),
                      (gemini_tool_defs(), "parameters")):
        names = {d["name"] for d in defs}
        assert names == set(PUBLIC_TOOLS)
        assert all(key in d and d["description"] for d in defs)


def test_gemini_sanitizer_strips_unsupported_keywords():
    schema = {
        "type": "object",
        "additionalProperties": False,
        "title": "X",
        "properties": {
            "user_id": {"type": "string"},
            "action_params": {
                "anyOf": [{"type": "object", "additionalProperties": True},
                          {"type": "null"}]
            },
        },
    }
    out = _sanitize_gemini(schema)
    assert "additionalProperties" not in out
    assert "title" not in out
    # Optional (anyOf [T, null]) collapses to a nullable single type.
    ap = out["properties"]["action_params"]
    assert "anyOf" not in ap and ap.get("nullable") is True


# --- Anthropic response parsing -------------------------------------------
def test_anthropic_parse_extracts_tool_use_and_text():
    resp = NS(
        content=[
            NS(type="tool_use", name="task_create_v1",
               input={"user_id": "u", "action": "generate_report", "type": "immediate"},
               id="toolu_1"),
            NS(type="text", text="Scheduled it."),
        ],
        usage=NS(input_tokens=120, output_tokens=18),
    )
    tool_calls, final, usage = AnthropicModel._parse(resp)
    assert tool_calls == [{"name": "task_create_v1",
                           "arguments": {"user_id": "u", "action": "generate_report",
                                         "type": "immediate"},
                           "call_id": "toolu_1"}]
    assert final == "Scheduled it."
    assert usage == {"input_tokens": 120, "output_tokens": 18}


def test_anthropic_parse_handles_text_only():
    resp = NS(content=[NS(type="text", text="What timezone?")],
              usage=NS(input_tokens=10, output_tokens=4))
    tool_calls, final, usage = AnthropicModel._parse(resp)
    assert tool_calls == []
    assert final == "What timezone?"


# --- Gemini response parsing ----------------------------------------------
def test_gemini_parse_extracts_function_call_and_text():
    resp = NS(
        candidates=[NS(content=NS(parts=[
            NS(function_call=NS(name="task_list_v1", args={"user_id": "u"}), text=None),
            NS(function_call=None, text="Listed your tasks."),
        ]))],
        usage_metadata=NS(prompt_token_count=90, candidates_token_count=12),
    )
    tool_calls, final, usage = GeminiModel._parse(resp)
    assert tool_calls == [{"name": "task_list_v1", "arguments": {"user_id": "u"}}]
    assert final == "Listed your tasks."
    assert usage == {"input_tokens": 90, "output_tokens": 12}


def test_gemini_parse_handles_no_candidates():
    resp = NS(candidates=[], usage_metadata=None)
    tool_calls, final, usage = GeminiModel._parse(resp)
    assert tool_calls == [] and final == ""
    assert usage == {"input_tokens": 0, "output_tokens": 0}


# --- OpenAI judge robustness (fenced JSON + 0-100 score) -------------------
def test_judge_extract_json_from_fence_and_prose():
    import json

    from evals.judge import _extract_json
    assert json.loads(_extract_json('```json\n{"passed": true, "score": 1}\n```'))["passed"] is True
    assert json.loads(_extract_json('Verdict: {"score": 0.5} thanks'))["score"] == 0.5
    with pytest.raises(ValueError):
        _extract_json("no json object here")


def test_judge_normalize_score_handles_0_100_scale():
    from evals.judge import _normalize_score
    assert _normalize_score(100) == 1.0      # 0-100 scale -> 1.0
    assert _normalize_score(85) == 0.85
    assert _normalize_score(0.8) == 0.8      # already 0-1
    assert _normalize_score(999) == 1.0      # clamp
    assert _normalize_score("bad") == 0.0
