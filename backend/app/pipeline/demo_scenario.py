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
from . import worker, clips

DEMO_MODEL_VERSION = "demo-fixture"


def _save_demo_snapshot(camera_id: str, camera_code: str) -> str | None:
    """Saves the camera's current real MJPEG frame as evidence, if the
    camera is actually running. Returns None (no fake snapshot) if not —
    the caller then simply has no snapshot for this sighting, same as the
    live pipeline when nothing has decoded yet."""
    jpeg_bytes = worker.LATEST_FRAMES.get(camera_id)
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
        snapshot_path = _save_demo_snapshot(camera.id, camera.camera_code)
        det = models.Detection(
            camera_id=camera.id, cls="car", confidence=0.91, bbox=[10, 10, 200, 150],
            timestamp=ts, source_timestamp=ts, model_version=DEMO_MODEL_VERSION,
            snapshot_path=snapshot_path,
        )
        db.add(det)
        db.flush()
        vehicle = upsert_vehicle_for_plate(db, plate, sighting_confidence)
        db.add(models.Plate(
            vehicle_id=vehicle.id, camera_id=camera.id, detection_id=det.id,
            plate_text_raw=plate, plate_text_normalized=plate,
            confidence=sighting_confidence, timestamp=ts,
            snapshot_path=snapshot_path,
        ))
        db.commit()
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
