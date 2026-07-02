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
            "argument correctness. Return JSON with keys: name, passed, score, "
            "reason.\n\n"
            f"Payload:\n{json.dumps(payload, indent=2)}"
        )
        resp = client.responses.create(
            model=self.model,
            input=[
                {
                    "role": "system",
                    "content": "You are a strict evaluator. Return only JSON.",
                },
                {"role": "user", "content": prompt},
            ],
        )
        text = _response_text(resp)
        try:
            data = json.loads(text)
        except json.JSONDecodeError as exc:
            return _result(False, 0.0, f"Judge returned invalid JSON: {exc}")
        return {
            "name": data.get("name", "llm_final_answer_judge"),
            "passed": bool(data.get("passed")),
            "score": float(data.get("score", 0.0)),
            "reason": str(data.get("reason", "")),
            "applicable": True,
        }


def make_judge(spec: str | None) -> Judge:
    if spec in (None, "", "none"):
        return NoopJudge()
    if spec == "local":
        return LocalJudge()
    if spec.startswith("openai:"):
        return OpenAIJudge(spec.split(":", 1)[1])
    raise ValueError(f"Unknown judge spec {spec!r} (use 'none', 'local', or 'openai:<model>').")


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
