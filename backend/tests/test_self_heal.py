"""Self-Heal engine: real DB-backed event recording + open-problem index,
and the read API that exposes them. Verifies the engine records genuine
recovery events (not fabricated), that RECOVERED events don't linger as
"open problems", and that FAILED/CONFIG_REQUIRED ones do."""
import pytest

from app import models
from app.self_heal import engine as self_heal


def _auth(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}


def test_record_event_sync_persists_a_real_row(db_session):
    row = self_heal.record_event_sync(
        component="database", error_type="SQLITE_LOCK", severity="warning",
        message="test lock event", recovery_action="ROLLBACK_RETRY",
        attempt=2, max_attempts=4, status="RECOVERED", duration_seconds=0.12,
    )
    assert row is not None
    reloaded = db_session.query(models.SelfHealEvent).filter(models.SelfHealEvent.id == row.id).first()
    assert reloaded is not None
    assert reloaded.error_type == "SQLITE_LOCK"
    assert reloaded.attempt == 2 and reloaded.max_attempts == 4
    assert reloaded.status == "RECOVERED"


def test_open_problems_excludes_recovered_but_includes_failed():
    self_heal._LATEST.clear()
    self_heal.record_event_sync(
        component="camera", camera_id="cam_test_ok", error_type="CAMERA_TIMEOUT",
        message="reconnected", status="RECOVERED", recovery_action="RECONNECT",
    )
    self_heal.record_event_sync(
        component="camera", camera_id="cam_test_bad", error_type="CAMERA_TIMEOUT",
        message="reconnect exhausted", status="FAILED", severity="critical", recovery_action="RECONNECT",
    )
    problems = self_heal.open_problems()
    camera_ids = {p.camera_id for p in problems}
    assert "cam_test_bad" in camera_ids
    assert "cam_test_ok" not in camera_ids


def test_a_later_recovered_event_clears_the_open_problem():
    self_heal._LATEST.clear()
    self_heal.record_event_sync(component="worker", camera_id="cam_flap", error_type="WORKER_EXCEPTION", message="crash", status="FAILED")
    assert any(p.camera_id == "cam_flap" for p in self_heal.open_problems())
    self_heal.record_event_sync(component="worker", camera_id="cam_flap", error_type="WORKER_EXCEPTION", message="restarted", status="RECOVERED")
    assert not any(p.camera_id == "cam_flap" for p in self_heal.open_problems())


@pytest.mark.parametrize(
    "exc,expected_type",
    [
        (TimeoutError("timed out"), "TIMEOUT"),
        (ConnectionError("connection reset"), "CONNECTION_ERROR"),
        (ValueError("something else"), "UNKNOWN"),
    ],
)
def test_classify_exception_is_narrow_and_honest(exc, expected_type):
    error_type, severity = self_heal.classify_exception(exc)
    assert error_type == expected_type
    assert severity in ("warning", "critical", "info")


def test_self_heal_health_endpoint_reflects_real_state(client, admin_token):
    resp = client.get("/api/self-heal/health", headers=_auth(admin_token))
    assert resp.status_code == 200
    body = resp.json()
    assert body["subsystems"]["self_heal"] == "ACTIVE"
    assert "summary" in body and "active_problems" in body["summary"]


def test_self_heal_events_endpoint_lists_recorded_events(client, admin_token):
    self_heal.record_event_sync(component="api", error_type="TIMEOUT", message="retried and succeeded", status="RECOVERED", endpoint="/api/test")
    resp = client.get("/api/self-heal/events?component=api", headers=_auth(admin_token))
    assert resp.status_code == 200
    body = resp.json()
    assert body["total"] >= 1
    assert all(e["component"] == "api" for e in body["events"])


def test_self_heal_event_detail_404_for_unknown_id(client, admin_token):
    resp = client.get("/api/self-heal/events/does_not_exist", headers=_auth(admin_token))
    assert resp.status_code == 404


def test_repeated_recovered_events_for_the_same_condition_are_deduped():
    """Audit finding: sustained-but-transient contention on one camera can
    hit-and-recover a lock on nearly every heartbeat — logging every single
    one would drown the Error Log. A repeat RECOVERED for the identical
    (component, camera_id, error_type) within the dedup window is suppressed
    (returns None, no new row); a still-real FAILED for the same key is
    never suppressed."""
    self_heal._last_recovered_at.clear()
    first = self_heal.record_event_sync(
        component="database", camera_id="cam_dedup", error_type="SQLITE_LOCK",
        message="lock 1", status="RECOVERED", severity="warning",
    )
    assert first is not None
    second = self_heal.record_event_sync(
        component="database", camera_id="cam_dedup", error_type="SQLITE_LOCK",
        message="lock 2 — should be suppressed", status="RECOVERED", severity="warning",
    )
    assert second is None

    # A genuine failure for the same key must NEVER be suppressed.
    failed = self_heal.record_event_sync(
        component="database", camera_id="cam_dedup", error_type="SQLITE_LOCK",
        message="lock exhausted", status="FAILED", severity="critical",
    )
    assert failed is not None


def test_self_heal_read_endpoints_require_authentication(client):
    assert client.get("/api/self-heal/health").status_code == 401
    assert client.get("/api/self-heal/problems").status_code == 401
    assert client.get("/api/self-heal/events").status_code == 401
