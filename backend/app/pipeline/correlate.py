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
from . import risk
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


async def upsert_track(
    db: Session, camera_id: str, yolo_track_id: int, cls: str, at: datetime,
    vehicle_id: str | None = None, plate_reads: int = 0,
) -> models.Track:
    """Get-or-create the Track row for one ByteTrack id on one camera.

    Track ids are only unique per predictor instance, and detector.py keeps one
    instance per camera, so (camera_id, yolo_track_id) is the real identity —
    never yolo_track_id alone. A long-running camera eventually recycles ids;
    that is accepted here (the row's last_seen moves forward) because the
    alternative — a new Track row per id reuse — would fragment a vehicle's
    history for no operational gain.
    """
    track = (
        db.query(models.Track)
        .filter(models.Track.camera_id == camera_id, models.Track.yolo_track_id == yolo_track_id)
        .first()
    )
    if track is not None:
        target_last_seen = at
        target_count = (track.detection_count or 0) + 1
        # Identity only ever gets ADDED to a track, never cleared: a frame in
        # which the plate happened not to read must not un-identify a vehicle
        # we already recognized on this same track.
        target_vehicle_id = vehicle_id or track.vehicle_id
        target_plate_reads = max(plate_reads, track.plate_reads or 0)
        track.last_seen = target_last_seen
        track.detection_count = target_count
        track.vehicle_id = target_vehicle_id
        track.plate_reads = target_plate_reads

        def reapply():
            # Persistent row: a rollback expires these mutations back to their
            # last-committed values, so reapply must reassign from the captured
            # locals rather than re-reading track.* (see db_retry.py).
            db.add(track)
            track.last_seen = target_last_seen
            track.detection_count = target_count
            track.vehicle_id = target_vehicle_id
            track.plate_reads = target_plate_reads
    else:
        track = models.Track(
            camera_id=camera_id, cls=cls, yolo_track_id=yolo_track_id,
            first_seen=at, last_seen=at, detection_count=1,
            plate_reads=plate_reads, vehicle_id=vehicle_id,
        )
        db.add(track)

        def reapply():
            # Transient row: rollback only detaches it, its attributes
            # (including the client-generated PK) survive, so re-add restores it.
            db.add(track)

    await safe_flush(db, "upsert_track", reapply=reapply)
    return track


async def upsert_plate_sighting(
    db: Session,
    *,
    vehicle: models.Vehicle,
    camera_id: str,
    track_id: str | None,
    detection_id: str,
    raw_text: str,
    normalized_text: str,
    confidence: float,
    reads_count: int,
    vehicle_class: str,
    detection_confidence: float,
    vehicle_bbox: list[float] | None,
    plate_bbox: list[float] | None,
    snapshot_path: str | None,
    source_timestamp: datetime | None,
    existing_plate_id: str | None,
) -> models.Plate:
    """Create — or update — the single Plate sighting row for this vehicle's
    presence on this camera.

    `existing_plate_id` comes from pipeline/plate_tracker.py: it is the row this
    track already owns. When set, the same tracked vehicle is still in frame and
    the row is UPDATED (better confidence, more corroborating reads, extended
    last_seen) instead of inserting another route hop for a vehicle that never
    moved. When None, this is a genuinely new sighting.

    Confidence only ever moves UP. A later, worse read of a plate we have
    already read well must not degrade the recorded quality of the sighting —
    the peak is the honest answer to "how well was this ever read", and the
    voting in plate_tracker has already decided WHICH text is correct.
    """
    now = datetime.utcnow()
    plate = None
    if existing_plate_id:
        plate = db.query(models.Plate).filter(models.Plate.id == existing_plate_id).first()

    if plate is not None:
        target_confidence = max(confidence, plate.confidence or 0.0)
        target_text = normalized_text
        target_raw = raw_text or plate.plate_text_raw
        target_reads = reads_count
        target_last_seen = now
        target_snapshot = plate.snapshot_path or snapshot_path
        target_plate_bbox = plate_bbox if plate_bbox is not None else plate.plate_bbox
        target_vehicle_bbox = vehicle_bbox if vehicle_bbox is not None else plate.vehicle_bbox
        plate.confidence = target_confidence
        plate.plate_text_normalized = target_text
        plate.plate_text_raw = target_raw
        plate.reads_count = target_reads
        plate.last_seen = target_last_seen
        plate.snapshot_path = target_snapshot
        plate.plate_bbox = target_plate_bbox
        plate.vehicle_bbox = target_vehicle_bbox

        def reapply():
            db.add(plate)
            plate.confidence = target_confidence
            plate.plate_text_normalized = target_text
            plate.plate_text_raw = target_raw
            plate.reads_count = target_reads
            plate.last_seen = target_last_seen
            plate.snapshot_path = target_snapshot
            plate.plate_bbox = target_plate_bbox
            plate.vehicle_bbox = target_vehicle_bbox
    else:
        plate = models.Plate(
            vehicle_id=vehicle.id, camera_id=camera_id, detection_id=detection_id,
            plate_text_raw=raw_text, plate_text_normalized=normalized_text,
            confidence=confidence, snapshot_path=snapshot_path,
            source_timestamp=source_timestamp, timestamp=now, last_seen=now,
            track_id=track_id, reads_count=reads_count,
            vehicle_class=vehicle_class, detection_confidence=detection_confidence,
            vehicle_bbox=vehicle_bbox, plate_bbox=plate_bbox,
        )
        db.add(plate)

        def reapply():
            db.add(plate)

    await safe_flush(db, "upsert_plate_sighting", reapply=reapply)
    return plate


