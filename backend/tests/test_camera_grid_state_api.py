"""GET /api/cameras and GET /api/cameras/{id} carry the in-memory lifecycle
diagnostics (grid_state, reconnect_count, last_error) from CAMERA_STATS. Null
if the worker never ran in this process.

The single-camera GET was missing them, so the detail page showed
DISCONNECTED while the camera was processing live video.
"""
from app.pipeline.worker import CAMERA_STATS


def _create_camera(client, admin_token, **overrides):
    payload = {
        "camera_code": "C-DIAG-TEST", "name": "Diag Test", "location": "",
        "source_type": "mock_vms", "source_uri": "",
        "ai_person": True, "ai_vehicle": True, "ai_anpr": True, "camera_group": "",
    }
    payload.update(overrides)
    resp = client.post("/api/cameras", json=payload, headers={"Authorization": f"Bearer {admin_token}"})
    assert resp.status_code == 200, resp.text
    return resp.json()


def test_list_cameras_exposes_grid_state_and_reconnect_diagnostics(client, admin_token, monkeypatch):
    # Same race as below: a real background worker from POST /api/cameras
    # would compete with this test's own CAMERA_STATS write.
    monkeypatch.setattr("app.routers.cameras.start_worker", lambda camera_id: None)
    camera = _create_camera(client, admin_token)
    CAMERA_STATS[camera["id"]] = {
        "grid_state": "RECONNECTING", "reconnects": 3,
        "last_error": "ConnectionError: RTSP handshake timed out",
    }
    try:
        resp = client.get("/api/cameras", headers={"Authorization": f"Bearer {admin_token}"})
        assert resp.status_code == 200, resp.text
        row = next(c for c in resp.json() if c["id"] == camera["id"])
        assert row["grid_state"] == "RECONNECTING"
        assert row["reconnect_count"] == 3
        assert row["last_error"] == "ConnectionError: RTSP handshake timed out"
    finally:
        CAMERA_STATS.pop(camera["id"], None)


def test_get_camera_exposes_grid_state_diagnostics(client, admin_token, monkeypatch):
    """The detail page (/live/[cameraId]) uses this endpoint, not the list."""
    monkeypatch.setattr("app.routers.cameras.start_worker", lambda camera_id: None)
    camera = _create_camera(client, admin_token, camera_code="C-DIAG-TEST-3")
    CAMERA_STATS[camera["id"]] = {
        "grid_state": "PROCESSING", "reconnects": 0, "last_error": None,
    }
    try:
        resp = client.get(f"/api/cameras/{camera['id']}", headers={"Authorization": f"Bearer {admin_token}"})
        assert resp.status_code == 200, resp.text
        row = resp.json()
        assert row["grid_state"] == "PROCESSING"
        assert row["reconnect_count"] == 0
    finally:
        CAMERA_STATS.pop(camera["id"], None)


def test_list_cameras_diagnostics_are_null_when_worker_never_ran(client, admin_token, monkeypatch):
    # create_camera starts a worker, and a real task in the TestClient loop
    # could repopulate grid_state before the GET. Patched at the router's
    # import so the "never ran" case doesn't depend on timing.
    monkeypatch.setattr("app.routers.cameras.start_worker", lambda camera_id: None)
    camera = _create_camera(client, admin_token, camera_code="C-DIAG-TEST-2")
    CAMERA_STATS.pop(camera["id"], None)
    resp = client.get("/api/cameras", headers={"Authorization": f"Bearer {admin_token}"})
    assert resp.status_code == 200, resp.text
    row = next(c for c in resp.json() if c["id"] == camera["id"])
    assert row["grid_state"] is None
    assert row["reconnect_count"] is None
    assert row["last_error"] is None
