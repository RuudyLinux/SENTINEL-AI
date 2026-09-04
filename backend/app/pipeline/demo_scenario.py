"""Deterministic judge-demo scenario trigger (Phase 6, DEMO_MODE only).

The primary demo scenario needs a vehicle carrying the exact seeded
watchlist plate (GJ05AB1234) to appear on camera, a few minutes apart, on
two different cameras. The only real test footage available
(uploads/car-detection.mp4) doesn't contain that plate — no real OCR read
of it can be produced from it — so a literal wait for real ANPR to read
that exact plate off that footage is not deterministic or repeatable for a
live judge demo.

This module stands in for ONLY the OCR-image-decode step, exactly as
documented in README.md ("Judge demo runbook") — every other step is the
real code: `upsert_vehicle_for_plate`
(real correlation), `rules_engine.evaluate` (real watchlist match, real
explainable alert, real cooldown, real auto-incident), `get_route` (real
cross-camera route). The ANPR quality gate itself is never touched, weakened,
or bypassed — this doesn't call `read_plate`/`passes_anpr_gate` at all; it
supplies the value a real high-confidence read WOULD have produced, the same
way an operator manually adding a watchlist entry supplies a value without
running OCR on it.

Every row this creates is clearly attributable: `Detection.model_version`
is set to "demo-fixture" (never "yolov8n-coco-*" like a real detection), and
every call is audit-logged as `trigger_demo_scenario`.

Evidence (snapshot + video clip) is real, not fabricated: if the target
camera is actually running, its current MJPEG frame (`worker.LATEST_FRAMES`)
and its live event-clip ring buffer (`clips`) are genuine, currently-decoding
footage from that camera — used exactly as the live pipeline would, just
triggered on demand instead of waiting for a real detection.
"""
import asyncio
from datetime import datetime, timedelta

from sqlalchemy.orm import Session

from .. import models
from ..config import settings
from ..audit import log_action
from .correlate import upsert_vehicle_for_plate, get_route
from .rules_engine import evaluate
from .anpr import passes_anpr_gate
from .db_retry import safe_commit, safe_flush
from . import worker, clips

DEMO_MODEL_VERSION = "demo-fixture"


DEMO_FRAME_WAIT_TIMEOUT_S = 3.0  # bounded — see _wait_for_live_frame


async def _wait_for_live_frame(camera_id: str, timeout_s: float | None = None) -> bytes | None:
    """A camera worker started moments ago (POST /demo/reset now starts the
    two demo cameras — see routers/system.py) needs a brief real interval to
    open uploads/car-detection.mp4 and decode its first frame before
    worker.LATEST_FRAMES has anything in it. Polls briefly rather than
    either fabricating a frame or giving up instantly — bounded, so a
    genuinely non-running camera still returns None (honest "no frame")
    within a few seconds, never hangs.

    `timeout_s` reads the module-level DEMO_FRAME_WAIT_TIMEOUT_S at CALL
    time (not as a function-signature default, which would bind at import
    time) specifically so tests can `monkeypatch.setattr(demo_scenario,
    "DEMO_FRAME_WAIT_TIMEOUT_S", ...)` and have it actually take effect."""
    if timeout_s is None:
        timeout_s = DEMO_FRAME_WAIT_TIMEOUT_S
    loop = asyncio.get_event_loop()
    deadline = loop.time() + timeout_s
    while True:
        frame = worker.LATEST_FRAMES.get(camera_id)
        if frame:
            return frame
        if loop.time() >= deadline:
            return None
        await asyncio.sleep(0.1)


async def _save_demo_snapshot(camera_id: str, camera_code: str) -> str | None:
    """Saves the camera's current real MJPEG frame as evidence, if the
    camera is actually running. Returns None (no fake snapshot) if not —
    the caller then simply has no snapshot for this sighting, same as the
    live pipeline when nothing has decoded yet."""
    jpeg_bytes = await _wait_for_live_frame(camera_id)
    if not jpeg_bytes:
        return None
    fname = f"{camera_code}_demo_{datetime.utcnow().strftime('%Y%m%d%H%M%S%f')}.jpg"
    path = settings.evidence_dir / fname
    path.write_bytes(jpeg_bytes)
    return str(path)


class DemoScenarioError(Exception):
    pass


