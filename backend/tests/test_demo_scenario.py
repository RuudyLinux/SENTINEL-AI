"""Phase 6 — demo reset + deterministic scenario trigger."""
import asyncio
import time
from pathlib import Path

import pytest

from app import models
from app.config import settings
from app.seed import reset_demo_data, DEMO_CAMERAS, DEMO_PLATE
from app.pipeline import demo_scenario, worker
from app.pipeline.demo_scenario import trigger_scenario, DemoScenarioError


@pytest.fixture(autouse=True)
def _fast_frame_wait(monkeypatch):
    """Real-evidence-workflow fix (final-demo-readiness phase): trigger_scenario
    now waits (bounded, real polling — see demo_scenario._wait_for_live_frame)
    for the camera's worker to have decoded at least one frame before giving
    up on a snapshot. None of these tests start a real worker, so without
    this the whole suite would burn the full real timeout (3s * 2 cameras)
    on every test in this file for no reason. Shortened here, not removed —
    the wait logic itself is still exercised, just on a fast clock."""
    monkeypatch.setattr(demo_scenario, "DEMO_FRAME_WAIT_TIMEOUT_S", 0.05)


def test_reset_demo_data_refuses_outside_demo_mode(db_session, monkeypatch):
    monkeypatch.setattr(settings, "demo_mode", False)
    with pytest.raises(RuntimeError):
        reset_demo_data(db_session)


def test_reset_demo_data_creates_demo_cameras_and_watchlist(db_session, monkeypatch):
    monkeypatch.setattr(settings, "demo_mode", True)
    summary = reset_demo_data(db_session)
    assert summary["cameras"] == ["C-014", "C-019"]

    codes = {c.camera_code for c in db_session.query(models.Camera).all()}
    assert {"C-014", "C-019"} <= codes
    wl = db_session.query(models.WatchlistEntry).filter(models.WatchlistEntry.identifier == DEMO_PLATE).first()
    assert wl is not None and wl.active is True


def test_reset_demo_data_wipes_transactional_data_without_duplicating_cameras(db_session, monkeypatch):
    monkeypatch.setattr(settings, "demo_mode", True)
    reset_demo_data(db_session)
    camera = db_session.query(models.Camera).filter(models.Camera.camera_code == "C-014").first()
    db_session.add(models.Alert(camera_id=camera.id, severity="HIGH", reasons=["test"]))
    db_session.commit()
    assert db_session.query(models.Alert).count() == 1

    reset_demo_data(db_session)
    assert db_session.query(models.Alert).count() == 0
    # re-running reset doesn't duplicate the camera rows
    assert db_session.query(models.Camera).filter(models.Camera.camera_code == "C-014").count() == 1


def test_trigger_scenario_refuses_outside_demo_mode(db_session, admin_user, monkeypatch):
    monkeypatch.setattr(settings, "demo_mode", False)
    with pytest.raises(DemoScenarioError):
        asyncio.run(trigger_scenario(db_session, admin_user))


def test_trigger_scenario_requires_demo_cameras_registered(db_session, admin_user, monkeypatch):
    monkeypatch.setattr(settings, "demo_mode", True)
    # Other tests in this module may have already registered the demo
    # cameras in this shared test DB — remove them so this test's
    # precondition ("not registered yet") actually holds.
    db_session.query(models.Camera).filter(models.Camera.camera_code.in_(["C-014", "C-019"])).delete(synchronize_session=False)
    db_session.commit()
    with pytest.raises(DemoScenarioError, match="not registered|Demo cameras"):
        asyncio.run(trigger_scenario(db_session, admin_user))


def test_trigger_scenario_produces_real_cross_camera_correlation(db_session, admin_user, monkeypatch):
    monkeypatch.setattr(settings, "demo_mode", True)
    reset_demo_data(db_session)

    result = asyncio.run(trigger_scenario(db_session, admin_user))

    assert result["plate"] == DEMO_PLATE
    assert [s["camera_code"] for s in result["sightings"]] == ["C-014", "C-019"]
    # a CRITICAL watchlist alert really fired on each real evaluate() call
    for sighting in result["sightings"]:
        severities = [a["severity"] for a in sighting["alerts"]]
        assert "CRITICAL" in severities

    # real Detection rows exist, clearly labeled as demo-sourced, not a real inference
    demo_detections = db_session.query(models.Detection).filter(models.Detection.model_version == "demo-fixture").all()
    assert len(demo_detections) == 2

    # real Incident(s) auto-created by the real rules engine
    assert db_session.query(models.Incident).count() >= 1

    # route has both cameras in chronological order
    assert [s["camera_code"] for s in result["route"]] == ["C-014", "C-019"]


