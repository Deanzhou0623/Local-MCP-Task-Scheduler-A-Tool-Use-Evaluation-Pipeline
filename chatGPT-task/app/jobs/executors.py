"""Action executors and the executor registry (spec 04, section 5).

Spec 04 replaces the Spec 01 placeholder ``execute_action`` with executors that
receive an :class:`ActionContext`, write ordered trace events, and return an
:class:`ActionResult`. Execution stays mock — no real email/news/network — but
it is now *inspectable*: every step lands in ``action_trace_events``.

The registry keys are the **single source of truth** for supported actions;
``app.jobs.actions.SUPPORTED_ACTIONS`` is derived from them.

Required action params are validated in two places (spec 04, section 6):

- ``validate_action_params`` at create/modify time (fast feedback, no trace),
- the executor again at run time (defense in depth; failure is recorded as a
  trace event before the executor raises).

Both read the one :data:`REQUIRED_ACTION_PARAMS` map so the rules cannot drift.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal, Optional, Protocol

from sqlalchemy.orm import Session

from app.core.errors import VALIDATION_ERROR, AppError
from app.core.ids import new_trace_event_id
from app.jobs.models import EVENT_FAILED, EVENT_SUCCEEDED

# Required ``action_params`` keys per action. Actions absent from this map have
# no required params (e.g. the placeholder actions).
REQUIRED_ACTION_PARAMS: dict[str, list[str]] = {
    "send_email": ["to", "subject", "body"],
    "send_reminder": ["text"],
}


@dataclass
class ActionContext:
    trace_id: str
    run_id: str
    job_id: str
    user_id: str
    action: str
    params: dict
    db: Session


@dataclass
class ActionResult:
    status: Literal["succeeded", "failed", "skipped"]
    summary: str
    artifact: Optional[dict] = None


class ActionExecutor(Protocol):
    def execute(self, ctx: ActionContext) -> ActionResult: ...


# ---------------------------------------------------------------------------
# Shared param validation
# ---------------------------------------------------------------------------
def _missing_param(action: str, params: dict) -> str | None:
    """Return the first required param that is missing/blank, else ``None``."""
    for field in REQUIRED_ACTION_PARAMS.get(action, ()):
        value = params.get(field)
        if value is None or (isinstance(value, str) and not value.strip()):
            return field
    return None


def validate_action_params(action: str, params: dict | None) -> None:
    """Raise ``VALIDATION_ERROR`` if a required ``action_params`` field is missing.

    Used at create/modify time so the model gets immediate, fixable feedback
    before any job is stored.
    """
    field = _missing_param(action, params or {})
    if field is not None:
        raise AppError(
            VALIDATION_ERROR,
            f"{action} requires action_params.{field}.",
            field=f"action_params.{field}",
            expected=f"non-empty {field}",
        )


def _append_event(
    ctx: ActionContext,
    *,
    stage: str,
    status: str,
    input: dict | None = None,
    output: dict | None = None,
    error: str | None = None,
) -> None:
    """Append one trace event via the trace repository.

    Imported lazily to avoid a module import cycle (traces -> models -> ...).
    """
    from app.jobs.traces import append_trace_event

    append_trace_event(
        ctx.db,
        ctx.trace_id,
        stage=stage,
        status=status,
        input=input,
        output=output,
        error=error,
    )


# ---------------------------------------------------------------------------
# Executors
# ---------------------------------------------------------------------------
class PlaceholderExecutor:
    """Generic mock executor: records a single ``mock_execute`` event."""

    def __init__(self, action: str) -> None:
        self.action = action

    def execute(self, ctx: ActionContext) -> ActionResult:
        _append_event(
            ctx,
            stage="mock_execute",
            status=EVENT_SUCCEEDED,
            output={"action": self.action, "provider": "mock"},
        )
        return ActionResult(
            status="succeeded",
            summary=f"Mock executed action '{self.action}'.",
        )


class SendEmailMockExecutor:
    """Mock email: validate params, render, mock-send (records an artifact)."""

    def execute(self, ctx: ActionContext) -> ActionResult:
        required = REQUIRED_ACTION_PARAMS["send_email"]
        missing = _missing_param("send_email", ctx.params)
        if missing is not None:
            _append_event(
                ctx,
                stage="validate_action_params",
                status=EVENT_FAILED,
                input={"required": required},
                error=f"Missing {missing}",
            )
            raise AppError(
                VALIDATION_ERROR,
                f"send_email requires action_params.{missing}.",
                field=f"action_params.{missing}",
                expected=f"non-empty email {missing}",
            )

        _append_event(
            ctx,
            stage="validate_action_params",
            status=EVENT_SUCCEEDED,
            input={"required": required},
        )
        _append_event(
            ctx,
            stage="render_email",
            status=EVENT_SUCCEEDED,
            output={"to": ctx.params["to"], "subject": ctx.params["subject"]},
        )
        artifact = {
            "provider": "mock",
            "message_id": f"mock_msg_{new_trace_event_id().split('_', 1)[1][:12]}",
        }
        _append_event(
            ctx, stage="mock_send_email", status=EVENT_SUCCEEDED, output=artifact
        )
        return ActionResult(
            status="succeeded",
            summary=f"Mock email prepared for {ctx.params['to']}.",
            artifact=artifact,
        )


class SendReminderMockExecutor:
    """Mock reminder: validate text, mock-remind."""

    def execute(self, ctx: ActionContext) -> ActionResult:
        missing = _missing_param("send_reminder", ctx.params)
        if missing is not None:
            _append_event(
                ctx,
                stage="validate_action_params",
                status=EVENT_FAILED,
                input={"required": ["text"]},
                error="Missing text",
            )
            raise AppError(
                VALIDATION_ERROR,
                "send_reminder requires action_params.text.",
                field="action_params.text",
                expected="non-empty reminder text",
            )

        text = ctx.params["text"].strip()
        _append_event(
            ctx,
            stage="validate_action_params",
            status=EVENT_SUCCEEDED,
            input={"required": ["text"]},
        )
        _append_event(
            ctx, stage="mock_remind", status=EVENT_SUCCEEDED, output={"text": text}
        )
        return ActionResult(
            status="succeeded", summary=f"Mock reminder prepared: {text[:60]}"
        )


# ---------------------------------------------------------------------------
# Registry — keys are the canonical list of supported actions.
# ---------------------------------------------------------------------------
EXECUTORS: dict[str, ActionExecutor] = {
    "send_email": SendEmailMockExecutor(),
    "send_reminder": SendReminderMockExecutor(),
    "generate_report": PlaceholderExecutor("generate_report"),
    "summarize_financial_news": PlaceholderExecutor("summarize_financial_news"),
    "fetch_news": PlaceholderExecutor("fetch_news"),
    "review_pr": PlaceholderExecutor("review_pr"),
}
