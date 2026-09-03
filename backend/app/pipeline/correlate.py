"""Cross-camera correlation: same normalized plate text seen on any camera is
the same Vehicle. This is the real mechanism behind the doc's
'C-014 -> C-019 -> C-027' cross-camera route (§18, §60) — driven by actual
OCR reads persisted in the Plate/Vehicle tables, not scripted data.
"""
from datetime import datetime
from sqlalchemy.orm import Session

from .. import models


def upsert_vehicle_for_plate(db: Session, normalized_plate: str, confidence: float) -> models.Vehicle:
    vehicle = db.query(models.Vehicle).filter(models.Vehicle.plate_text == normalized_plate).first()
    now = datetime.utcnow()
    if vehicle:
        vehicle.last_seen = now
        if confidence > vehicle.plate_confidence:
            vehicle.plate_confidence = confidence
    else:
        watchlisted = db.query(models.WatchlistEntry).filter(
            models.WatchlistEntry.entity_type == "plate",
            models.WatchlistEntry.identifier == normalized_plate,
            models.WatchlistEntry.active == True,  # noqa: E712
        ).first()
        vehicle = models.Vehicle(
            plate_text=normalized_plate,
            plate_confidence=confidence,
            first_seen=now,
            last_seen=now,
            watchlist_flag=bool(watchlisted),
        )
        db.add(vehicle)
    db.flush()
    return vehicle


def get_route(db: Session, vehicle_id: str):
    """Ordered cross-camera sightings for a vehicle, built from real Plate rows."""
    plates = (
        db.query(models.Plate)
        .filter(models.Plate.vehicle_id == vehicle_id)
        .order_by(models.Plate.timestamp.asc())
        .all()
    )
    sightings = []
    for p in plates:
        cam = db.query(models.Camera).filter(models.Camera.id == p.camera_id).first()
        if not cam:
            continue
        sightings.append({
            "camera_id": cam.id,
            "camera_code": cam.camera_code,
            "camera_name": cam.name,
            "timestamp": p.timestamp,
            "confidence": p.confidence,
            "snapshot_path": p.snapshot_path,
        })
    return sightings