def test_trigger_scenario_produces_real_evidence_when_camera_has_a_live_frame(db_session, admin_user, monkeypatch, tmp_path):
    """Final-demo-readiness-phase fix: reset_demo_data alone never started
    the demo cameras' workers, so worker.LATEST_FRAMES was always empty and
    _save_demo_snapshot always returned None — the ENTIRE real evidence
    chain (Detection.snapshot_path -> rules_engine.evaluate's own real
    `if detection.snapshot_path: Evidence(...)`) never fired, so the demo's
    flagship CRITICAL alert produced zero Evidence rows. This simulates
    "the camera is actually running" the same way other tests fake a real
    decoded frame (a real JPEG-encoded array, not an empty/placeholder
    value) and proves a REAL Evidence row comes out the other end, correctly
    linked to the incident/camera/detection — through the unmodified,
    already-real rules_engine code path, not a new evidence-creation path
    written for the demo."""
    import cv2
    import numpy as np

    monkeypatch.setattr(settings, "demo_mode", True)
    monkeypatch.setattr(settings, "evidence_dir", tmp_path)
    reset_demo_data(db_session)

    cameras = {c.camera_code: c for c in db_session.query(models.Camera).filter(models.Camera.camera_code.in_(["C-014", "C-019"])).all()}
    frame = np.zeros((64, 64, 3), dtype=np.uint8)
    ok, buf = cv2.imencode(".jpg", frame)
    assert ok
    for cam in cameras.values():
        worker.LATEST_FRAMES[cam.id] = buf.tobytes()

    try:
        result = asyncio.run(trigger_scenario(db_session, admin_user))

        for sighting in result["sightings"]:
            assert sighting["snapshot_path"], f"expected a real snapshot for {sighting['camera_code']}"
            assert (tmp_path / Path(sighting["snapshot_path"]).name).exists()

        evidence_rows = db_session.query(models.Evidence).all()
        assert len(evidence_rows) >= 1, "rules_engine.evaluate should have auto-created Evidence once snapshot_path was real"
        for ev in evidence_rows:
            assert ev.incident_id is not None  # incident association
            assert ev.camera_id in {c.id for c in cameras.values()}  # camera ID
            assert ev.file_path  # real file path, not fabricated metadata
            incident = db_session.query(models.Incident).filter(models.Incident.id == ev.incident_id).first()
            assert incident is not None and incident.priority == "CRITICAL"
    finally:
        for cam in cameras.values():
            worker.LATEST_FRAMES.pop(cam.id, None)


def test_demo_reset_endpoint_actually_starts_the_demo_cameras(client, admin_token, tmp_path, monkeypatch):
    """HTTP-level regression test for the root cause: reset_demo_data()
    alone never started a worker for either demo camera, so
    worker.LATEST_FRAMES stayed empty forever and the demo's real evidence
    chain never had a snapshot to work with. Drives the REAL
    POST /api/system/demo/reset endpoint (real event loop, real
    asyncio.create_task) against the REAL bundled
    uploads/car-detection.mp4 and waits briefly for a real decoded frame —
    this is the concrete, observable proof that a judge calling reset then
    trigger-scenario moments later gets real evidence, not an empty demo."""
    monkeypatch.setattr(settings, "evidence_dir", tmp_path)
    resp = client.post("/api/system/demo/reset", headers={"Authorization": f"Bearer {admin_token}"})
    assert resp.status_code == 200, resp.text

    db = worker.SessionLocal()
    try:
        cameras = db.query(models.Camera).filter(models.Camera.camera_code.in_(["C-014", "C-019"])).all()
        assert len(cameras) == 2
        try:
            for _ in range(50):  # up to ~5s for a real local video file to open + decode one frame
                if all(c.id in worker.LATEST_FRAMES for c in cameras):
                    break
                time.sleep(0.1)
            assert all(c.id in worker.LATEST_FRAMES for c in cameras), (
                "demo cameras never produced a real decoded frame after /demo/reset"
            )
        finally:
            # Test-isolation hygiene, not a product fix: stop_worker() only
            # REQUESTS cancellation (see supervisor.py's own audit-finding
            # comment) — waiting here for it to actually finish before this
            # test returns keeps this real video-decode background activity
            # from bleeding into a LATER test's own timing (observed: left
            # running, it destabilized test_stress_concurrency's tight
            # real-concurrency assertions when run immediately after).
            stopped_tasks = [worker.stop_worker(c.id) for c in cameras]
            stopped_tasks = [t for t in stopped_tasks if t is not None]
            for _ in range(30):
                if all(t.done() for t in stopped_tasks):
                    break
                time.sleep(0.1)
            for c in cameras:
                worker.LATEST_FRAMES.pop(c.id, None)
    finally:
        db.close()
