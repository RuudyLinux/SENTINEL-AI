"""Cross-camera correlation: same normalized plate text seen on any camera is
the same Vehicle. This is the real mechanism behind the doc's
'C-014 -> C-019 -> C-027' cross-camera route (§18, §60) — driven by actual
OCR reads persisted in the Plate/Vehicle tables, not scripted data.

Also: cross-camera PERSON correlation by appearance-similarity signature (Phase
5) — see `find_similar_person_detections` and pipeline/appearance.py. Explicitly
not face recognition or identity resolution: a ranked visual-similarity candidate
list only, for an investigator to review manually.
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
    """BUG-1 recovery path: another session's Vehicle row for this exact
    plate already won the race and committed between our read and our write.
    Fold this read's confidence/last_seen into THAT row instead of leaving
    our own insert attempt as a rejected no-op. Returns None if no such row
    can be found after all (see caller — that means the conflict was NOT a
    plate_text collision, and must not be silently absorbed)."""
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
    """Final-demo-readiness-phase finding: this function's own `db.flush()`
    was unguarded against SQLite lock contention — the same root-cause class
    PR #1 fixed in worker.py's detection insert, just in a different, shared
    call site (both worker.py's real live pipeline AND demo_scenario.py call
    this). Now retried with the same bounded rollback -> reapply -> backoff
    contract as every other write in the pipeline (db_retry.safe_flush).

    BUG-1 fix (10/10 debugging pass): the read-then-insert below is a classic
    TOCTOU race across TWO DIFFERENT sessions — each camera worker holds its
    own session for its stream's whole lifetime (worker.py), so two cameras
    seeing the same never-before-seen plate within the same race window could
    each see "nothing yet" and each insert, silently splitting one real
    vehicle across two rows (see models.py::Vehicle.plate_text for the DB-
    level half of this fix). The CREATE path below now commits directly
    (locked_commit — no retry, so IntegrityError reaches here rather than
    being swallowed by safe_flush's generic retry/give-up handling, and
    committed rather than merely flushed so a competing session's insert
    resolves fast instead of blocking on an open transaction — see that
    call's own comment) and, on a unique-constraint conflict, folds this
    read into whichever row actually won instead of leaving a rejected,
    wasted write.
    """
    vehicle = db.query(models.Vehicle).filter(models.Vehicle.plate_text == normalized_plate).first()
    now = datetime.utcnow()
    if vehicle:
        target_last_seen = now
        target_confidence = max(confidence, vehicle.plate_confidence)
        # The cached flag is refreshed at every sighting of an existing
        # vehicle, which is the moment it matters and the only event that
        # reliably follows a time-based expiry (nothing runs at the instant an
        # entry's `valid_until` passes). Deactivation and creation refresh it
        # directly through the watchlist router.
        target_watchlist_flag = watchlist.plate_entry_in_force(db, normalized_plate) is not None
        # Corroboration only ever RATCHETS UP, exactly as confidence does: once
        # this vehicle's plate has been confirmed across frames, a later
        # single-frame sighting does not un-confirm it. A vehicle identified
        # well at one camera must not be downgraded by a glimpse at the next.
        target_corroborated = bool(vehicle.plate_corroborated) or bool(corroborated)
        vehicle.last_seen = target_last_seen
        vehicle.plate_confidence = target_confidence
        vehicle.watchlist_flag = target_watchlist_flag
        vehicle.plate_corroborated = target_corroborated

        def reapply():
            # `vehicle` is already PERSISTENT here — a rollback expires its
            # mutated attributes back to their last-committed value, so
            # reapply must reassign from these captured locals, never from
            # re-reading vehicle.* (same reasoning as db_retry.py's module
            # docstring / worker.py's own reapply callbacks).
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
    # Committed immediately, not just flushed, deliberately: a genuinely NEW
    # Vehicle has nothing else in this transaction depending on it yet (the
    # Plate/Detection rows that reference it are added AFTER this call
    # returns, by the caller), so nothing is lost by making it durable right
    # away — and a competing session's conflicting insert needs this row to
    # actually COMMIT to resolve as a fast IntegrityError. Left merely
    # flushed (uncommitted), a concurrent camera worker's competing insert
    # would instead BLOCK on SQLite's write lock for the full busy_timeout
    # (up to 30s) waiting on a transaction this session has no reason to
    # commit until it finishes the REST of its own frame's processing —
    # turning a race this fix is supposed to resolve in milliseconds into a
    # real multi-second stall on an unrelated camera's pipeline. Found via
    # tests/test_vehicle_upsert_race.py's real concurrent-asyncio-tasks case,
    # not by inspection alone.
    #
    # A dedicated retry loop, not safe_commit/safe_flush: those two share one
    # contract (retry ONLY OperationalError, swallow everything else into a
    # single "attempt failed" outcome with no way for the caller to tell
    # WHICH exception it was) that is exactly wrong here — an IntegrityError
    # is a definitive answer needing zero retries and different recovery
    # (merge into the winner), while OperationalError is the transient
    # condition needing the retry/backoff. Same attempt count/backoff shape
    # as db_retry._attempt_loop, so this path is not less resilient to lock
    # contention than every other write in the pipeline.
    max_attempts = 4
    for attempt in range(1, max_attempts + 1):
        try:
            await locked_commit(db)
            return vehicle
        except IntegrityError:
            # Not a lock — a real unique-constraint hit. The DB itself just
            # proved another session's row for this exact plate already
            # exists; roll back OUR failed insert and merge into that row.
            await locked_rollback(db)
            winner = await _merge_into_winner(db, normalized_plate, confidence, now)
            if winner is not None:
                logger.info(
                    "upsert_vehicle_for_plate: recovered a concurrent-insert race for plate %s "
                    "by merging into the winning row instead of creating a duplicate.",
                    normalized_plate,
                )
                return winner
            # A conflict happened but no row exists for this plate —
            # genuinely unexpected (some OTHER constraint, not the one this
            # fix targets). Must not be silently absorbed as if it were the
            # race this function understands.
            raise
        except OperationalError:
            # Genuine lock contention, not a constraint conflict. A failed
            # commit leaves the Session's transaction unusable (SQLAlchemy
            # raises PendingRollbackError on the next call otherwise) — roll
            # back before any further use, same requirement db_retry.py's
            # own module docstring documents for every other retry path.
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
    corroborated: bool = True,
    ocr_variant: str = "",
    variants_agreeing: int = 1,
    plate_crop_path: str | None = None,
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
