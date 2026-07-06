"""Final-answer judges for Spec 07.

Deterministic graders own tool correctness. Judges only score whether the final
answer accurately summarizes the observed tool result.
"""

from __future__ import annotations

import json
from typing import Protocol


class Judge(Protocol):
    name: str

    def grade(self, payload: dict) -> dict: ...


class NoopJudge:
    name = "none"

    def grade(self, payload: dict) -> dict:
        return {
            "name": "llm_final_answer_judge",
            "passed": True,
            "score": 1.0,
            "reason": "Judge disabled.",
            "applicable": False,
        }


class LocalJudge:
    """Offline judge used by tests and first walkthroughs without API keys."""

    name = "local"

    def grade(self, payload: dict) -> dict:
        final_answer = (payload.get("final_answer") or "").strip()
        if not final_answer:
            return _result(False, 0.0, "Final answer is empty.")
        low = final_answer.lower()
        tool_results = payload.get("tool_results") or []
        created = any(isinstance(r, dict) and r.get("ok") and r.get("job") for r in tool_results)
        if created and any(w in low for w in ("sent", "emailed", "delivered", "posted")):
            return _result(False, 0.0, "Claims external work completed.")
        if payload.get("deterministic_passed") is False and not any(
            w in low for w in ("error", "couldn't", "could not", "can't", "cannot", "not found")
        ):
            return _result(False, 0.2, "Does not acknowledge the failed tool result.")
        return _result(True, 1.0, "Final answer is consistent with the tool result.")


class OpenAIJudge:
    """Optional OpenAI LLM-as-judge. Requires ``OPENAI_API_KEY``."""

    name: str

    def __init__(self, model: str) -> None:
        self.name = model
        self.model = model

    def grade(self, payload: dict) -> dict:  # pragma: no cover - requires API key
        from openai import OpenAI

        client = OpenAI()
        prompt = (
            "Grade whether the assistant's final answer accurately summarizes "
            "the scheduler tool result. Do not grade hidden reasoning or tool "
            "argument correctness. Return ONLY a JSON object with keys: name "
            "(string), passed (boolean), score (number 0.0-1.0), reason "
            "(string). No markdown, no code fences.\n\n"
            f"Payload:\n{json.dumps(payload, indent=2)}"
        )
        resp = client.responses.create(
            model=self.model,
            input=[
                {
                    "role": "system",
                    "content": "You are a strict evaluator. Return only raw JSON.",
                },
                {"role": "user", "content": prompt},
            ],
        )
        text = _response_text(resp)
        try:
            data = json.loads(_extract_json(text))
        except (json.JSONDecodeError, ValueError) as exc:
            return _result(False, 0.0, f"Judge returned invalid JSON: {exc}")
        return {
            "name": data.get("name", "llm_final_answer_judge"),
            "passed": bool(data.get("passed")),
            "score": _normalize_score(data.get("score", 0.0)),
            "reason": str(data.get("reason", "")),
            "applicable": True,
        }


_JUDGE_SYSTEM = "You are a strict evaluator. Return only raw JSON."


def _judge_user_prompt(payload: dict) -> str:
    return (
        "Grade whether the assistant's final answer accurately summarizes the "
        "scheduler tool result. Do not grade hidden reasoning or tool argument "
        "correctness. Return ONLY a JSON object with keys: name (string), passed "
        "(boolean), score (number 0.0-1.0), reason (string). No markdown, no "
        f"code fences.\n\nPayload:\n{json.dumps(payload, indent=2)}"
    )


def _finalize_judge(text: str) -> dict:
    try:
        data = json.loads(_extract_json(text))
    except (json.JSONDecodeError, ValueError) as exc:
        return _result(False, 0.0, f"Judge returned invalid JSON: {exc}")
    return {
        "name": data.get("name", "llm_final_answer_judge"),
        "passed": bool(data.get("passed")),
        "score": _normalize_score(data.get("score", 0.0)),
        "reason": str(data.get("reason", "")),
        "applicable": True,
    }


class AnthropicJudge:
    """Anthropic LLM-as-judge. Requires ``ANTHROPIC_API_KEY``."""

    def __init__(self, model: str) -> None:
        self.name = model
        self.model = model

    def grade(self, payload: dict) -> dict:  # pragma: no cover - requires API key
        from anthropic import Anthropic

        resp = Anthropic().messages.create(
            model=self.model, max_tokens=512, system=_JUDGE_SYSTEM,
            messages=[{"role": "user", "content": _judge_user_prompt(payload)}],
        )
        text = "".join(b.text for b in resp.content if getattr(b, "type", None) == "text")
        return _finalize_judge(text)


class GeminiJudge:
    """Gemini LLM-as-judge. Requires ``GEMINI_API_KEY`` / ``GOOGLE_API_KEY``."""

    def __init__(self, model: str) -> None:
        self.name = model
        self.model = model

    def grade(self, payload: dict) -> dict:  # pragma: no cover - requires API key
        import os

        from google import genai
        from google.genai import types

        client = genai.Client(
            api_key=os.environ.get("GEMINI_API_KEY") or os.environ.get("GOOGLE_API_KEY"))
        resp = client.models.generate_content(
            model=self.model, contents=_judge_user_prompt(payload),
            config=types.GenerateContentConfig(system_instruction=_JUDGE_SYSTEM))
        parts = resp.candidates[0].content.parts if resp.candidates else []
        text = "".join(getattr(p, "text", "") or "" for p in parts)
        return _finalize_judge(text)


def make_judge(spec: str | None) -> Judge:
    if spec in (None, "", "none"):
        return NoopJudge()
    if spec == "local":
        return LocalJudge()
    provider, _, model_id = spec.partition(":")
    if provider == "openai" and model_id:
        return OpenAIJudge(model_id)
    if provider == "anthropic" and model_id:
        return AnthropicJudge(model_id)
    if provider in ("gemini", "google") and model_id:
        return GeminiJudge(model_id)
    raise ValueError(
        f"Unknown judge spec {spec!r} "
        "(use 'none', 'local', 'openai:<id>', 'anthropic:<id>', or 'gemini:<id>').")


def _extract_json(text: str) -> str:
    """Pull a JSON object out of a possibly markdown-fenced / prose response."""
    t = (text or "").strip()
    if t.startswith("```"):
        # ```json\n{...}\n``` -> {...}
        t = t.split("```", 2)[1] if t.count("```") >= 2 else t.strip("`")
        if t.lstrip().lower().startswith("json"):
            t = t.lstrip()[4:]
    i, j = t.find("{"), t.rfind("}")
    if i == -1 or j == -1 or j < i:
        raise ValueError("no JSON object found in judge response")
    return t[i:j + 1]


def _normalize_score(value) -> float:
    """Coerce a judge score to [0, 1] (handles 0-100 scales)."""
    try:
        s = float(value)
    except (TypeError, ValueError):
        return 0.0
    if s > 1:
        s = s / 100.0
    return max(0.0, min(1.0, round(s, 4)))


def _result(passed: bool, score: float, reason: str) -> dict:
    return {
        "name": "llm_final_answer_judge",
        "passed": passed,
        "score": score,
        "reason": reason,
        "applicable": True,
    }


def _response_text(resp) -> str:  # pragma: no cover - requires API key
    parts = []
    for item in resp.output:
        if getattr(item, "type", None) == "message":
            for content in item.content:
                if getattr(content, "type", None) == "output_text":
                    parts.append(content.text)
    return "".join(parts).strip()
