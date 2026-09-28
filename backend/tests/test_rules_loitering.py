"""Loitering rules and the zone schedule window. Neither worked before
(schedule_start/end were never read, loitering didn't exist). Same clock
monkeypatching as test_alert_dedup.py."""
import pytest
import asyncio
import uuid
from datetime import datetime

from app.pipeline import rules_engine
from app import models


@pytest.fixture(autouse=True)
def _one_frame_zone_entry(monkeypatch):
    # these tests fire a zone alert from one tracked frame; the multi-frame
    # confirmation has its own tests in test_zone_alert_accuracy.py
    from app.config import settings as _settings
    monkeypatch.setattr(_settings, "zone_entry_min_frames", 1)


def _make_camera_and_zone(db_session, **zone_kwargs):
    # uuid4, not id(): CPython reuses ids of short-lived objects and that gave
    # camera_code collisions in the shared DB
    camera = models.Camera(camera_code=f"C-TEST-{uuid.uuid4().hex[:10]}", name="Test Cam", source_type="mock_vms", source_uri="")
    db_session.add(camera)
    db_session.flush()
    zone = models.Zone(
        name="Test Zone", camera_id=camera.id, x1=0.0, y1=0.0, x2=1.0, y2=1.0,
        active=True, **zone_kwargs,
    )
    db_session.add(zone)
    db_session.flush()
    return camera, zone


def _make_person_detection(db_session, camera, ts, track_id="9"):
    # same track_id every call = ByteTrack following one object; dwell and
    # cooldown are keyed on it, a new id each call would be a new object
    det = models.Detection(
        camera_id=camera.id, cls="person", confidence=0.9, bbox=[10, 10, 50, 50],
        source_timestamp=ts, track_id=track_id,
    )
    db_session.add(det)
    db_session.flush()
    return det


def test_loitering_does_not_fire_before_dwell_threshold(monkeypatch, db_session):
    rules_engine._alert_claims.clear()
    rules_engine._zone_presence.clear()
    t = [1000.0]
    monkeypatch.setattr(rules_engine.time, "monotonic", lambda: t[0])

    camera, zone = _make_camera_and_zone(db_session, loitering_seconds=5.0)
    rule = models.AlertRule(name="Loiter", rule_type="loitering", zone_id=zone.id, active=True)
    db_session.add(rule)
    db_session.commit()

    ts = datetime.utcnow()  # inside the default 00:00-23:59 schedule
    det1 = _make_person_detection(db_session, camera, ts)
    alerts1 = asyncio.run(rules_engine.evaluate(db_session, camera, det1, 640, 480))
    assert any("Loitering" in r for r in (alerts1[0].reasons if alerts1 else [])) is False

    t[0] += 3.0  # dwell = 3s, still under the 5s threshold
    det2 = _make_person_detection(db_session, camera, ts)
    alerts2 = asyncio.run(rules_engine.evaluate(db_session, camera, det2, 640, 480))
    assert alerts2 == []  # zone_entry on cooldown, dwell not past threshold yet


def test_loitering_fires_once_past_threshold_then_respects_cooldown(monkeypatch, db_session):
    rules_engine._alert_claims.clear()
    rules_engine._zone_presence.clear()
    t = [2000.0]
    monkeypatch.setattr(rules_engine.time, "monotonic", lambda: t[0])

    camera, zone = _make_camera_and_zone(db_session, loitering_seconds=5.0)
    rule = models.AlertRule(name="Loiter", rule_type="loitering", zone_id=zone.id, active=True)
    db_session.add(rule)
    db_session.commit()

    ts = datetime.utcnow()  # inside the default 00:00-23:59 schedule
    det1 = _make_person_detection(db_session, camera, ts)
    asyncio.run(rules_engine.evaluate(db_session, camera, det1, 640, 480))  # establishes presence, dwell=0

    t[0] += 6.0  # dwell 6s, past the 5s threshold; zone_entry still on its 45s cooldown
    det2 = _make_person_detection(db_session, camera, ts)
    alerts2 = asyncio.run(rules_engine.evaluate(db_session, camera, det2, 640, 480))
    assert len(alerts2) == 1
    assert any("Loitering" in r for r in alerts2[0].reasons)

    t[0] += 1.0  # loitering's own 45s cooldown stops an immediate repeat
    det3 = _make_person_detection(db_session, camera, ts)
    alerts3 = asyncio.run(rules_engine.evaluate(db_session, camera, det3, 640, 480))
    assert alerts3 == []


def test_zone_and_loitering_alerts_suppressed_outside_schedule_window(monkeypatch, db_session):
    rules_engine._alert_claims.clear()
    rules_engine._zone_presence.clear()
    t = [3000.0]
    monkeypatch.setattr(rules_engine.time, "monotonic", lambda: t[0])

    # window 08:00-09:00, detection at 14:00
    camera, zone = _make_camera_and_zone(
        db_session, loitering_seconds=1.0, schedule_start="08:00", schedule_end="09:00",
    )
    rule = models.AlertRule(name="Loiter", rule_type="loitering", zone_id=zone.id, active=True)
    db_session.add(rule)
    db_session.commit()

    outside_window_ts = datetime(2026, 1, 1, 14, 0, 0)
    det = _make_person_detection(db_session, camera, outside_window_ts)
    t[0] += 10.0  # way past the 1s threshold if the schedule gate were missing
    alerts = asyncio.run(rules_engine.evaluate(db_session, camera, det, 640, 480))
    assert alerts == []


def test_loitering_threshold_boundary_is_deterministic(monkeypatch, db_session):
    """threshold X: dwell X-1 does not fire, dwell X fires (the rule is `>=`),
    and X+1 inside the cooldown does not fire again."""
    rules_engine._alert_claims.clear()
    rules_engine._zone_presence.clear()
    t = [5000.0]
    monkeypatch.setattr(rules_engine.time, "monotonic", lambda: t[0])
    camera, zone = _make_camera_and_zone(db_session, loitering_seconds=10.0)
    db_session.add(models.AlertRule(name="Loiter", rule_type="loitering", zone_id=zone.id, active=True))
    db_session.commit()
    ts = datetime.utcnow()

    def loitering_fired() -> bool:
        det = _make_person_detection(db_session, camera, ts, track_id="boundary")
        alerts = asyncio.run(rules_engine.evaluate(db_session, camera, det, 640, 480))
        return any("Loitering" in r for a in alerts for r in a.reasons)

    assert loitering_fired() is False          # dwell 0
    t[0] += 9.0
    assert loitering_fired() is False          # dwell X-1
    t[0] += 1.0
    assert loitering_fired() is True           # dwell X
    t[0] += 1.0
    assert loitering_fired() is False          # dwell X+1, same track, inside cooldown
