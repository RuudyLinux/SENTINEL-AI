"""Restarting a grid camera, via the single-camera or the bulk endpoint,
leaves it registered with the supervisor, not just restarted. Both go through
supervisor.restart; each entry point is tested separately.

worker.start_worker/stop_worker are faked (like test_supervisor.py). A real
grid worker goes through SentinelGridAdapter, which even without credentials
held a to_thread slot for a multi-second timeout that cancel() can't
interrupt, and that slowed and destabilized later tests on the same
executor. This is about AUTO_MANAGED/OPERATOR_DISCONNECTED bookkeeping; the
adapter has its own tests.
"""
from app import models
from app.pipeline import supervisor


def _auth(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}


def _make_grid_camera(db_session, code: str) -> models.Camera:
    cam = models.Camera(
        camera_code=code, name=code, source_type="sentinel_grid",
        source_uri=code.lower(), status="offline",
    )
    db_session.add(cam)
    db_session.commit()
    db_session.refresh(cam)
    return cam


def _reset_supervisor_state():
    supervisor.AUTO_MANAGED.clear()
    supervisor.OPERATOR_DISCONNECTED.clear()


def _mock_worker(monkeypatch):
    started, stopped = [], []
    monkeypatch.setattr(supervisor.worker, "start_worker", lambda cid: started.append(cid))
    monkeypatch.setattr(supervisor.worker, "stop_worker", lambda cid: stopped.append(cid))
    return started, stopped


def test_single_camera_restart_endpoint_rejoins_grid_camera_to_auto_managed(client, admin_token, db_session, monkeypatch):
    _reset_supervisor_state()
    started, stopped = _mock_worker(monkeypatch)
    cam = _make_grid_camera(db_session, "C-RESTART-SINGLE")
    # Realistic precondition: operator had disconnected it before.
    supervisor.OPERATOR_DISCONNECTED.add(cam.id)

    try:
        resp = client.post(f"/api/cameras/{cam.id}/restart", headers=_auth(admin_token))
        assert resp.status_code == 200, resp.text

        # restart clears OPERATOR_DISCONNECTED and rejoins AUTO_MANAGED, so the
        # next sweep manages it again
        assert cam.id not in supervisor.OPERATOR_DISCONNECTED
        assert cam.id in supervisor.AUTO_MANAGED
        assert stopped == [cam.id]
        assert started == [cam.id]
    finally:
        _reset_supervisor_state()


def test_bulk_restart_endpoint_rejoins_grid_camera_to_auto_managed(client, admin_token, db_session, monkeypatch):
    _reset_supervisor_state()
    started, stopped = _mock_worker(monkeypatch)
    cam = _make_grid_camera(db_session, "C-RESTART-BULK")
    supervisor.OPERATOR_DISCONNECTED.add(cam.id)

    try:
        resp = client.post(
            "/api/cameras/bulk", json={"action": "restart", "camera_ids": [cam.id]}, headers=_auth(admin_token),
        )
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert body["successful"] == 1

        assert cam.id not in supervisor.OPERATOR_DISCONNECTED
        assert cam.id in supervisor.AUTO_MANAGED
        assert stopped == [cam.id]
        assert started == [cam.id]
    finally:
        _reset_supervisor_state()


def test_restart_rbac_still_enforced_for_both_endpoints(client, db_session, monkeypatch):
    """Both entry points still need Administrator/Control Room Operator."""
    from app.security import hash_password, create_access_token

    _mock_worker(monkeypatch)
    _reset_supervisor_state()

    role = db_session.query(models.Role).filter(models.Role.name == "Investigator").first()
    if role is None:
        role = models.Role(name="Investigator", description="test")
        db_session.add(role)
        db_session.flush()
    viewer = db_session.query(models.User).filter(models.User.username == "test_restart_viewer").first()
    if viewer is None:
        viewer = models.User(
            username="test_restart_viewer", password_hash=hash_password("testpass123"),
            full_name="Test Viewer", role_id=role.id,
        )
        db_session.add(viewer)
        db_session.commit()
        db_session.refresh(viewer)
    token = create_access_token(viewer)

    cam = _make_grid_camera(db_session, "C-RESTART-RBAC")
    try:
        assert client.post(f"/api/cameras/{cam.id}/restart", headers=_auth(token)).status_code == 403
        assert client.post(
            "/api/cameras/bulk", json={"action": "restart", "camera_ids": [cam.id]}, headers=_auth(token),
        ).status_code == 403
        # RBAC rejected both before ever reaching supervisor.restart().
        assert cam.id not in supervisor.AUTO_MANAGED
    finally:
        _reset_supervisor_state()
