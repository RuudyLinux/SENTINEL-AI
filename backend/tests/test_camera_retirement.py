"""Camera retirement, and camera status being right at boot.

A camera with history can't be deleted (409), and disconnecting didn't
survive a restart since startup resumed every video_file camera, so the demo
cameras came back running AI after every reboot.

Boot reconciliation: a hard restart left cameras "online" with no worker
("4/34 online", 2 running).
"""
import uuid

from app import main, models
from app.pipeline import worker


def _auth(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}


def _camera(db_session, **kw) -> models.Camera:
    cam = models.Camera(
        camera_code=f"C-RET-{uuid.uuid4().hex[:6]}", name="ret", source_type="mock_vms", source_uri="", **kw,
    )
    db_session.add(cam)
    db_session.commit()
    db_session.refresh(cam)
    return cam


def _with_history(db_session, cam: models.Camera) -> None:
    det = models.Detection(camera_id=cam.id, cls="car", confidence=0.9, bbox=[1, 1, 2, 2])
    db_session.add(det)
    db_session.flush()
    alert = models.Alert(camera_id=cam.id, severity="HIGH", detection_id=det.id, reasons=["x"])
    db_session.add(alert)
    db_session.flush()
    db_session.add(models.Evidence(camera_id=cam.id, alert_id=alert.id, evidence_type="snapshot", file_path=""))
    db_session.commit()


def test_camera_with_history_can_be_retired_and_keeps_it(client, admin_token, db_session):
    cam = _camera(db_session)
    _with_history(db_session, cam)
    assert client.delete(f"/api/cameras/{cam.id}", headers=_auth(admin_token)).status_code == 409

    resp = client.post(f"/api/cameras/{cam.id}/retire", headers=_auth(admin_token))
    assert resp.status_code == 200, resp.text
    db_session.expire_all()
    assert db_session.query(models.Detection).filter_by(camera_id=cam.id).count() == 1
    assert db_session.query(models.Alert).filter_by(camera_id=cam.id).count() == 1
    assert db_session.query(models.Evidence).filter_by(camera_id=cam.id).count() == 1

    active = [c["id"] for c in client.get("/api/cameras", headers=_auth(admin_token)).json()]
    assert cam.id not in active
    everything = client.get("/api/cameras?include_retired=true", headers=_auth(admin_token)).json()
    assert any(c["id"] == cam.id and c["retired"] is True for c in everything)
    # the record itself is still reachable for history screens
    assert client.get(f"/api/cameras/{cam.id}", headers=_auth(admin_token)).status_code == 200


def test_retired_camera_cannot_be_started_by_any_route(client, admin_token, db_session):
    cam = _camera(db_session, retired=True)
    for path in (f"/api/cameras/{cam.id}/start", f"/api/cameras/{cam.id}/restart"):
        assert client.post(path, headers=_auth(admin_token)).status_code == 409, path
    for action in ("connect", "start", "start_ai", "restart"):
        body = client.post(
            "/api/cameras/bulk", json={"action": action, "camera_ids": [cam.id]}, headers=_auth(admin_token),
        ).json()
        assert body["successful"] == 0 and body["results"][0]["detail"] == "Camera is retired", action
    assert cam.id not in worker.RUNNING


def test_bulk_all_skips_retired_cameras(client, admin_token, db_session):
    active = _camera(db_session)
    cam = _camera(db_session, retired=True)
    body = client.post("/api/cameras/bulk", json={"action": "stop"}, headers=_auth(admin_token)).json()
    targeted = {r["camera_id"] for r in body["results"]}
    assert active.id in targeted
    assert cam.id not in targeted


def test_worker_loop_refuses_a_retired_camera(db_session):
    import asyncio
    cam = _camera(db_session, retired=True)
    asyncio.run(worker._camera_loop(cam.id))  # returns immediately, never opens a source
    assert worker.CAMERA_STATS.get(cam.id, {}).get("grid_state") is None


def test_startup_never_resumes_a_retired_camera(db_session, monkeypatch):
    active = _camera(db_session)
    retired = _camera(db_session, retired=True)
    started = []
    monkeypatch.setattr(main, "start_worker", started.append)
    main._resume_local_workers(db_session)
    assert active.id in started
    assert retired.id not in started


def test_retired_cameras_are_not_counted_in_statistics(client, admin_token, db_session):
    before = client.get("/api/analytics/overview", headers=_auth(admin_token)).json()["cameras"]["total"]
    _camera(db_session, retired=True, status="online")
    after = client.get("/api/analytics/overview", headers=_auth(admin_token)).json()["cameras"]
    assert after["total"] == before
    health = client.get("/api/self-heal/health", headers=_auth(admin_token)).json()["cameras"]
    assert health["total"] == after["total"]


def test_reinstate_returns_a_camera_to_service(client, admin_token, db_session):
    cam = _camera(db_session, retired=True)
    assert client.post(f"/api/cameras/{cam.id}/reinstate", headers=_auth(admin_token)).status_code == 200
    assert cam.id in [c["id"] for c in client.get("/api/cameras", headers=_auth(admin_token)).json()]


def test_only_an_administrator_can_retire(client, db_session):
    from app.security import create_access_token, hash_password
    role = db_session.query(models.Role).filter_by(name="Control Room Operator").first()
    op = models.User(username=f"op_{uuid.uuid4().hex[:6]}", password_hash=hash_password("x" * 10), full_name="o", role_id=role.id)
    db_session.add(op)
    db_session.commit()
    cam = _camera(db_session)
    token = create_access_token(op)
    assert client.post(f"/api/cameras/{cam.id}/retire", headers=_auth(token)).status_code == 403


def test_boot_marks_every_camera_offline_until_a_worker_reports(db_session):
    """At boot nothing is running yet, so offline is the only true status."""
    online = _camera(db_session, status="online")
    degraded = _camera(db_session, status="degraded")
    main._mark_all_cameras_offline(db_session)
    db_session.expire_all()
    assert db_session.get(models.Camera, online.id).status == "offline"
    assert db_session.get(models.Camera, degraded.id).status == "offline"
