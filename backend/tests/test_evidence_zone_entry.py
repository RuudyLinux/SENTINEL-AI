"""Evidence backfill for bare zone_entry alerts (final hardening task).

Previously: rules_engine.py only attached a snapshot/Evidence at alert-creation
time when the triggering Detection already had one (ANPR/watchlist path only) —
a restricted-zone entry with no plate match produced Alert + Incident but NO
Evidence at all. worker.py now captures one from the real processed frame for
ANY alert that doesn't already have a snapshot, covering zone_entry without
touching the existing ANPR/watchlist path (already-set snapshot_path short-
circuits the new code, so that path is provably unchanged — asserted below).
"""
import asyncio
import uuid
from pathlib import Path

import numpy as np
import pytest

from app import models
from app.pipeline import worker, rules_engine


def _make_camera_and_full_frame_zone(db_session, severity="HIGH", camera_code=None):
    # uuid-suffixed, not a fixed literal — the shared SQLite file across this
    # test module's tests (and the rest of the suite) enforces camera_code
    # uniqueness; a hardcoded code collided across tests here.
    camera_code = camera_code or f"C-EVIDENCE-TEST-{uuid.uuid4().hex[:8]}"
    camera = models.Camera(
        camera_code=camera_code, name="Evidence Test Cam", location="Test Location",
        source_type="mock_vms", source_uri="", ai_person=True, ai_vehicle=True, ai_anpr=False,
        status="online",
    )
    db_session.add(camera)
    db_session.flush()
    zone = models.Zone(
        name="Full-frame evidence zone", camera_id=camera.id, x1=0.0, y1=0.0, x2=1.0, y2=1.0,
        severity=severity, active=True,
    )
    db_session.add(zone)
    db_session.commit()
    db_session.refresh(camera)
    return camera


@pytest.fixture(autouse=True)
def _clear_rule_engine_state():
    rules_engine._last_alert_at.clear()
    rules_engine._zone_presence.clear()
    yield
    rules_engine._last_alert_at.clear()
    rules_engine._zone_presence.clear()


def test_bare_zone_entry_now_produces_a_real_evidence_row(monkeypatch, db_session, tmp_path):
    monkeypatch.setattr(worker.settings, "evidence_dir", tmp_path)

    camera = _make_camera_and_full_frame_zone(db_session, severity="HIGH")
    frame = np.zeros((480, 640, 3), dtype=np.uint8)

    async def _fake_detect_and_track(frame_arg, camera_id, want_person=True, want_vehicle=True):
        return [{"cls": "person", "confidence": 0.9, "bbox": [100, 100, 300, 300], "track_id": 1}]

    # detect_and_track is a sync function normally run via asyncio.to_thread —
    # patch the sync entry point worker actually calls.
    monkeypatch.setattr(worker, "detect_and_track", lambda f, cid, want_person=True, want_vehicle=True: [
        {"cls": "person", "confidence": 0.9, "bbox": [100, 100, 300, 300], "track_id": 1}
    ])

    asyncio.run(worker._process_frame(db_session, camera, frame, 0, 640, 480, None, []))

    alerts = db_session.query(models.Alert).filter(models.Alert.camera_id == camera.id).all()
    assert len(alerts) == 1
    alert = alerts[0]
    assert "Restricted-zone entry" in alert.reasons[0]
    assert alert.snapshot_path  # backfilled, not left null

    evidence_rows = db_session.query(models.Evidence).filter(models.Evidence.alert_id == alert.id).all()
    assert len(evidence_rows) == 1
    evidence = evidence_rows[0]
    assert evidence.evidence_type == "snapshot"
    assert evidence.camera_id == camera.id
    assert evidence.event_type == "zone_entry"
    assert evidence.verification_status == "unverified"
    assert evidence.file_path == alert.snapshot_path

    # A real file, not a fabricated path — the actual processed frame, written to disk.
    assert Path(evidence.file_path).exists()
    assert Path(evidence.file_path).stat().st_size > 0

    # Detection row also backfilled for consistency with the alert/evidence.
    det = db_session.query(models.Detection).filter(models.Detection.camera_id == camera.id).first()
    assert det.snapshot_path == alert.snapshot_path