def get_route(db: Session, vehicle_id: str):
    """Ordered cross-camera sightings for a vehicle, built from real Plate rows.

    V2: consecutive rows on the SAME camera are collapsed into one hop carrying
    `first_seen`/`last_seen`/`dwell_seconds`, so a vehicle that was recognized
    repeatedly at one junction reads as "seen at C-014 for 40s", not as forty
    hops between C-014 and itself. Non-consecutive returns to the same camera
    stay separate hops — a vehicle that genuinely came back IS a second
    sighting, and collapsing those would erase real movement.

    Camera coordinates are included so the map/journey UI does not have to
    cross-reference a separate /api/cameras fetch per hop.
    """
    plates = (
        db.query(models.Plate)
        .filter(models.Plate.vehicle_id == vehicle_id)
        .order_by(models.Plate.timestamp.asc())
        .all()
    )
    # One lookup for every camera involved, rather than a query per sighting.
    camera_ids = {p.camera_id for p in plates}
    cameras = {
        c.id: c for c in db.query(models.Camera).filter(models.Camera.id.in_(camera_ids)).all()
    } if camera_ids else {}

    sightings: list[dict] = []
    for p in plates:
        cam = cameras.get(p.camera_id)
        if not cam:
            continue
        last_seen = p.last_seen or p.timestamp
        previous = sightings[-1] if sightings else None
        if previous is not None and previous["camera_id"] == cam.id:
            # Same camera as the previous hop — extend it rather than adding a
            # duplicate. Keeps the highest confidence actually achieved and any
            # snapshot we managed to capture.
            previous["last_seen"] = max(previous["last_seen"], last_seen)
            previous["dwell_seconds"] = max(
                0.0, (previous["last_seen"] - previous["first_seen"]).total_seconds()
            )
            previous["confidence"] = max(previous["confidence"], p.confidence or 0.0)
            previous["reads_count"] = (previous["reads_count"] or 0) + (p.reads_count or 1)
            previous["snapshot_path"] = previous["snapshot_path"] or p.snapshot_path
            continue
        sightings.append({
            "camera_id": cam.id,
            "camera_code": cam.camera_code,
            "camera_name": cam.name,
            "location": cam.location or "",
            "lat": cam.lat or 0.0,
            "lng": cam.lng or 0.0,
            # `timestamp` stays the first-seen instant — the existing API
            # contract (schemas.SightingOut) and every current caller depend on
            # this field, so it is preserved, not renamed.
            "timestamp": p.timestamp,
            "first_seen": p.timestamp,
            "last_seen": last_seen,
            "dwell_seconds": max(0.0, (last_seen - p.timestamp).total_seconds()),
            "confidence": p.confidence or 0.0,
            "reads_count": p.reads_count or 1,
            "track_id": p.track_id,
            "vehicle_class": p.vehicle_class or "",
            "plate_id": p.id,
            "detection_id": p.detection_id,
            "snapshot_path": p.snapshot_path,
        })
    return sightings


