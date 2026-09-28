"""Evidence for plain zone_entry alerts.

rules_engine only attached a snapshot when the detection already had one
(ANPR/watchlist), so a zone entry without a plate got an Alert and Incident
but no Evidence. worker.py now saves one from the frame for any alert
without a snapshot. An existing snapshot_path skips it, so the ANPR path is
unchanged (checked below).
"""
import asyncio
import uuid
from pathlib import Path

import numpy as np
import pytest

from app import models
from app.pipeline import worker, rules_engine


@pytest.fixture(autouse=True)
def _one_frame_zone_entry(monkeypatch):
    # these tests fire a zone alert from one tracked frame; the multi-frame
    # confirmation has its own tests in test_zone_alert_accuracy.py
    from app.config import settings as _settings
    monkeypatch.setattr(_settings, "zone_entry_min_frames", 1)


def _make_camera_and_full_frame_zone(db_session, severity="HIGH", camera_code=None):
    # uuid suffix, camera_code is unique across the shared test DB
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
    rules_engine._alert_claims.clear()
    rules_engine._zone_presence.clear()
    yield
    rules_engine._alert_claims.clear()
    rules_engine._zone_presence.clear()


def test_bare_zone_entry_now_produces_a_real_evidence_row(monkeypatch, db_session, tmp_path):
    monkeypatch.setattr(worker.settings, "evidence_dir", tmp_path)

    camera = _make_camera_and_full_frame_zone(db_session, severity="HIGH")
    frame = np.zeros((480, 640, 3), dtype=np.uint8)

    async def _fake_detect_and_track(frame_arg, camera_id, want_person=True, want_vehicle=True):
        return [{"cls": "person", "confidence": 0.9, "bbox": [100, 100, 300, 300], "track_id": 1}]

    # patch the sync function the worker runs via to_thread
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

    # a real file, the processed frame written to disk
    assert Path(evidence.file_path).exists()
    assert Path(evidence.file_path).stat().st_size > 0

    # Detection row also backfilled for consistency with the alert/evidence.
    det = db_session.query(models.Detection).filter(models.Detection.camera_id == camera.id).first()
    assert det.snapshot_path == alert.snapshot_path


def test_filenames_do_not_collide_across_concurrent_cameras(monkeypatch, db_session, tmp_path):
    """Two cameras firing at the same instant get different paths
    (camera_code + alert_id + microsecond timestamp)."""
    monkeypatch.setattr(worker.settings, "evidence_dir", tmp_path)
    monkeypatch.setattr(worker.settings, "max_ai_cameras", 2)  # both cameras run AI
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

    # only these two cameras' evidence; the whole table has other tests' rows
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
    """With snapshot_path already set by the ANPR path, the backfill doesn't
    run and there's no second Evidence row."""
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

    # the gate rejects every read (blank crop), so a car takes the plain
    # zone_entry path too
    frame = np.zeros((480, 640, 3), dtype=np.uint8)
    asyncio.run(worker._process_frame(db_session, camera, frame, 0, 640, 480, None, []))

    alerts = db_session.query(models.Alert).filter(models.Alert.camera_id == camera.id).all()
    assert len(alerts) == 1
    evidence_rows = db_session.query(models.Evidence).filter(models.Evidence.alert_id == alerts[0].id).all()
    assert len(evidence_rows) == 1  # no duplicate


def test_critical_watchlist_evidence_carries_alert_and_detection_reference(db_session):
    """rules_engine's own incident Evidence row didn't set alert_id/
    detection_id/event_type/source_timestamp (worker.py's backfill does), so
    the UI showed "Alert: —" on a real CRITICAL watchlist match."""
    from app.pipeline import rules_engine

    camera = models.Camera(
        camera_code=f"C-EVIDENCE-CRITICAL-{uuid.uuid4().hex[:8]}", name="Critical Evidence Test Cam",
        source_type="mock_vms", source_uri="", status="online",
    )
    db_session.add(camera)
    db_session.flush()
    # plate_confidence above the watchlist floor, this is about linkage not
    # confidence gating (test_watchlist_confidence_gating.py). And a real
    # WatchlistEntry, not just the flag: the flag is only ever set from an
    # entry, and the rule now needs the entry so its reason string is true.
    db_session.add(models.WatchlistEntry(
        entity_type="plate", identifier="GJ01ZZ9999", priority="CRITICAL", active=True,
        reason="evidence linkage test",
    ))
    vehicle = models.Vehicle(
        plate_text="GJ01ZZ9999", plate_confidence=0.9, watchlist_flag=True,
        plate_corroborated=True,  # CRITICAL now needs corroboration as well as confidence
    )
    db_session.add(vehicle)
    db_session.flush()
    detection = models.Detection(
        camera_id=camera.id, cls="car", confidence=0.9, bbox=[1, 2, 3, 4],
        snapshot_path="/tmp/fake-real-snapshot.jpg",
    )
    db_session.add(detection)
    db_session.commit()

    rules_engine._alert_claims.clear()
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
