"""Confidence-aware intelligence (10/10 roadmap P5): a watchlist match must not
carry the same alert severity when the underlying plate read is uncertain.
Before this, `rules_engine.evaluate` set severity=CRITICAL for ANY
`vehicle.watchlist_flag`, regardless of `vehicle.plate_confidence` — a 48%
guess produced an identical alert to a 94% confident read. The match itself
must still fire (never silence a real watchlist hit for being uncertain);
only its severity and reason string change.
"""
import asyncio
import uuid

import pytest

from app import models
from app.config import settings
from app.pipeline import rules_engine


@pytest.fixture(autouse=True)
def _clean_cooldowns():
    rules_engine._last_alert_at.clear()
    rules_engine._zone_presence.clear()
    yield
    rules_engine._last_alert_at.clear()
    rules_engine._zone_presence.clear()


def _camera(db) -> models.Camera:
    camera = models.Camera(
        camera_code=f"WLC-{uuid.uuid4().hex[:8]}", name="watchlist confidence cam",
        source_type="video_file", source_uri="x.mp4",
    )
    db.add(camera)
    db.flush()
    return camera


def _watchlisted_vehicle(db, plate: str, plate_confidence: float) -> models.Vehicle:
    db.add(models.WatchlistEntry(
        entity_type="plate", identifier=plate, priority="CRITICAL", active=True, reason="test",
    ))
    vehicle = models.Vehicle(plate_text=plate, plate_confidence=plate_confidence, watchlist_flag=True)
    db.add(vehicle)
    db.flush()
    return vehicle


def _detection(db, camera) -> models.Detection:
    detection = models.Detection(
        camera_id=camera.id, cls="car", confidence=0.88, bbox=[10.0, 10.0, 90.0, 90.0],
        track_id="9001", snapshot_path="/evidence/test.jpg",
    )
    db.add(detection)
    db.flush()
    return detection


def test_high_confidence_watchlist_match_is_critical(db_session):
    camera = _camera(db_session)
    vehicle = _watchlisted_vehicle(db_session, f"GJ05HC{uuid.uuid4().hex[:4].upper()}", plate_confidence=0.94)
    alerts = asyncio.run(rules_engine.evaluate(db_session, camera, _detection(db_session, camera), 100, 100, vehicle))

    assert len(alerts) == 1
    assert alerts[0].severity == "CRITICAL"
    assert "LOW CONFIDENCE" not in alerts[0].reasons[0]


def test_low_confidence_watchlist_match_is_capped_at_high_but_still_fires(db_session):
    camera = _camera(db_session)
    low_confidence = settings.watchlist_high_confidence_floor - 0.10
    assert low_confidence > settings.plate_min_confidence  # still a real, gate-passing read — not garbage
    vehicle = _watchlisted_vehicle(db_session, f"GJ05LC{uuid.uuid4().hex[:4].upper()}", plate_confidence=low_confidence)
    alerts = asyncio.run(rules_engine.evaluate(db_session, camera, _detection(db_session, camera), 100, 100, vehicle))

    # Never silenced — a real watchlist hit still produces an alert.
    assert len(alerts) == 1
    assert alerts[0].severity == "HIGH"
    assert "LOW CONFIDENCE" in alerts[0].reasons[0]
    assert "requires confirmation" in alerts[0].reasons[0]


def test_low_confidence_match_still_escalates_to_critical_via_other_signals(db_session):
    """The confidence cap only limits the WATCHLIST rule's own contribution.
    A separately-earned CRITICAL (e.g. a restricted zone entered in the same
    evaluation) must still be able to raise the final severity — the risk
    score is a floor-raising mechanism, never suppressed by this cap."""
    camera = _camera(db_session)
    zone = models.Zone(
        name="Secure Yard", camera_id=camera.id, x1=0.0, y1=0.0, x2=1.0, y2=1.0,
        active=True, severity="CRITICAL",
    )
    db_session.add(zone)
    db_session.flush()

    low_confidence = settings.watchlist_high_confidence_floor - 0.10
    vehicle = _watchlisted_vehicle(db_session, f"GJ05ZC{uuid.uuid4().hex[:4].upper()}", plate_confidence=low_confidence)
    alerts = asyncio.run(rules_engine.evaluate(db_session, camera, _detection(db_session, camera), 100, 100, vehicle))

    assert len(alerts) == 1
    # zone_entry's own CRITICAL severity still wins, independent of the watchlist cap.
    assert alerts[0].severity == "CRITICAL"
    assert any("LOW CONFIDENCE" in r for r in alerts[0].reasons)
