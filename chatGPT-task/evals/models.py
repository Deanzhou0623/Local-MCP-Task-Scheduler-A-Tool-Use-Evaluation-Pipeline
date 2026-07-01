"""Models under evaluation (spec 07 section 6).

Two implementations behind one interface:

- ``HeuristicModel`` (default): a genuine rule-based NL->tool solver. It parses
  intent, time, and timezone and *can be wrong* on hard cases, so the graders
  report a realistic mixed pass rate rather than a vacuous 100%.
- ``OpenAIModel`` (optional): a real Responses-API call with the scheduler tool
  schemas. Requires ``OPENAI_API_KEY``; used only when explicitly selected.

The eval harness executes the returned tool calls against the isolated DB; the
model only decides *what* to call.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Optional, Protocol
from zoneinfo import ZoneInfo


@dataclass
class ModelResponse:
    tool_calls: list[dict] = field(default_factory=list)  # [{name, arguments}]
    final_answer: str = ""
    usage: dict = field(default_factory=lambda: {"input_tokens": 0, "output_tokens": 0})
    latency_ms: int = 0


class Model(Protocol):
    name: str

    def run(self, prompt: str, now: datetime, context: dict) -> ModelResponse: ...


# --- Heuristic solver ------------------------------------------------------
_JOB_ID = re.compile(r"job_[0-9a-f]+")
_TRACE_ID = re.compile(r"trace_[0-9a-f]+")
_TIME = re.compile(r"\b(\d{1,2})(?::(\d{2}))?\s*(am|pm)?\b")

_CITY_TZ = {
    "vancouver": "America/Vancouver",
    "los angeles": "America/Los_Angeles",
    "san francisco": "America/Los_Angeles",
    "new york": "America/New_York",
    "toronto": "America/Toronto",
    "london": "Europe/London",
    "tokyo": "Asia/Tokyo",
}

_SCOPE_WORDS = (
    "remind", "reminder", "schedule", "task", "report", "news", "review",
    "email", "list", "show", "get", "status", "cancel", "delete", "modify",
    "change", "reschedule", "move", "every", "daily", "runs", "history", "trace",
    "pr", "pull request",
)


class HeuristicModel:
    name = "local-heuristic"

    def run(self, prompt: str, now: datetime, context: dict) -> ModelResponse:
        text = prompt.strip()
        low = text.lower()
        user_id = context.get("user_id", "eval-user")
        usage = {"input_tokens": len(text.split()), "output_tokens": 12}

        def resp(tool_calls, answer):
            return ModelResponse(tool_calls, answer, usage, 0)

        # --- Stateful tools keyed by an explicit id in the prompt ---------
        trace_m = _TRACE_ID.search(text)
        if trace_m and ("trace" in low or "what happened" in low or "detail" in low):
            tid = trace_m.group(0)
            return resp([{"name": "task_trace_get_v1",
                          "arguments": {"user_id": user_id, "trace_id": tid}}],
                        f"Here is the execution trace {tid}.")

        job_m = _JOB_ID.search(text)
        if job_m:
            jid = job_m.group(0)
            if any(w in low for w in ("run history", "runs", "history", "attempts")):
                return resp([{"name": "task_runs_list_v1",
                              "arguments": {"user_id": user_id, "job_id": jid}}],
                            f"Here is the run history for {jid}.")
            if any(w in low for w in ("cancel", "delete", "remove")):
                return resp([{"name": "task_delete_v1",
                              "arguments": {"user_id": user_id, "job_id": jid}}],
                            f"Cancelled task {jid}.")
            if any(w in low for w in ("pause", "reschedule", "change", "modify",
                                      "update", "move")):
                return resp([{"name": "task_modify_v1",
                              "arguments": self._modify_args(user_id, jid, low, now, context)}],
                            f"Updated task {jid}.")
            return resp([{"name": "task_get_v1",
                          "arguments": {"user_id": user_id, "job_id": jid}}],
                        f"Here are the details for {jid}.")

        # --- List ---------------------------------------------------------
        if _TRACE_ID.search(text) is None and job_m is None and any(
            p in low for p in ("list", "what tasks", "my tasks", "all tasks",
                               "scheduled tasks", "show me my")
        ):
            args = {"user_id": user_id}
            for status in ("scheduled", "paused", "failed", "completed"):
                if status in low:
                    args["status"] = status
            return resp([{"name": "task_list_v1", "arguments": args}],
                        "Listed your scheduled tasks.")

        # --- Out of scope -------------------------------------------------
        if not any(w in low for w in _SCOPE_WORDS):
            return resp([], "I can only schedule and manage tasks, so I can't help "
                            "with that request.")

        # --- Create -------------------------------------------------------
        return self._create(user_id, text, low, now, context, resp)

    # -- create helpers ----------------------------------------------------
    def _action(self, low: str) -> str:
        # Reminder intent wins over verbs inside the reminder text
        # ("remind me to review ..." is a reminder, not review_pr).
        if "remind" in low:
            return "send_reminder"
        if "financial news" in low or ("financ" in low and "news" in low):
            return "summarize_financial_news"
        if "report" in low:
            return "generate_report"
        if "review" in low or "pull request" in low or re.search(r"\bpr\b", low):
            return "review_pr"
        if "email" in low:
            return "send_email"
        if "news" in low:
            return "fetch_news"
        return "send_reminder"

    def _timezone(self, low: str, context: dict) -> Optional[str]:
        for city, tz in _CITY_TZ.items():
            if city in low:
                return tz
        return context.get("timezone")

    def _time_of_day(self, low: str) -> Optional[tuple[int, int]]:
        if "noon" in low:
            return (12, 0)
        if "midnight" in low:
            return (0, 0)
        m = _TIME.search(low)
        if not m:
            return None
        hour = int(m.group(1))
        minute = int(m.group(2) or 0)
        ampm = m.group(3)
        if ampm == "pm" and hour != 12:
            hour += 12
        elif ampm == "am" and hour == 12:
            hour = 0
        if hour > 23:
            return None
        return (hour, minute)

    def _reminder_text(self, text: str) -> str:
        low = text.lower()
        body = text
        for lead in ("remind me to ", "set a reminder to ", "reminder to ",
                     "remind me ", "schedule a task to ", "schedule "):
            i = low.find(lead)
            if i != -1:
                body = text[i + len(lead):]
                break
        # Cut trailing time/schedule clauses.
        cut = re.split(r"\s+(?:at|every|each|today|tomorrow|on|next)\b", body,
                       maxsplit=1, flags=re.IGNORECASE)[0]
        return cut.strip().rstrip(".").strip()

    def _create(self, user_id, text, low, now, context, resp) -> ModelResponse:
        action = self._action(low)
        tz_name = self._timezone(low, context)
        recurring = any(w in low for w in ("every day", "each day", "daily",
                                           "every morning", "every evening",
                                           "every night", "every week", "weekly",
                                           "every"))
        immediate = any(w in low for w in ("right now", "immediately", "now",
                                           "asap")) and not recurring
        tod = self._time_of_day(low)

        # Missing-timezone good behavior: scheduled work needs a tz we can't find.
        if (recurring or (tod is not None)) and not immediate and tz_name is None:
            return resp([], "What timezone should I schedule that in?")

        args: dict = {"user_id": user_id, "action": action}
        if immediate or (tod is None and not recurring):
            args["type"] = "immediate"
            when = "now"
        elif recurring:
            args["type"] = "recurring"
            hour, minute = tod or (8, 0)
            args["schedule"] = f"{minute} {hour} * * *"
            args["timezone"] = tz_name
            when = f"every day at {hour:02d}:{minute:02d}"
        else:
            args["type"] = "one_time"
            hour, minute = tod
            tz = ZoneInfo(tz_name)
            local_now = now.astimezone(tz)
            target = local_now.replace(hour=hour, minute=minute, second=0, microsecond=0)
            if "tomorrow" in low or (target <= local_now and "today" not in low):
                target += timedelta(days=1)
            args["time"] = target.isoformat()
            args["timezone"] = tz_name
            when = target.strftime("%Y-%m-%d %H:%M")

        if action == "send_reminder":
            args["action_params"] = {"text": self._reminder_text(text)}

        return resp([{"name": "task_create_v1", "arguments": args}],
                    f"Scheduled a {args['type'].replace('_', '-')} "
                    f"'{action}' task for {when}.")

    def _modify_args(self, user_id, jid, low, now, context) -> dict:
        args = {"user_id": user_id, "job_id": jid}
        if "pause" in low:
            args["status"] = "paused"
        tod = self._time_of_day(low)
        if tod is not None and ("reschedule" in low or "move" in low or "change" in low
                                or "to " in low):
            tz_name = self._timezone(low, context) or context.get("timezone")
            if tz_name:
                tz = ZoneInfo(tz_name)
                local_now = now.astimezone(tz)
                target = local_now.replace(hour=tod[0], minute=tod[1], second=0,
                                           microsecond=0)
                if "tomorrow" in low or target <= local_now:
                    target += timedelta(days=1)
                args["type"] = "one_time"
                args["time"] = target.isoformat()
                args["timezone"] = tz_name
        return args


# --- OpenAI model (optional) ----------------------------------------------
class OpenAIModel:
    """Real OpenAI Responses-API model. Requires ``OPENAI_API_KEY``.

    Kept import-light so the default local path never needs the ``openai`` SDK.
    """

    def __init__(self, model: str, system_prompt: str) -> None:
        self.name = model
        self.model = model
        self.system_prompt = system_prompt

    def run(self, prompt: str, now: datetime, context: dict) -> ModelResponse:  # pragma: no cover
        import time

        from openai import OpenAI

        from evals.tool_schemas import openai_tool_defs

        client = OpenAI()
        tz = context.get("timezone", "UTC")
        sys = (f"{self.system_prompt}\nCurrent time: {now.isoformat()}. "
               f"User id: {context.get('user_id', 'eval-user')}. "
               f"User timezone: {tz}.")
        started = time.time()
        resp = client.responses.create(
            model=self.model,
            input=[{"role": "system", "content": sys},
                   {"role": "user", "content": prompt}],
            tools=openai_tool_defs(),
        )
        latency_ms = int((time.time() - started) * 1000)
        tool_calls = []
        final_answer = ""
        import json as _json
        for item in resp.output:
            if getattr(item, "type", None) == "function_call":
                tool_calls.append({"name": item.name,
                                   "arguments": _json.loads(item.arguments)})
            elif getattr(item, "type", None) == "message":
                for c in item.content:
                    if getattr(c, "type", None) == "output_text":
                        final_answer += c.text
        usage = {"input_tokens": getattr(resp.usage, "input_tokens", 0),
                 "output_tokens": getattr(resp.usage, "output_tokens", 0)}
        return ModelResponse(tool_calls, final_answer.strip(), usage, latency_ms)


def make_model(spec: str, system_prompt: str) -> Model:
    """Build a model from a spec string: ``local`` or ``openai:<model>``."""
    if spec == "local" or spec == "local-heuristic":
        return HeuristicModel()
    if spec.startswith("openai:"):
        return OpenAIModel(spec.split(":", 1)[1], system_prompt)
    raise ValueError(f"Unknown model spec {spec!r} (use 'local' or 'openai:<model>').")
