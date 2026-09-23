"""Self-Heal engine: real DB-backed event recording + open-problem index,
and the read API that exposes them. Verifies the engine records genuine
recovery events (not fabricated), that RECOVERED events don't linger as
"open problems", and that FAILED/CONFIG_REQUIRED ones do."""
import pytest

from app import models
from app.self_heal import engine as self_heal


def _auth(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}


@pytest.fixture
def real_cameras(db_session):
    """SelfHealEvent.camera_id is a real FOREIGN KEY to cameras.id. These
    tests used to invent ids ("cam_test_bad", "cam_dedup") that no camera row
    ever had — which only worked while SQLite silently ignored foreign keys.
    With `PRAGMA foreign_keys=ON` (app/db.py, matching what PostgreSQL has
    always enforced) those inserts are rejected, and `record_event_sync` —
    correctly, being best-effort and never-raising — swallowed the failure,
    so the events simply vanished and the assertions saw an empty set.

    Creating the cameras for real is what production actually does: a
    self-heal event is always recorded against a camera that exists.
    """
    created = []
    for camera_id in ("cam_test_ok", "cam_test_bad", "cam_flap", "cam_dedup"):
        if db_session.query(models.Camera).filter(models.Camera.id == camera_id).first() is None:
            camera = models.Camera(
                id=camera_id, camera_code=f"SH-{camera_id}", name=camera_id,
                source_type="video_file", source_uri="x.mp4",
            )
            db_session.add(camera)
            created.append(camera)
    db_session.commit()
    yield
    for camera in created:
        db_session.query(models.SelfHealEvent).filter(
            models.SelfHealEvent.camera_id == camera.id
        ).delete(synchronize_session=False)
        db_session.delete(camera)
    db_session.commit()


def test_record_event_sync_persists_a_real_row(db_session):
    # `_recovered_claims` is module-global dedup state: a RECOVERED event for
    # the same (component, camera_id, error_type) recorded recently by ANY
    # earlier test suppresses this one, and record_event_sync then correctly
    # returns None. The concurrency tests genuinely produce
    # ("database", None, "SQLITE_LOCK") events, which is exactly this key.
    # Caught by the --random-order gate; invisible under alphabetical order.
    self_heal._recovered_claims.clear()
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


def test_open_problems_excludes_recovered_but_includes_failed(real_cameras):
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


def test_a_later_recovered_event_clears_the_open_problem(real_cameras):
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


def test_repeated_recovered_events_for_the_same_condition_are_deduped(real_cameras):
    """Audit finding: sustained-but-transient contention on one camera can
    hit-and-recover a lock on nearly every heartbeat — logging every single
    one would drown the Error Log. A repeat RECOVERED for the identical
    (component, camera_id, error_type) within the dedup window is suppressed
    (returns None, no new row); a still-real FAILED for the same key is
    never suppressed."""
    self_heal._recovered_claims.clear()
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