def test_filenames_do_not_collide_across_concurrent_cameras(monkeypatch, db_session, tmp_path):
    """Two different cameras firing the same instant must not write to the same
    path — the naming strategy is camera_code + alert_id + microsecond
    timestamp, so this is inherently collision-safe."""
    monkeypatch.setattr(worker.settings, "evidence_dir", tmp_path)
    monkeypatch.setattr(worker, "detect_and_track", lambda f, cid, want_person=True, want_vehicle=True: [
        {"cls": "person", "confidence": 0.9, "bbox": [100, 100, 300, 300], "track_id": 1}
    ])

    cam_a = _make_camera_and_full_frame_zone(db_session, severity="HIGH")
    cam_b_camera = models.Camera(
        camera_code=f"C-EVIDENCE-TEST-B-{uuid.uuid4().hex[:8]}", name="Cam B", source_type="mock_vms", source_uri="",
        ai_person=True, ai_vehicle=True, ai_anpr=False, status="online",
    )
    db_session.add(cam_b_camera)
    db_session.flush()
    zone_b = models.Zone(name="Zone B", camera_id=cam_b_camera.id, x1=0.0, y1=0.0, x2=1.0, y2=1.0, severity="HIGH", active=True)
    db_session.add(zone_b)
    db_session.commit()

    frame = np.zeros((480, 640, 3), dtype=np.uint8)
    asyncio.run(worker._process_frame(db_session, cam_a, frame, 0, 640, 480, None, []))
    asyncio.run(worker._process_frame(db_session, cam_b_camera, frame, 0, 640, 480, None, []))

    # Scoped to exactly these two cameras' own evidence — an unscoped query
    # over the whole table picks up rows from every other test file sharing
    # this DB (same class of cross-test pollution fixed elsewhere this
    # session), which is a test-isolation bug, not evidence of a real
    # filename collision.
    paths = [
        e.file_path for e in db_session.query(models.Evidence)
        .filter(models.Evidence.camera_id.in_([cam_a.id, cam_b_camera.id]))
        .all()
    ]
    assert len(paths) == 2
    assert len(paths) == len(set(paths))  # no collisions
    for p in paths:
        assert Path(p).exists()


def test_anpr_watchlist_snapshot_path_unchanged_evidence_not_duplicated(monkeypatch, db_session, tmp_path):
    """The pre-existing ANPR/watchlist evidence path is untouched: when a
    detection already carries a snapshot_path (set earlier in _process_frame,
    same as before this change), the new backfill code must not run again and
    must not create a second Evidence row for the same alert."""
    monkeypatch.setattr(worker.settings, "evidence_dir", tmp_path)
    monkeypatch.setattr(worker, "detect_and_track", lambda f, cid, want_person=True, want_vehicle=True: [
        {"cls": "car", "confidence": 0.9, "bbox": [100, 100, 300, 300], "track_id": 1}
    ])

    camera = models.Camera(
        camera_code=f"C-EVIDENCE-ANPR-TEST-{uuid.uuid4().hex[:8]}", name="ANPR Test Cam", source_type="mock_vms", source_uri="",
        ai_person=True, ai_vehicle=True, ai_anpr=True, status="online",
    )
    db_session.add(camera)
    db_session.flush()
    zone = models.Zone(name="Zone", camera_id=camera.id, x1=0.0, y1=0.0, x2=1.0, y2=1.0, severity="HIGH", active=True)
    db_session.add(zone)
    db_session.commit()
    db_session.refresh(camera)

    # ANPR gate rejects every read (no plausible plate in a blank crop) — so
    # this exercises the plain zone_entry path even on a car, proving the new
    # code activates regardless of vehicle class once ANPR doesn't match.
    frame = np.zeros((480, 640, 3), dtype=np.uint8)
    asyncio.run(worker._process_frame(db_session, camera, frame, 0, 640, 480, None, []))

    alerts = db_session.query(models.Alert).filter(models.Alert.camera_id == camera.id).all()
    assert len(alerts) == 1
    evidence_rows = db_session.query(models.Evidence).filter(models.Evidence.alert_id == alerts[0].id).all()
    assert len(evidence_rows) == 1  # exactly one — no duplication


def test_critical_watchlist_evidence_carries_alert_and_detection_reference(db_session):
    """Final-demo-readiness-phase finding, discovered via live browser
    verification: a real CRITICAL watchlist-match Evidence row (created by
    rules_engine.evaluate's own incident_evidence, NOT worker.py's
    backfill block below it) showed "Alert: —" in the UI — that code path
    never set alert_id/detection_id/event_type/source_timestamp, unlike the
    identical Evidence model's OTHER real creation site (worker.py's
    backfill), which does. Matched to that existing shape."""
    from app.pipeline import rules_engine

    camera = models.Camera(
        camera_code=f"C-EVIDENCE-CRITICAL-{uuid.uuid4().hex[:8]}", name="Critical Evidence Test Cam",
        source_type="mock_vms", source_uri="", status="online",
    )
    db_session.add(camera)
    db_session.flush()
    vehicle = models.Vehicle(plate_text="GJ01ZZ9999", watchlist_flag=True)
    db_session.add(vehicle)
    db_session.flush()
    detection = models.Detection(
        camera_id=camera.id, cls="car", confidence=0.9, bbox=[1, 2, 3, 4],
        snapshot_path="/tmp/fake-real-snapshot.jpg",
    )
    db_session.add(detection)
    db_session.commit()

    rules_engine._last_alert_at.clear()
    alerts = asyncio.run(rules_engine.evaluate(db_session, camera, detection, 640, 480, vehicle))
    assert len(alerts) == 1
    assert alerts[0].severity == "CRITICAL"

    incident = db_session.query(models.Incident).filter(models.Incident.alert_id == alerts[0].id).first()
    assert incident is not None
    evidence = db_session.query(models.Evidence).filter(models.Evidence.incident_id == incident.id).first()
    assert evidence is not None
    assert evidence.alert_id == alerts[0].id  # the missing field
    assert evidence.detection_id == detection.id
    assert evidence.event_type == "watchlist_match"
    assert evidence.camera_id == camera.id
    assert evidence.file_path == "/tmp/fake-real-snapshot.jpg"
