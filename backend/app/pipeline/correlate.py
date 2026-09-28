"""Cross-camera correlation: the same normalized plate on any camera is the same
Vehicle. That's what builds the C-014 -> C-019 -> C-027 route (doc §18, §60),
from real OCR reads in the Plate/Vehicle tables.

Also person similarity across cameras via appearance signatures
(find_similar_person_detections, pipeline/appearance.py). Not face
recognition or identity: a ranked list of lookalikes for an investigator.
"""
import asyncio
import logging
from datetime import datetime

from sqlalchemy.exc import IntegrityError, OperationalError
from sqlalchemy.orm import Session

from .. import models, watchlist
from . import risk
from .anpr import review_status_for
from .appearance import similarity
from .db_retry import locked_commit, locked_rollback, safe_flush

logger = logging.getLogger("sentinel.correlate")


async def _merge_into_winner(db: Session, normalized_plate: str, confidence: float, now: datetime) -> "models.Vehicle | None":
    """Another session committed a Vehicle for this plate between our read and
    our insert. Fold this read into that row. None if there's no such row,
    meaning the conflict wasn't a plate_text collision (caller re-raises)."""
    winner = db.query(models.Vehicle).filter(models.Vehicle.plate_text == normalized_plate).first()
    if winner is None:
        return None
    target_last_seen = now
    target_confidence = max(confidence, winner.plate_confidence or 0.0)
    winner.last_seen = target_last_seen
    winner.plate_confidence = target_confidence

    def reapply():
        db.add(winner)
        winner.last_seen = target_last_seen
        winner.plate_confidence = target_confidence

    await safe_flush(db, "upsert_vehicle_for_plate_conflict_resolution", reapply=reapply)
    return winner


async def upsert_vehicle_for_plate(
    db: Session, normalized_plate: str, confidence: float, corroborated: bool = False,
) -> models.Vehicle:
    """Get or create the Vehicle for a plate (used by the live worker and
    demo_scenario.py). Updates go through safe_flush like every other write.

    Read-then-insert races across sessions: each camera worker has its own
    session, so two cameras seeing a new plate at once could both insert and
    split one vehicle over two rows (models.Vehicle.plate_text is unique for
    that reason). The create path commits directly and, on a unique conflict,
    merges into whichever row won.
    """
    vehicle = db.query(models.Vehicle).filter(models.Vehicle.plate_text == normalized_plate).first()
    now = datetime.utcnow()
    if vehicle:
        target_last_seen = now
        target_confidence = max(confidence, vehicle.plate_confidence)
        # Refresh the cached flag on every sighting; nothing runs at the moment
        # an entry's valid_until passes, so this is where expiry lands. Create
        # and deactivate update it directly in the watchlist router.
        target_watchlist_flag = watchlist.plate_entry_in_force(db, normalized_plate) is not None
        # only ratchets up like confidence: a glimpse at the next camera
        # doesn't un-confirm a plate confirmed earlier
        target_corroborated = bool(vehicle.plate_corroborated) or bool(corroborated)
        vehicle.last_seen = target_last_seen
        vehicle.plate_confidence = target_confidence
        vehicle.watchlist_flag = target_watchlist_flag
        vehicle.plate_corroborated = target_corroborated

        def reapply():
            # persistent row, rollback expires these; reassign from the locals
            db.add(vehicle)
            vehicle.last_seen = target_last_seen
            vehicle.plate_confidence = target_confidence
            vehicle.watchlist_flag = target_watchlist_flag
            vehicle.plate_corroborated = target_corroborated

        await safe_flush(db, "upsert_vehicle_for_plate", reapply=reapply)
        return vehicle

    watchlisted = watchlist.plate_entry_in_force(db, normalized_plate)
    vehicle = models.Vehicle(
        plate_text=normalized_plate,
        plate_confidence=confidence,
        first_seen=now,
        last_seen=now,
        watchlist_flag=bool(watchlisted),
        plate_corroborated=bool(corroborated),
    )
    db.add(vehicle)
    # Commit now, not just flush. Nothing else in this transaction depends on
    # the new row yet (the caller adds Plate/Detection after), and a competing
    # insert only fails fast with IntegrityError once this row is committed.
    # Left flushed, the other camera's insert blocks on SQLite's write lock for
    # up to busy_timeout (30s) while we finish our frame. Caught by
    # tests/test_vehicle_upsert_race.py.
    #
    # Own retry loop instead of safe_commit/safe_flush: those retry
    # OperationalError and fold everything else into one "failed" result, but
    # here IntegrityError needs no retry and a different recovery (merge into
    # the winner). Same attempts/backoff as db_retry._attempt_loop.
    max_attempts = 4
    for attempt in range(1, max_attempts + 1):
        try:
            await locked_commit(db)
            return vehicle
        except IntegrityError:
            # a real unique-constraint hit, another session's row exists
            await locked_rollback(db)
            winner = await _merge_into_winner(db, normalized_plate, confidence, now)
            if winner is not None:
                logger.info(
                    "upsert_vehicle_for_plate: recovered a concurrent-insert race for plate %s "
                    "by merging into the winning row instead of creating a duplicate.",
                    normalized_plate,
                )
                return winner
            # conflict but no row for this plate, so some other constraint.
            # don't swallow it
            raise
        except OperationalError:
            # lock contention. a failed commit leaves the transaction unusable
            # (PendingRollbackError next time), so roll back first
            await locked_rollback(db)
            if attempt >= max_attempts:
                logger.warning(
                    "upsert_vehicle_for_plate: giving up on plate %s after %d attempts — still locked.",
                    normalized_plate, max_attempts,
                )
                raise
            db.add(vehicle)  # re-add the still-transient object; rollback only detached it
            await asyncio.sleep(min(0.5, 0.05 * (2 ** (attempt - 1))))
    raise AssertionError("unreachable")  # loop always returns or raises


