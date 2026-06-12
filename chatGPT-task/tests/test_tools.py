"""Tool registry dispatch and error envelopes (spec 01, sections 5, 13)."""

from __future__ import annotations

from app.mcp import TOOL_REGISTRY, dispatch

USER = "user_123"

EXPECTED_TOOLS = {
    "task.create@v1",
    "task.list@v1",
    "task.get@v1",
    "task.modify@v1",
    "task.delete@v1",
    "task.trace.get@v1",
}


def test_registry_declares_all_tools_with_callables():
    assert set(TOOL_REGISTRY) == EXPECTED_TOOLS
    assert all(callable(h) for h in TOOL_REGISTRY.values())


def test_dispatch_unknown_tool_returns_error_envelope(isolated_db):
    res = dispatch("task.frobnicate@v1", {})
    assert res["ok"] is False
    assert res["error"]["code"] == "VALIDATION_ERROR"
    assert res["error"]["field"] == "tool"


def test_dispatch_create_then_get_roundtrip(isolated_db):
    created = dispatch(
        "task.create@v1",
        {"user_id": USER, "action": "review_pr", "job_params": {"type": "immediate"}},
    )
    assert created["ok"] is True
    job_id = created["job"]["job_id"]

    got = dispatch("task.get@v1", {"user_id": USER, "job_id": job_id})
    assert got["ok"] is True
    assert got["job"]["job_id"] == job_id


def test_dispatch_create_validation_error_envelope(isolated_db):
    res = dispatch(
        "task.create@v1",
        {"user_id": USER, "action": "review_pr", "job_params": {"type": "bogus"}},
    )
    assert res["ok"] is False
    assert res["error"]["code"] == "VALIDATION_ERROR"


def test_dispatch_unsupported_action_envelope(isolated_db):
    res = dispatch(
        "task.create@v1",
        {"user_id": USER, "action": "fly_me", "job_params": {"type": "immediate"}},
    )
    assert res["ok"] is False
    assert res["error"]["code"] == "UNSUPPORTED_ACTION"
    assert res["error"]["field"] == "action"
    assert "send_reminder" in res["error"]["expected"]


def test_dispatch_create_recurring_with_explicit_timezone(isolated_db):
    created = dispatch(
        "task.create@v1",
        {
            "user_id": USER,
            "action": "review_pr",
            "job_params": {
                "type": "recurring",
                "schedule": "0 8 * * *",
                "timezone": "America/Vancouver",
            },
        },
    )
    assert created["ok"] is True
    assert created["job"]["timezone"] == "America/Vancouver"


def test_dispatch_recurring_without_timezone_is_rejected(isolated_db):
    # Strict scheduler: it rejects, it does not infer a timezone.
    res = dispatch(
        "task.create@v1",
        {
            "user_id": USER,
            "action": "review_pr",
            "job_params": {"type": "recurring", "schedule": "0 8 * * *"},
        },
    )
    assert res["ok"] is False
    assert res["error"]["code"] == "VALIDATION_ERROR"
    assert res["error"]["field"] == "job_params.timezone"


def test_dispatch_modify_and_delete(isolated_db):
    created = dispatch(
        "task.create@v1",
        {
            "user_id": USER,
            "action": "review_pr",
            "job_params": {"type": "recurring", "schedule": "0 8 * * *", "timezone": "UTC"},
        },
    )
    job_id = created["job"]["job_id"]

    modified = dispatch(
        "task.modify@v1",
        {"user_id": USER, "job_id": job_id, "job_params": {"schedule": "30 8 * * *"}},
    )
    assert modified["job"]["schedule"] == "30 8 * * *"
    assert modified["cancelled_run_count"] == 1

    deleted = dispatch("task.delete@v1", {"user_id": USER, "job_id": job_id})
    assert deleted["status"] == "deleted"
