"""GET /api/cameras and GET /api/cameras/{id} both expose in-memory
connection-lifecycle diagnostics (grid_state, reconnect_count, last_error)
from worker.CAMERA_STATS — the 24/7 auto-connect supervisor's UI depends on
this to distinguish REGISTERED/CONNECTED/PROCESSING/RECONNECTING/etc. from
the stable DB `status` column. Null when a camera's worker has never run in
this process, never fabricated.

The singular GET was found missing this attachment via the final freeze
browser smoke test: the single-camera detail page read grid_state as always
absent and rendered DISCONNECTED even while a camera was genuinely
PROCESSING with real video and detections flowing — see
test_get_camera_exposes_grid_state_diagnostics below."""
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
    """The single-camera detail page (/live/[cameraId]) hits this endpoint,
    not the list one — it must carry the same real-time diagnostics."""
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
    # POST /api/cameras itself calls start_worker() unconditionally on create
    # (routers/cameras.py) — a real background asyncio task in the TestClient's
    # own event loop would race a bare CAMERA_STATS.pop() after the fact (it
    # can repopulate grid_state before the GET below runs). Patched out at
    # the router's import site instead, so this test proves the "never ran"
    # case deterministically rather than by timing luck.
    monkeypatch.setattr("app.routers.cameras.start_worker", lambda camera_id: None)
    camera = _create_camera(client, admin_token, camera_code="C-DIAG-TEST-2")
    CAMERA_STATS.pop(camera["id"], None)
    resp = client.get("/api/cameras", headers={"Authorization": f"Bearer {admin_token}"})
    assert resp.status_code == 200, resp.text
    row = next(c for c in resp.json() if c["id"] == camera["id"])
    assert row["grid_state"] is None
    assert row["reconnect_count"] is None
    assert row["last_error"] is None
