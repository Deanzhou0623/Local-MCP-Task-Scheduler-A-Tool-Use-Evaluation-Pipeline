"""End-to-end REST tests via FastAPI TestClient (spec 01, section 14)."""

from __future__ import annotations

USER = "user_123"


def _create(client, **job_params):
    return client.post(
        "/v1/jobs",
        json={"user_id": USER, "action": "review_pr", "job_params": job_params},
    )


def test_create_immediate_job(client):
    r = _create(client, type="immediate")
    assert r.status_code == 200
    body = r.json()
    assert body["ok"] is True
    assert body["job"]["type"] == "immediate"


def test_create_one_time_job(client):
    r = _create(client, type="one_time", time="2026-06-10T09:00:00", timezone="UTC")
    assert r.status_code == 200
    assert r.json()["job"]["type"] == "one_time"


def test_create_recurring_job(client):
    r = _create(client, type="recurring", schedule="0 8 * * *", timezone="UTC")
    assert r.status_code == 200
    assert r.json()["job"]["schedule"] == "0 8 * * *"


def test_reject_recurring_without_schedule(client):
    r = _create(client, type="recurring", timezone="UTC")
    assert r.status_code == 422
    assert r.json()["error"]["code"] == "VALIDATION_ERROR"


def test_reject_recurring_without_timezone(client):
    r = _create(client, type="recurring", schedule="0 8 * * *")
    assert r.status_code == 422
    body = r.json()
    assert body["error"]["field"] == "job_params.timezone"


def test_reject_one_time_without_time(client):
    r = _create(client, type="one_time", timezone="UTC")
    assert r.status_code == 422
    assert r.json()["error"]["field"] == "job_params.time"


def test_reject_extra_field(client):
    r = client.post(
        "/v1/jobs",
        json={
            "user_id": USER,
            "action": "review_pr",
            "job_params": {"type": "immediate"},
            "extra": "x",
        },
    )
    assert r.status_code == 422
    assert r.json()["error"]["code"] == "VALIDATION_ERROR"


def test_list_and_filter_by_status(client):
    _create(client, type="immediate")
    r = client.get("/v1/jobs", params={"user_id": USER})
    assert r.status_code == 200
    assert r.json()["pagination"]["total"] == 1

    r2 = client.get("/v1/jobs", params={"user_id": USER, "status": "scheduled"})
    assert r2.json()["pagination"]["total"] == 1


def test_get_job_detail(client):
    job_id = _create(client, type="immediate").json()["job"]["job_id"]
    r = client.get(f"/v1/jobs/{job_id}", params={"user_id": USER})
    assert r.status_code == 200
    assert len(r.json()["runs"]) == 1


def test_modify_recurring_schedule(client):
    job_id = _create(
        client, type="recurring", schedule="0 8 * * *", timezone="UTC"
    ).json()["job"]["job_id"]
    r = client.patch(
        f"/v1/jobs/{job_id}",
        json={"user_id": USER, "job_params": {"schedule": "30 8 * * *"}},
    )
    assert r.status_code == 200
    body = r.json()
    assert body["job"]["schedule"] == "30 8 * * *"
    assert body["cancelled_run_count"] == 1


def test_modify_deleted_job_returns_conflict(client):
    job_id = _create(client, type="immediate").json()["job"]["job_id"]
    client.request("DELETE", f"/v1/jobs/{job_id}", json={"user_id": USER})
    r = client.patch(
        f"/v1/jobs/{job_id}", json={"user_id": USER, "action": "send_email"}
    )
    assert r.status_code == 409
    assert r.json()["error"]["code"] == "CONFLICT"


def test_delete_job_cancels_runs(client):
    job_id = _create(
        client, type="recurring", schedule="0 8 * * *", timezone="UTC"
    ).json()["job"]["job_id"]
    r = client.request("DELETE", f"/v1/jobs/{job_id}", json={"user_id": USER})
    assert r.status_code == 200
    body = r.json()
    assert body["status"] == "deleted"
    assert body["cancelled_run_count"] == 1


def test_get_missing_job_returns_404(client):
    r = client.get("/v1/jobs/job_nope", params={"user_id": USER})
    assert r.status_code == 404
    assert r.json()["error"]["code"] == "NOT_FOUND"


# --- Trace read surface (spec 04) -----------------------------------------
def test_get_trace_over_rest(client, isolated_db):
    """Create a job over REST, run it, then read its trace over REST."""
    from app.jobs.models import ActionTrace
    from app.scheduler import process_run

    created = client.post(
        "/v1/jobs",
        json={
            "user_id": USER,
            "action": "send_reminder",
            "job_params": {"type": "immediate"},
            "action_params": {"text": "Stand up and stretch."},
        },
    ).json()
    run_id = created["next_run"]["run_id"]

    session = isolated_db()
    try:
        process_run(session, run_id)
        trace_id = (
            session.query(ActionTrace).filter_by(run_id=run_id).one().trace_id
        )
    finally:
        session.close()

    r = client.get(f"/v1/traces/{trace_id}", params={"user_id": USER})
    assert r.status_code == 200
    body = r.json()
    assert body["ok"] is True
    assert body["trace"]["status"] == "succeeded"
    assert [e["stage"] for e in body["trace"]["events"]] == [
        "validate_action_params",
        "mock_remind",
    ]


def test_get_trace_wrong_user_returns_403(client, isolated_db):
    from app.jobs.models import ActionTrace
    from app.scheduler import process_run

    created = client.post(
        "/v1/jobs",
        json={
            "user_id": USER,
            "action": "generate_report",
            "job_params": {"type": "immediate"},
        },
    ).json()
    run_id = created["next_run"]["run_id"]

    session = isolated_db()
    try:
        process_run(session, run_id)
        trace_id = (
            session.query(ActionTrace).filter_by(run_id=run_id).one().trace_id
        )
    finally:
        session.close()

    r = client.get(f"/v1/traces/{trace_id}", params={"user_id": "intruder"})
    assert r.status_code == 403
    assert r.json()["error"]["code"] == "PERMISSION_DENIED"
