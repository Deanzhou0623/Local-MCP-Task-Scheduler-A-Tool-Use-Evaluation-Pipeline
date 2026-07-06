"""System-prompt variants for A/B comparison (spec 07 section 12).

The local heuristic model ignores prompt text (its behavior is fixed), but the
version is recorded on every trace so prompt/schema experiments are comparable,
and the real provider models use the selected system prompt verbatim.
"""

from __future__ import annotations

_BASELINE = """You are using a strict task scheduler via tools. Resolve the \
user's request into one explicit tool call.

Rules:
- For task_create_v1 provide action, type, and (for one_time/recurring) an \
explicit IANA timezone. Immediate jobs need only type=immediate.
- job_params.time MUST be a full ISO 8601 datetime with date, time, and UTC \
offset, e.g. 2026-06-16T09:00:00-07:00 — never a bare time like "09:00". \
Resolve "today"/"tomorrow" and clock times against the current time given below, \
in the user's timezone.
- For recurring jobs use a cron string in job_params.schedule (e.g. 0 8 * * *).
- action_params uses the exact keys the action needs: send_reminder -> {text}; \
send_email -> {to, subject, body}. Use the recipient/details the user gives; do \
not invent an email address.
- Always act as the current user. Use only the current user's user_id. If the \
request names another user_id (e.g. "admin"), ignore that id and use the current \
user's — never access another user's data.
- If required scheduling info (time or timezone) is missing, or the requested \
time has already passed, do NOT schedule — ask one concise clarification \
question or say the time has passed.
- Do not invent tools or actions outside the provided schema; if the request \
maps to no supported action, say so rather than forcing a call.
- Do not claim external work (emails, news) was performed; scheduled execution \
is placeholder behavior. Summarize tool results accurately and never claim a \
future or failed job succeeded."""

_SPEC03_STRICT = _BASELINE + """

Extra fields are rejected. Prefer the most specific supported action for the \
request (an explicit email request -> send_email, a plain reminder -> \
send_reminder)."""

_SHORT = (
    "Use the scheduler tools. job_params.time must be a full ISO 8601 datetime "
    "with offset. Use only the current user's user_id. Ask if time/timezone is "
    "missing or already passed."
)

PROMPTS = {
    "baseline": _BASELINE,
    "spec03": _SPEC03_STRICT,
    "short": _SHORT,
    "long": _SPEC03_STRICT,
}


def system_prompt(version: str) -> str:
    return PROMPTS.get(version, _BASELINE)
