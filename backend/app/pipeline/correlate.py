"""Cross-camera correlation: same normalized plate text seen on any camera is
the same Vehicle. This is the real mechanism behind the doc's
'C-014 -> C-019 -> C-027' cross-camera route (§18, §60) — driven by actual
OCR reads persisted in the Plate/Vehicle tables, not scripted data.

Also: cross-camera PERSON correlation by appearance-similarity signature (Phase
5) — see `find_similar_person_detections` and pipeline/appearance.py. Explicitly
not face recognition or identity resolution: a ranked visual-similarity candidate
list only, for an investigator to review manually.
"""
from datetime import datetime
from sqlalchemy.orm import Session

from .. import models
from .appearance import similarity
from .db_retry import safe_flush


async def upsert_vehicle_for_plate(db: Session, normalized_plate: str, confidence: float) -> models.Vehicle:
    """Final-demo-readiness-phase finding: this function's own `db.flush()`
    was unguarded against SQLite lock contention — the same root-cause class
    PR #1 fixed in worker.py's detection insert, just in a different, shared
    call site (both worker.py's real live pipeline AND demo_scenario.py call
    this). Caught live: a real concurrent-camera-write lock here surfaced as
    an unhandled 500 from the demo-scenario endpoint. Now retried with the
    same bounded rollback -> reapply -> backoff contract as every other
    write in the pipeline (db_retry.safe_flush)."""
    vehicle = db.query(models.Vehicle).filter(models.Vehicle.plate_text == normalized_plate).first()
    now = datetime.utcnow()
    if vehicle:
        target_last_seen = now
        target_confidence = max(confidence, vehicle.plate_confidence)
        vehicle.last_seen = target_last_seen
        vehicle.plate_confidence = target_confidence

        def reapply():
            # `vehicle` is already PERSISTENT here — a rollback expires its
            # mutated attributes back to their last-committed value, so
            # reapply must reassign from these captured locals, never from
            # re-reading vehicle.* (same reasoning as db_retry.py's module
            # docstring / worker.py's own reapply callbacks).
            db.add(vehicle)
            vehicle.last_seen = target_last_seen
            vehicle.plate_confidence = target_confidence
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

        def reapply():
            # `vehicle` is still TRANSIENT (never committed) — rollback only
            # detaches it; its already-set attributes (including the
            # client-generated PK) survive, so re-add() alone restores it.
            db.add(vehicle)

    await safe_flush(db, "upsert_vehicle_for_plate", reapply=reapply)
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


def find_similar_person_detections(
    db: Session,
    reference_detection_id: str,
    min_similarity: float = 0.6,
    exclude_camera_id: str | None = None,
    after: datetime | None = None,
    before: datetime | None = None,
    limit: int = 50,
) -> list[dict]:
    """Ranked candidate list of other person Detection rows whose stored
    appearance_signature is visually similar to the reference detection's — NOT an
    identity match, a lead-generation ranking only (see pipeline/appearance.py).
    Detections without a stored signature (never computed, or the crop was too
    small) are skipped, never guessed. Returns [] if the reference detection
    itself has no signature to compare against."""
    reference = db.query(models.Detection).filter(models.Detection.id == reference_detection_id).first()
    if reference is None or reference.cls != "person" or not reference.appearance_signature:
        return []

    q = db.query(models.Detection).filter(
        models.Detection.cls == "person",
        models.Detection.id != reference_detection_id,
        models.Detection.appearance_signature.isnot(None),
    )
    if exclude_camera_id:
        q = q.filter(models.Detection.camera_id != exclude_camera_id)
    if after:
        q = q.filter(models.Detection.timestamp >= after)
    if before:
        q = q.filter(models.Detection.timestamp <= before)

    candidates = q.order_by(models.Detection.timestamp.desc()).limit(1000).all()

    ranked = []
    for cand in candidates:
        score = similarity(reference.appearance_signature, cand.appearance_signature)
        if score < min_similarity:
            continue
        cam = db.query(models.Camera).filter(models.Camera.id == cand.camera_id).first()
        ranked.append({
            "detection_id": cand.id,
            "camera_id": cand.camera_id,
            "camera_code": cam.camera_code if cam else "",
            "camera_name": cam.name if cam else "",
            "timestamp": cand.timestamp,
            "similarity": round(score, 4),
            "snapshot_path": cand.snapshot_path,
        })
    ranked.sort(key=lambda r: r["similarity"], reverse=True)
    return ranked[:limit]
