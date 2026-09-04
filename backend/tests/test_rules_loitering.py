"""Rule engine (Phase 6): loitering (dwell-time) rules and the schedule-window
gate — both previously dead capability (Zone.schedule_start/end existed on the
model/schema but rules_engine.evaluate() never read them; loitering didn't exist
at all). Mirrors test_alert_dedup.py's monotonic-clock monkeypatch style."""
import asyncio
import uuid
from datetime import datetime

from app.pipeline import rules_engine
from app import models


def _make_camera_and_zone(db_session, **zone_kwargs):
    # uuid4, not id(zone_kwargs) — CPython recycles small short-lived objects'
    # ids, which produced real cross-test camera_code collisions (UNIQUE
    # constraint failures) against the suite's shared SQLite file.
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
    # Same track_id across calls simulates ByteTrack following the same physical
    # object across consecutive frames — dwell-time/cooldown keys are keyed on
    # track_id (see rules_engine.evaluate's track_key), so a fresh track_id per
    # call would make every "frame" look like a brand-new, unrelated object.
    det = models.Detection(
        camera_id=camera.id, cls="person", confidence=0.9, bbox=[10, 10, 50, 50],
        source_timestamp=ts, track_id=track_id,
    )
    db_session.add(det)
    db_session.flush()
    return det


def test_loitering_does_not_fire_before_dwell_threshold(monkeypatch, db_session):
    rules_engine._last_alert_at.clear()
    rules_engine._zone_presence.clear()
    t = [1000.0]
    monkeypatch.setattr(rules_engine.time, "monotonic", lambda: t[0])

    camera, zone = _make_camera_and_zone(db_session, loitering_seconds=5.0)
    rule = models.AlertRule(name="Loiter", rule_type="loitering", zone_id=zone.id, active=True)
    db_session.add(rule)
    db_session.commit()

    ts = datetime.utcnow()  # naive "now" — inside default 00:00-23:59 schedule
    det1 = _make_person_detection(db_session, camera, ts)
    alerts1 = asyncio.run(rules_engine.evaluate(db_session, camera, det1, 640, 480))
    assert any("Loitering" in r for r in (alerts1[0].reasons if alerts1 else [])) is False

    t[0] += 3.0  # dwell = 3s, still under the 5s threshold
    det2 = _make_person_detection(db_session, camera, ts)
    alerts2 = asyncio.run(rules_engine.evaluate(db_session, camera, det2, 640, 480))
    assert alerts2 == []  # zone_entry on cooldown, dwell not yet past threshold — nothing fires


def test_loitering_fires_once_past_threshold_then_respects_cooldown(monkeypatch, db_session):
    rules_engine._last_alert_at.clear()
    rules_engine._zone_presence.clear()
    t = [2000.0]
    monkeypatch.setattr(rules_engine.time, "monotonic", lambda: t[0])

    camera, zone = _make_camera_and_zone(db_session, loitering_seconds=5.0)
    rule = models.AlertRule(name="Loiter", rule_type="loitering", zone_id=zone.id, active=True)
    db_session.add(rule)
    db_session.commit()

    ts = datetime.utcnow()  # naive "now" — inside default 00:00-23:59 schedule
    det1 = _make_person_detection(db_session, camera, ts)
    asyncio.run(rules_engine.evaluate(db_session, camera, det1, 640, 480))  # establishes presence, dwell=0

    t[0] += 6.0  # dwell = 6s, past the 5s threshold; zone_entry itself is still on its own 45s cooldown
    det2 = _make_person_detection(db_session, camera, ts)
    alerts2 = asyncio.run(rules_engine.evaluate(db_session, camera, det2, 640, 480))
    assert len(alerts2) == 1
    assert any("Loitering" in r for r in alerts2[0].reasons)

    t[0] += 1.0  # 1s later — loitering's own cooldown (45s) suppresses an immediate repeat
    det3 = _make_person_detection(db_session, camera, ts)
    alerts3 = asyncio.run(rules_engine.evaluate(db_session, camera, det3, 640, 480))
    assert alerts3 == []


def test_zone_and_loitering_alerts_suppressed_outside_schedule_window(monkeypatch, db_session):
    rules_engine._last_alert_at.clear()
    rules_engine._zone_presence.clear()
    t = [3000.0]
    monkeypatch.setattr(rules_engine.time, "monotonic", lambda: t[0])

    # Schedule window is 08:00-09:00; the detection's source_timestamp is 14:00 — outside it.
    camera, zone = _make_camera_and_zone(
        db_session, loitering_seconds=1.0, schedule_start="08:00", schedule_end="09:00",
    )
    rule = models.AlertRule(name="Loiter", rule_type="loitering", zone_id=zone.id, active=True)
    db_session.add(rule)
    db_session.commit()

    outside_window_ts = datetime(2026, 1, 1, 14, 0, 0)
    det = _make_person_detection(db_session, camera, outside_window_ts)
    t[0] += 10.0  # well past the 1s loitering threshold if the schedule gate were absent
    alerts = asyncio.run(rules_engine.evaluate(db_session, camera, det, 640, 480))
    assert alerts == []
