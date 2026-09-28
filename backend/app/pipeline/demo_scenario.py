"""Deterministic demo scenario trigger, DEMO_MODE only.

The main demo needs the seeded watchlist plate GJ05AB1234 to show up on two
cameras a few minutes apart. The only real footage we have
(app/demo_assets/car-detection.mp4) doesn't contain that plate, so waiting for
real ANPR to read it isn't something a live demo can rely on.

This replaces only the OCR read, as the README runbook says. Everything else
is the real code: upsert_vehicle_for_plate, rules_engine.evaluate (watchlist
match, alert, cooldown, auto-incident) and get_route. The ANPR gate isn't
touched; read_plate/passes_anpr_gate aren't called, we just supply the value
a confident read would have, like an operator typing in a watchlist entry.

Rows are easy to tell apart: Detection.model_version is "demo-fixture" and
every call is audit-logged as trigger_demo_scenario.

Snapshot and clip are real when the camera is running: its current MJPEG
frame (worker.LATEST_FRAMES) and its live clip ring buffer, triggered on
demand instead of by a detection.
"""
import asyncio
from datetime import datetime, timedelta

from sqlalchemy.orm import Session

from .. import background, models
from ..config import settings
from ..audit import log_action
from .correlate import upsert_vehicle_for_plate, get_route
from .rules_engine import evaluate
from .anpr import passes_anpr_gate
from .db_retry import safe_commit, safe_flush
from . import worker, clips

DEMO_MODEL_VERSION = "demo-fixture"


DEMO_FRAME_WAIT_TIMEOUT_S = 3.0  # see _wait_for_live_frame


async def _wait_for_live_frame(camera_id: str, timeout_s: float | None = None) -> bytes | None:
    """A worker started moments ago (POST /demo/reset starts both demo
    cameras, routers/system.py) needs a moment to open the video and decode a
    frame before LATEST_FRAMES has anything. Poll briefly; a camera that
    isn't running still gets None within a few seconds.

    timeout_s reads DEMO_FRAME_WAIT_TIMEOUT_S at call time, not as a default
    argument (bound at import), so tests can monkeypatch it."""
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
    """Save the camera's current MJPEG frame as evidence if it's running.
    None otherwise, same as the live pipeline before anything decodes."""
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
    """One sighting of `plate` on each demo camera (C-014, then C-019 four
    minutes later) through the real correlation/alerting path. DEMO_MODE
    only; the caller checks and so does this."""
    if not settings.demo_mode:
        raise DemoScenarioError("trigger_scenario called outside DEMO_MODE — refusing")

    cameras = db.query(models.Camera).filter(models.Camera.camera_code.in_(["C-014", "C-019"])).all()
    by_code = {c.camera_code: c for c in cameras}
    missing = [code for code in ("C-014", "C-019") if code not in by_code]
    if missing:
        raise DemoScenarioError(f"Demo cameras not registered: {missing} — run POST /api/system/demo/reset first")
    retired = [code for code, c in by_code.items() if c.retired]
    if retired:
        raise DemoScenarioError(f"Demo cameras are retired: {sorted(retired)} — reinstate them to run the scenario")

    # confident enough to clear the real ANPR gate, same threshold as live
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
        # retried like the rest of the pipeline; bare flush/commit here gave
        # a 500 when the demo cameras' workers were writing at the same time
        await safe_flush(db, "demo_scenario", reapply=lambda _det=det: db.add(_det))
        # corroborated=True: this models a car read confidently and repeatedly
        # across cameras, which is what legitimately reaches CRITICAL. False
        # would demo a single-frame read the pipeline caps at HIGH.
        vehicle = await upsert_vehicle_for_plate(db, plate, sighting_confidence, corroborated=True)
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
            # det and plate_row are new, so rollback just detaches them and
            # re-add() restores them. vehicle may be an existing row whose
            # last_seen/plate_confidence were only flushed above; rollback
            # expires those, so reassign from the locals (same as worker.py)
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
            # real clip from this camera's ring buffer, like worker.py on an alert
            background.spawn(
                clips.build_event_clip(
                    camera.id, camera.camera_code, alert.id, det.id,
                    incident.id if incident else None, event_type, ts,
                ),
                name=f"clip:{camera.camera_code}:{alert.id}",
            )
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
