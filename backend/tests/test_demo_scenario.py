"""Phase 6 — demo reset + deterministic scenario trigger."""
import asyncio

import pytest

from app import models
from app.config import settings
from app.seed import reset_demo_data, DEMO_CAMERAS, DEMO_PLATE
from app.pipeline.demo_scenario import trigger_scenario, DemoScenarioError


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
