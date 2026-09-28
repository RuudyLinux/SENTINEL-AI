"""Fewer false zone alerts: a confidence floor for alerting, and a tracked
object has to be in the zone on zone_entry_min_frames frames before
zone_entry fires. Detections below the floor are still stored and drawn;
only the alert is held back."""
import asyncio
import uuid

import pytest

from app import models
from app.config import settings
from app.pipeline import rules_engine


@pytest.fixture(autouse=True)
def _defaults(monkeypatch):
    monkeypatch.setattr(settings, "zone_alert_min_confidence", 0.40)
    monkeypatch.setattr(settings, "zone_entry_min_frames", 2)
    rules_engine._alert_claims.clear()
    rules_engine._zone_presence.clear()
    yield
    rules_engine._alert_claims.clear()
    rules_engine._zone_presence.clear()


def _camera_with_zone(db_session):
    camera = models.Camera(camera_code=f"C-ZA-{uuid.uuid4().hex[:10]}", name="zone cam",
                           source_type="mock_vms", source_uri="")
    db_session.add(camera)
    db_session.flush()
    db_session.add(models.Zone(name="Yard", camera_id=camera.id, x1=0.0, y1=0.0, x2=1.0, y2=1.0,
                               severity="HIGH", active=True))
    db_session.commit()
    return camera


def _detect(db_session, camera, confidence=0.9, track_id="7"):
    det = models.Detection(camera_id=camera.id, cls="car", confidence=confidence,
                           bbox=[100, 100, 200, 200], track_id=track_id)
    db_session.add(det)
    db_session.commit()
    return asyncio.run(rules_engine.evaluate(db_session, camera, det, 640, 480))


def test_a_tracked_object_needs_two_frames_in_the_zone(db_session):
    camera = _camera_with_zone(db_session)
    assert _detect(db_session, camera) == []
    alerts = _detect(db_session, camera)
    assert len(alerts) == 1
    assert "Restricted-zone entry" in alerts[0].reasons[0]


def test_a_one_frame_ghost_never_alerts(db_session):
    camera = _camera_with_zone(db_session)
    assert _detect(db_session, camera, track_id="ghost") == []
    assert _detect(db_session, camera, track_id="another-ghost") == []


def test_untracked_detections_are_not_held_back(db_session):
    camera = _camera_with_zone(db_session)
    assert len(_detect(db_session, camera, track_id=None)) == 1


def test_low_confidence_detections_do_not_alert(db_session):
    camera = _camera_with_zone(db_session)
    for _ in range(5):
        assert _detect(db_session, camera, confidence=0.35) == []


def test_low_confidence_frames_still_count_toward_presence(db_session):
    # a car first seen at 0.35 then confidently: the earlier frame counts, so
    # the confident frame fires straight away
    camera = _camera_with_zone(db_session)
    assert _detect(db_session, camera, confidence=0.35) == []
    assert len(_detect(db_session, camera, confidence=0.8)) == 1


def test_one_frame_setting_restores_the_old_behaviour(db_session, monkeypatch):
    monkeypatch.setattr(settings, "zone_entry_min_frames", 1)
    camera = _camera_with_zone(db_session)
    assert len(_detect(db_session, camera)) == 1