async def upsert_track(
    db: Session, camera_id: str, yolo_track_id: int, cls: str, at: datetime,
    vehicle_id: str | None = None, plate_reads: int = 0,
) -> models.Track:
    """Get or create the Track row for one ByteTrack id on one camera.

    Ids are unique per predictor and detector.py keeps one per camera, so the
    identity is (camera_id, yolo_track_id). Ids get recycled eventually on a
    long-running camera; we just move last_seen forward rather than split a
    vehicle's history over new rows.
    """
    track = (
        db.query(models.Track)
        .filter(models.Track.camera_id == camera_id, models.Track.yolo_track_id == yolo_track_id)
        .first()
    )
    if track is not None:
        target_last_seen = at
        target_count = (track.detection_count or 0) + 1
        # identity only gets added, a frame where the plate didn't read
        # mustn't un-identify the vehicle
        target_vehicle_id = vehicle_id or track.vehicle_id
        target_plate_reads = max(plate_reads, track.plate_reads or 0)
        track.last_seen = target_last_seen
        track.detection_count = target_count
        track.vehicle_id = target_vehicle_id
        track.plate_reads = target_plate_reads

        def reapply():
            # persistent row, reassign from the locals (db_retry.py)
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
            # new row, rollback just detaches it
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
    corroborated: bool = True,
    ocr_variant: str = "",
    variants_agreeing: int = 1,
    plate_crop_path: str | None = None,
) -> models.Plate:
    """Create or update the one Plate sighting row for this vehicle's stay on
    this camera.

    existing_plate_id is the row this track already owns (plate_tracker.py).
    Set means the same car is still in frame, so the row is updated instead of
    adding another route hop for a car that never moved.

    Confidence only goes up; the peak is the honest answer to "how well was
    this ever read", and plate_tracker's voting already chose the text.
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
        target_review_status = review_status_for(target_confidence, plate.review_status, corroborated)
        target_plate_crop = plate.plate_crop_path or plate_crop_path
        plate.confidence = target_confidence
        plate.plate_text_normalized = target_text
        plate.plate_text_raw = target_raw
        plate.reads_count = target_reads
        plate.last_seen = target_last_seen
        plate.snapshot_path = target_snapshot
        plate.plate_bbox = target_plate_bbox
        plate.vehicle_bbox = target_vehicle_bbox
        plate.review_status = target_review_status
        plate.ocr_variant = ocr_variant or plate.ocr_variant
        plate.variants_agreeing = variants_agreeing
        plate.corroborated = corroborated
        plate.plate_crop_path = target_plate_crop

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
            plate.review_status = target_review_status
            plate.ocr_variant = ocr_variant or plate.ocr_variant
            plate.variants_agreeing = variants_agreeing
            plate.corroborated = corroborated
            plate.plate_crop_path = target_plate_crop
    else:
        plate = models.Plate(
            vehicle_id=vehicle.id, camera_id=camera_id, detection_id=detection_id,
            plate_text_raw=raw_text, plate_text_normalized=normalized_text,
            confidence=confidence, snapshot_path=snapshot_path,
            source_timestamp=source_timestamp, timestamp=now, last_seen=now,
            track_id=track_id, reads_count=reads_count,
            vehicle_class=vehicle_class, detection_confidence=detection_confidence,
            vehicle_bbox=vehicle_bbox, plate_bbox=plate_bbox,
            review_status=review_status_for(confidence, None, corroborated),
            ocr_variant=ocr_variant or None, variants_agreeing=variants_agreeing,
            corroborated=corroborated, plate_crop_path=plate_crop_path,
        )
        db.add(plate)

        def reapply():
            db.add(plate)

    await safe_flush(db, "upsert_plate_sighting", reapply=reapply)
    return plate


def get_route(db: Session, vehicle_id: str):
    """A vehicle's sightings across cameras, in order, from Plate rows.

    Consecutive rows on the same camera collapse into one hop with
    first_seen/last_seen/dwell_seconds ("at C-014 for 40s", not forty hops).
    Coming back to a camera later is a separate hop, that's real movement.
    Camera coordinates are included so the map doesn't need another fetch.
    """
    plates = (
        db.query(models.Plate)
        .filter(models.Plate.vehicle_id == vehicle_id)
        .order_by(models.Plate.timestamp.asc())
        .all()
    )
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
            # same camera as last hop, extend it. keep best confidence and
            # any snapshot we got
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
            # first-seen instant; schemas.SightingOut and callers use this name
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
    """Investigation header for one vehicle: where it is (or was last), how
    much we have on it, and a risk score. Counted from real rows. is_live
    comes from how recently it was seen, so a stale row never shows as live.
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

    watchlist_entry = watchlist.plate_entry_in_force(db, vehicle.plate_text)

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
        # raw row count vs collapsed hops, they differ once same-camera rows merge
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
    """Other person detections that look like the reference one, ranked. A lead
    list, not an identity match (pipeline/appearance.py). Detections with no
    stored signature are skipped; [] if the reference has none."""
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
