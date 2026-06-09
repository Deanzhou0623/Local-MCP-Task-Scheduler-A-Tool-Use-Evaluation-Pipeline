"""Supported task actions and the Spec 01 placeholder executor.

An ``action`` names the work a job performs. Spec 01 does not call real
external APIs (that is Feature 04); execution is a deterministic placeholder so
the watcher/worker pipeline can be exercised end to end. The allow-list backs
the ``UNSUPPORTED_ACTION`` error: an LLM that invents an action name must be
rejected, not silently accepted.
"""

from __future__ import annotations

# Allow-list of executable actions. Keep this small and explicit so eval
# datasets can target known-good action names.
SUPPORTED_ACTIONS: frozenset[str] = frozenset(
    {
        "summarize_financial_news",
        "review_pr",
        "send_email",
        "fetch_news",
        "generate_report",
    }
)


def is_supported(action: str) -> bool:
    return action in SUPPORTED_ACTIONS


def execute_action(action: str, *, job_id: str) -> str:
    """Run the placeholder action and return a human-readable result string.

    Feature 04 replaces this with real external API calls.
    """
    return f"[placeholder] executed action '{action}' for {job_id}"
