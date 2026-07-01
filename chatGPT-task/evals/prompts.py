"""System-prompt variants for A/B comparison (spec 07 section 12).

The local heuristic model ignores prompt text (its behavior is fixed), but the
version is recorded on every trace so prompt/schema experiments are comparable,
and the OpenAI model path uses the selected system prompt verbatim.
"""

from __future__ import annotations

_BASELINE = """You are using a strict task scheduler via tools. Resolve the \
user's request into an explicit tool call. For task_create_v1 provide action, \
type, and (for one_time/recurring) an explicit IANA timezone and time/schedule. \
If required scheduling info is missing, ask one concise clarification question \
instead of guessing. Do not claim external work (emails, news) was performed; \
scheduled execution is placeholder behavior. Summarize tool results accurately."""

_SPEC03_STRICT = _BASELINE + """ Extra fields are rejected. Never invent tools \
or actions outside the provided schema; if the request maps to no supported \
action, say so rather than forcing a call."""

_SHORT = "Use the scheduler tools. Be explicit; ask if timezone/time is missing."

PROMPTS = {
    "baseline": _BASELINE,
    "spec03": _SPEC03_STRICT,
    "short": _SHORT,
    "long": _SPEC03_STRICT,
}


def system_prompt(version: str) -> str:
    return PROMPTS.get(version, _BASELINE)
