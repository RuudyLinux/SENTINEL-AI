"""Camera Control Center bulk endpoint: RBAC enforcement, partial-failure
handling (one bad camera never aborts the batch), duplicate-in-progress
skip, and the one audit-log entry per bulk call."""
import pytest

from app import models
from app.routers import camera_control
from app.security import hash_password, create_access_token


def _auth(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}


@pytest.fixture
def operator_user(db_session):
    role = db_session.query(models.Role).filter(models.Role.name == "Control Room Operator").first()
    if role is None:
        role = models.Role(name="Control Room Operator", description="test")
        db_session.add(role)
        db_session.flush()
    user = db_session.query(models.User).filter(models.User.username == "test_operator_bulk").first()
    if user is None:
        user = models.User(
            username="test_operator_bulk", password_hash=hash_password("testpass123"),
            full_name="Test Operator", role_id=role.id,
        )
        db_session.add(user)
        db_session.commit()
        db_session.refresh(user)
    return user


@pytest.fixture
def operator_token(operator_user):
    return create_access_token(operator_user)


@pytest.fixture
def viewer_user(db_session):
    """Investigator: no camera-control permission per seed.py's role list —
    stands in for "Viewer: no control actions" (Part 7)."""
    role = db_session.query(models.Role).filter(models.Role.name == "Investigator").first()
    if role is None:
        role = models.Role(name="Investigator", description="test")
        db_session.add(role)
        db_session.flush()
    user = db_session.query(models.User).filter(models.User.username == "test_viewer_bulk").first()
    if user is None:
        user = models.User(
            username="test_viewer_bulk", password_hash=hash_password("testpass123"),
            full_name="Test Viewer", role_id=role.id,
        )
        db_session.add(user)
        db_session.commit()
        db_session.refresh(user)
    return user


@pytest.fixture
def viewer_token(viewer_user):
    return create_access_token(viewer_user)


def _make_camera(client, admin_token, code: str) -> str:
    resp = client.post(
        "/api/cameras",
        json={"camera_code": code, "name": code, "source_type": "mock_vms", "source_uri": ""},
        headers=_auth(admin_token),
    )
    assert resp.status_code == 200, resp.text
    return resp.json()["id"]


def test_viewer_cannot_run_bulk_action(client, viewer_token):
    resp = client.post("/api/cameras/bulk", json={"action": "stop", "camera_ids": []}, headers=_auth(viewer_token))
    assert resp.status_code == 403


def test_operator_can_run_bulk_action(client, operator_token, admin_token):
    cam_id = _make_camera(client, admin_token, "C-BULK-OP")
    resp = client.post(
        "/api/cameras/bulk", json={"action": "stop", "camera_ids": [cam_id]}, headers=_auth(operator_token),
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["total"] == 1
    assert body["successful"] == 1


def test_bulk_action_on_unknown_camera_is_a_partial_failure_not_a_500(client, admin_token):
    good_id = _make_camera(client, admin_token, "C-BULK-GOOD")
    resp = client.post(
        "/api/cameras/bulk",
        json={"action": "stop", "camera_ids": [good_id, "cam_does_not_exist"]},
        headers=_auth(admin_token),
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["total"] == 2
    assert body["successful"] == 1
    assert body["failed"] == 1
    outcomes = {r["camera_id"]: r["ok"] for r in body["results"]}
    assert outcomes[good_id] is True
    assert outcomes["cam_does_not_exist"] is False


def test_bulk_action_empty_target_list_is_a_client_error(client, admin_token):
    resp = client.post("/api/cameras/bulk", json={"action": "stop", "camera_ids": []}, headers=_auth(admin_token))
    assert resp.status_code == 400


def test_bulk_action_records_one_audit_log_entry(client, admin_token, db_session):
    cam_id = _make_camera(client, admin_token, "C-BULK-AUDIT")
    before = db_session.query(models.AuditLog).filter(models.AuditLog.action == "bulk_stop_cameras").count()
    resp = client.post("/api/cameras/bulk", json={"action": "stop", "camera_ids": [cam_id]}, headers=_auth(admin_token))
    assert resp.status_code == 200
    after = db_session.query(models.AuditLog).filter(models.AuditLog.action == "bulk_stop_cameras").count()
    assert after == before + 1


def test_camera_already_in_progress_is_skipped_not_double_actioned(client, admin_token):
    """Duplicate-click / overlapping-bulk-op guard (Part 4): a camera_id
    already marked in-progress by another in-flight bulk call is reported as
    skipped, never actioned twice."""
    cam_id = _make_camera(client, admin_token, "C-BULK-INFLIGHT")
    camera_control._IN_PROGRESS.add(cam_id)
    try:
        resp = client.post("/api/cameras/bulk", json={"action": "stop", "camera_ids": [cam_id]}, headers=_auth(admin_token))
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert body["skipped"] == 1
        assert body["successful"] == 0
        assert body["results"][0]["skipped"] is True
    finally:
        camera_control._IN_PROGRESS.discard(cam_id)


def test_disruptive_actions_list_matches_documented_set(client, admin_token):
    resp = client.get("/api/cameras/bulk/disruptive-actions", headers=_auth(admin_token))
    assert resp.status_code == 200
    assert set(resp.json()) == {"restart", "disconnect", "stop"}