def get_vehicle_summary(db: Session, vehicle_id: str, live_window_seconds: float = 120.0) -> dict | None:
    """Aggregate investigation header for one vehicle: where it is now (or was
    last), how much we have on it, and an explainable risk score.

    Every number here is counted from real rows. `is_live` in particular is
    derived from how recently the vehicle was actually seen — the UI must be
    able to distinguish "on camera now" from "last known position", and a stale
    row must never be presented as a live one.
    """
    vehicle = db.query(models.Vehicle).filter(models.Vehicle.id == vehicle_id).first()
    if vehicle is None:
        return None

    route = get_route(db, vehicle_id)
    sighting_rows = db.query(models.Plate).filter(models.Plate.vehicle_id == vehicle_id).count()
    cameras_visited = len({hop["camera_id"] for hop in route})
    alert_count = db.query(models.Alert).filter(models.Alert.vehicle_id == vehicle_id).count()
    incident_count = db.query(models.Incident).filter(models.Incident.vehicle_id == vehicle_id).count()
    evidence_count = (
        db.query(models.Evidence)
        .join(models.Alert, models.Evidence.alert_id == models.Alert.id)
        .filter(models.Alert.vehicle_id == vehicle_id)
        .count()
    )

    current = route[-1] if route else None
    last_seen = vehicle.last_seen
    if current is not None:
        last_seen = max(last_seen or current["last_seen"], current["last_seen"])
    is_live = bool(
        last_seen is not None
        and (datetime.utcnow() - last_seen).total_seconds() <= live_window_seconds
    )

    watchlist_entry = db.query(models.WatchlistEntry).filter(
        models.WatchlistEntry.entity_type == "plate",
        models.WatchlistEntry.identifier == vehicle.plate_text,
        models.WatchlistEntry.active == True,  # noqa: E712
    ).first()

    assessment = risk.assess(risk.RiskSignals(
        watchlist_priority=watchlist_entry.priority if watchlist_entry else None,
        plate_text=vehicle.plate_text or "",
        plate_confidence=vehicle.plate_confidence or 0.0,
        plate_reads=sum(hop.get("reads_count") or 0 for hop in route),
        cameras_visited=cameras_visited,
        total_sightings=len(route),
        at=last_seen,
        prior_incidents=incident_count,
        related_alerts=alert_count,
    ))

    return {
        "vehicle": vehicle,
        "total_sightings": len(route),
        # Raw Plate row count is kept separate from the collapsed hop count:
        # they legitimately differ once consecutive same-camera rows merge, and
        # conflating them would misreport one of them.
        "sighting_records": sighting_rows,
        "cameras_visited": cameras_visited,
        "first_seen": route[0]["first_seen"] if route else vehicle.first_seen,
        "last_seen": last_seen,
        "current_camera_id": current["camera_id"] if current else None,
        "current_camera_code": current["camera_code"] if current else None,
        "current_camera_name": current["camera_name"] if current else None,
        "current_seen_at": current["last_seen"] if current else None,
        "is_live": is_live,
        "alert_count": alert_count,
        "incident_count": incident_count,
        "evidence_count": evidence_count,
        "watchlist_flag": bool(vehicle.watchlist_flag),
        "best_plate_confidence": vehicle.plate_confidence or 0.0,
        "risk_score": assessment.score,
        "risk_severity": assessment.severity,
        "risk_factors": assessment.as_dicts(),
    }


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
