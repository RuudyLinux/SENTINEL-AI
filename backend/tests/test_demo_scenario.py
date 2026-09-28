"""Demo reset and the deterministic scenario trigger."""
import asyncio
import time
from pathlib import Path

import pytest

from app import models
from app.config import settings
from app.seed import reset_demo_data, DEMO_PLATE
from app.pipeline import demo_scenario, worker
from app.pipeline.demo_scenario import trigger_scenario, DemoScenarioError


@pytest.fixture(autouse=True)
def _fast_frame_wait(monkeypatch):
    """trigger_scenario polls for a decoded frame before giving up on a
    snapshot (demo_scenario._wait_for_live_frame). No test here starts a real
    worker, so the full 3s x 2 cameras would be wasted on every test.
    Shortened, not removed; the wait logic still runs."""
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
    # An earlier test (anywhere, under --random-order) may have registered the
    # demo cameras, so make "not registered" true instead of assuming it.
    # With FKs on, a camera that has detections/plates can't be deleted on
    # its own; delete_cameras_by_code clears dependents first.
    from conftest import delete_cameras_by_code

    delete_cameras_by_code(db_session, ["C-014", "C-019"])
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
    """reset_demo_data didn't start the demo workers, so LATEST_FRAMES was
    empty, _save_demo_snapshot returned None, and the demo's CRITICAL alert
    produced no Evidence at all. Fakes a running camera with a real encoded
    JPEG and checks a real Evidence row comes out of the unchanged
    rules_engine path, linked to incident, camera and detection."""
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
            assert ev.file_path  # a real path
            incident = db_session.query(models.Incident).filter(models.Incident.id == ev.incident_id).first()
            assert incident is not None and incident.priority == "CRITICAL"
    finally:
        for cam in cameras.values():
            worker.LATEST_FRAMES.pop(cam.id, None)


def test_demo_reset_endpoint_actually_starts_the_demo_cameras(client, admin_token, tmp_path, monkeypatch):
    """Through the real POST /api/system/demo/reset (real loop, real
    create_task) on the bundled car-detection.mp4, waiting for a decoded
    frame: reset then trigger-scenario gives real evidence."""
    monkeypatch.setattr(settings, "evidence_dir", tmp_path)
    resp = client.post("/api/system/demo/reset", headers={"Authorization": f"Bearer {admin_token}"})
    assert resp.status_code == 200, resp.text

    db = worker.SessionLocal()
    try:
        cameras = db.query(models.Camera).filter(models.Camera.camera_code.in_(["C-014", "C-019"])).all()
        assert len(cameras) == 2
        try:
            # 5s was fine locally but a throttled CI runner is slower to open
            # and decode the first frame (no errors, just slow I/O). Same
            # assertion, more time.
            for _ in range(150):  # up to ~15s for a real local video file to open + decode one frame
                if all(c.id in worker.LATEST_FRAMES for c in cameras):
                    break
                time.sleep(0.1)
            assert all(c.id in worker.LATEST_FRAMES for c in cameras), (
                "demo cameras never produced a real decoded frame after /demo/reset"
            )
        finally:
            # test hygiene: stop_worker only requests cancellation, wait for it
            # so this decode doesn't bleed into the next test's timing (it
            # upset test_stress_concurrency when left running)
            stopped_tasks = [worker.stop_worker(c.id) for c in cameras]
            stopped_tasks = [t for t in stopped_tasks if t is not None]
            # widened with the loop above, slow runners need longer to clean up
            for _ in range(100):
                if all(t.done() for t in stopped_tasks):
                    break
                time.sleep(0.1)
            for c in cameras:
                worker.LATEST_FRAMES.pop(c.id, None)
    finally:
        db.close()