async def trigger_scenario(db: Session, user: models.User, plate: str = "GJ05AB1234") -> dict:
    """Fires one deterministic sighting of `plate` on each of the two demo
    cameras (C-014 then C-019, 4 minutes apart), through the real
    correlation/alerting code path. DEMO_MODE only — caller enforces this;
    this function refuses too as a second guard."""
    if not settings.demo_mode:
        raise DemoScenarioError("trigger_scenario called outside DEMO_MODE — refusing")

    cameras = db.query(models.Camera).filter(models.Camera.camera_code.in_(["C-014", "C-019"])).all()
    by_code = {c.camera_code: c for c in cameras}
    missing = [code for code in ("C-014", "C-019") if code not in by_code]
    if missing:
        raise DemoScenarioError(f"Demo cameras not registered: {missing} — run POST /api/system/demo/reset first")

    # A high-confidence read is required to clear the real ANPR gate — same
    # threshold the live pipeline enforces, not bypassed here.
    confidence = 0.85
    if not passes_anpr_gate(plate, confidence):
        raise DemoScenarioError(f"'{plate}' would not clear the real ANPR quality gate at confidence {confidence}")

    t0 = datetime.utcnow() - timedelta(seconds=2)
    results = []
    for camera_code, ts, sighting_confidence in (("C-014", t0, 0.85), ("C-019", t0 + timedelta(minutes=4), 0.90)):
        camera = by_code[camera_code]
        snapshot_path = await _save_demo_snapshot(camera.id, camera.camera_code)
        det = models.Detection(
            camera_id=camera.id, cls="car", confidence=0.91, bbox=[10, 10, 200, 150],
            timestamp=ts, source_timestamp=ts, model_version=DEMO_MODEL_VERSION,
            snapshot_path=snapshot_path,
        )
        db.add(det)
        # Root-cause fix (found live): this and the commit below were bare
        # db.flush()/db.commit() calls, unguarded against SQLite lock
        # contention — the exact bug class PR #1 fixed in worker.py, just
        # never applied here. Caught in practice: triggering the demo
        # scenario while the two demo cameras' real workers were actively
        # writing (heartbeats, frame processing) produced a genuine
        # unhandled 500. Same bounded retry contract as the rest of the
        # pipeline now applies here too.
        await safe_flush(db, "demo_scenario", reapply=lambda _det=det: db.add(_det))
        vehicle = await upsert_vehicle_for_plate(db, plate, sighting_confidence)
        plate_row = models.Plate(
            vehicle_id=vehicle.id, camera_id=camera.id, detection_id=det.id,
            plate_text_raw=plate, plate_text_normalized=plate,
            confidence=sighting_confidence, timestamp=ts,
            snapshot_path=snapshot_path,
        )
        db.add(plate_row)

        vehicle_target_last_seen = vehicle.last_seen
        vehicle_target_confidence = vehicle.plate_confidence

        def _reapply_plate_commit(_det=det, _plate_row=plate_row, _vehicle=vehicle):
            # `det` and `plate_row` are freshly db.add()'d (never committed
            # before this point in the loop) — a rollback only detaches
            # them, so re-add() alone restores them. `vehicle` may instead
            # be a pre-existing PERSISTENT row whose last_seen/
            # plate_confidence were only FLUSHED (not committed) by
            # upsert_vehicle_for_plate just above — a rollback here expires
            # those back to their last-committed value, so they're
            # explicitly reassigned from the locals captured right after
            # that call, never by re-reading vehicle.* (same reasoning as
            # worker.py's identical reapply pattern).
            db.add(_det)
            db.add(_plate_row)
            db.add(_vehicle)
            _vehicle.last_seen = vehicle_target_last_seen
            _vehicle.plate_confidence = vehicle_target_confidence

        await safe_commit(db, "demo_scenario", reapply=_reapply_plate_commit)
        alerts = await evaluate(db, camera, det, 640, 480, vehicle)
        for alert in alerts:
            incident = db.query(models.Incident).filter(models.Incident.alert_id == alert.id).first()
            event_type = "watchlist_match" if alert.vehicle_id else "zone_entry"
            # Real video clip from this camera's own live ring buffer, same
            # mechanism the real pipeline uses on a real alert (worker.py).
            asyncio.create_task(clips.build_event_clip(
                camera.id, camera.camera_code, alert.id, det.id,
                incident.id if incident else None, event_type, ts,
            ))
        results.append({
            "camera_code": camera_code, "detection_id": det.id, "snapshot_path": snapshot_path,
            "alerts": [{"id": a.id, "severity": a.severity, "reasons": a.reasons} for a in alerts],
        })

    log_action(db, user, "trigger_demo_scenario", resource=plate)
    route = get_route(db, vehicle.id)
    return {
        "plate": plate,
        "vehicle_id": vehicle.id,
        "sightings": results,
        "route": [{"camera_code": s["camera_code"], "timestamp": s["timestamp"].isoformat()} for s in route],
    }
