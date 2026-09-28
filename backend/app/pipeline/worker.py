"""Per-camera background task: frames -> YOLO detection -> ANPR -> persist ->
rules -> broadcast. Runs against a webcam, RTSP stream or uploaded video file
(see source.py).

State machine, reconnect logic and frame helpers live in camera_state.py,
camera_connection.py, db_helpers.py and frame_utils.py; their names are
re-exported below because routers and tests import them from here.
_process_frame and _camera_loop stay together on purpose: they're one control
flow sharing one session, and the ANPR helpers are only reached from there.
"""
import asyncio
import logging
import time
from datetime import datetime, timezone
from typing import Any

import cv2
import numpy as np
from sqlalchemy.orm import Session

logger = logging.getLogger("sentinel.worker")

from .. import models, metrics, background
from ..db import SessionLocal
from ..config import settings
from ..ws import manager, EventType
from .source import CameraSource
from .detector import detect_and_track, release_model
from .anpr import (
    read_plate, passes_anpr_gate, review_status_for,
    read_plate_structured, passes_read_gate, looks_like_plate, OcrRead,
)
from .appearance import compute_signature
from .correlate import upsert_vehicle_for_plate, upsert_plate_sighting, upsert_track
from . import plate_detector, plate_preprocess, plate_tracker
from .rules_engine import evaluate, find_incident_for_alert
from .timing import compute_source_timestamp
from .db_retry import close_session, run_db
from .frame_reader import FrameResult, LatestFrameReader
from ..evidence_hash import sha256_file
from ..self_heal import engine as self_heal
from . import ai_capacity, clips, recorder
from .camera_state import (
    # re-exported: routers and tests use app.pipeline.worker.<name>
    CAMERA_STATS, GRID_STATES, ILLEGAL_TRANSITIONS, _DB_STATUS_FOR_GRID_STATE,
    _TRANSITIONS, _ema, _set_grid_state, _stats,
)
from .db_helpers import _safe_commit, _safe_flush  # test_worker_resilience imports _safe_commit from here
from .camera_connection import _open_with_timeout, _reopen_with_backoff
from .frame_utils import _draw_boxes, _restore_row, _save_snapshot, _snapshot_attrs

LATEST_FRAMES: dict[str, bytes] = {}
RUNNING: dict[str, "asyncio.Task[None]"] = {}
# open MJPEG streams per camera (routers/streams.py)
VIEWERS: dict[str, int] = {}


def is_watched(camera_id: str) -> bool:
    return VIEWERS.get(camera_id, 0) > 0 or recorder.is_recording(camera_id)

# Both JPEGs made from a frame are consumed at no more than this rate: MJPEG
# sends at most 10 frames/s (routers/streams.py) and clips encode at their
# capture rate (clips._playback_fps). Encoding every source frame on the event
# loop cost ~30 ms per 1080p frame, ~68% of the loop at 23 fps for ONE camera.
# Measured 2026-09-28.
_PREVIEW_INTERVAL_S = 0.1
# Nobody watching (no MJPEG viewer, no recording): refresh the preview this
# often, just so snapshots and the demo have a recent frame. With 30 cameras
# connected the 10/s preview encodes were CPU nobody looked at.
_IDLE_PREVIEW_INTERVAL_S = 2.0
# Loop period for a camera running neither AI nor a viewer: only the
# heartbeat and the occasional preview need a frame.
_IDLE_LOOP_S = 0.5
_JPEG_PARAMS = [int(cv2.IMWRITE_JPEG_QUALITY), 70]


def _encode_jpeg(frame: np.ndarray) -> "bytes | None":
    ok, buf = cv2.imencode(".jpg", frame, _JPEG_PARAMS)
    return buf.tobytes() if ok else None


def _encode_annotated(frame: np.ndarray, detections: list[dict[str, Any]]) -> "bytes | None":
    return _encode_jpeg(_draw_boxes(frame.copy(), detections))


VEHICLE_CLASSES = ("car", "truck", "bus", "motorbike")

# Fields an upsert may mutate on an already-persistent row. Rollback expires a
# persistent object's changes back to the last committed values (it doesn't
# just detach it like a new object), so a retry reassigns these from values
# captured before the rollback. re-add() alone would restore the stale row.
_PLATE_REAPPLY_FIELDS = (
    "confidence", "plate_text_normalized", "plate_text_raw", "reads_count",
    "last_seen", "snapshot_path", "plate_bbox", "vehicle_bbox",
    # upsert_plate_sighting mutates these on the same row too
    "ocr_variant", "variants_agreeing", "corroborated", "plate_crop_path",
)
_TRACK_REAPPLY_FIELDS = ("last_seen", "detection_count", "vehicle_id", "plate_reads")


def _rejection_reason(read) -> str:
    # small fixed set on purpose, it's a prometheus label
    if not read.normalized:
        return "no_text"
    if not looks_like_plate(read.normalized):
        return "bad_format"
    if read.confidence < settings.plate_min_confidence:
        return "low_confidence"
    return "low_variant_agreement"


async def _read_plate_for_track(
    crop: np.ndarray, camera_code: str, offset_x: int, offset_y: int,
) -> "tuple[Any, list[float] | None, np.ndarray | None]":
    """Localize a plate in a vehicle crop and read it.

    Returns (OcrRead, full-frame plate bbox or None, plate crop or None).

    Only the best detected region is read, since each extra region is a full
    OCR pass. Perspective correction only fires on a skewed quad. If the
    localized read fails the gate, the whole vehicle crop is read as a
    fallback: localization sometimes returns a sub-region of the plate and OCR
    then reads nothing (exact match 0.16 -> 0.04 on the labelled corpus, see
    docs/ANPR_ACCURACY.md). The fallback only costs anything on failure.
    """
    metrics.PLATE_DETECT_ATTEMPTS.labels(camera_code=camera_code).inc()
    detect_started = time.monotonic()
    boxes = await asyncio.to_thread(plate_detector.detect_plates, crop)
    metrics.PLATE_DETECT_SECONDS.labels(camera_code=camera_code).observe(
        time.monotonic() - detect_started
    )

    plate_bbox: list[float] | None = None
    plate_crop_image: np.ndarray | None = None
    variants: list[tuple[str, np.ndarray]] = []
    if boxes:
        box = boxes[0]
        metrics.PLATE_LOCALIZED.labels(camera_code=camera_code, source=box.source).inc()
        metrics.PLATE_DETECT_CONFIDENCE.labels(
            camera_code=camera_code, source=box.source,
        ).observe(box.confidence)
        plate_crop_image = plate_detector.crop_plate(crop, box)
        if plate_crop_image is not None:
            variants = await asyncio.to_thread(
                plate_preprocess.build_variants, plate_crop_image,
                plate_detector.quad_in_crop(box, crop),
            )
            # localizer works in crop coords, stored bbox is full-frame
            plate_bbox = [
                box.x1 + offset_x, box.y1 + offset_y, box.x2 + offset_x, box.y2 + offset_y,
            ]
    if not variants and plate_detector.get_plate_model() is not None and not settings.plate_whole_crop_fallback_with_model:
        # Trained detector found no plate, so nothing legible. Whole-crop OCR
        # here was ~200ms per vehicle per frame for reads that almost never
        # passed the gate (see config.plate_whole_crop_fallback_with_model).
        return OcrRead(raw="", normalized="", confidence=0.0, variant="", variants_agreeing=0, variant_count=0), None, None
    if not variants:
        # no plate region: read the whole vehicle crop. plate_bbox stays None
        # so the row says this read wasn't localized
        variants = await asyncio.to_thread(
            plate_preprocess.build_variants, crop, None,
        )
        plate_bbox, plate_crop_image = None, None

    ocr_started = time.monotonic()
    read = await asyncio.to_thread(read_plate_structured, variants)
    if plate_bbox is not None and not passes_read_gate(read):
        fallback_variants = await asyncio.to_thread(plate_preprocess.build_variants, crop, None)
        fallback = await asyncio.to_thread(read_plate_structured, fallback_variants)
        if _better_structured_read(read, fallback) is fallback:
            # winner came from the whole crop, the localized box doesn't describe it
            read, plate_bbox, plate_crop_image = fallback, None, None
    metrics.OCR_SECONDS.labels(camera_code=camera_code).observe(time.monotonic() - ocr_started)
    return read, plate_bbox, plate_crop_image


def _better_structured_read(first, second):
    # same ranking as anpr.better_read (gate pass, then non-empty, then
    # confidence) but keeps the OcrRead so variant provenance survives
    first_passes, second_passes = passes_read_gate(first), passes_read_gate(second)
    if first_passes != second_passes:
        return first if first_passes else second
    if bool(first.normalized) != bool(second.normalized):
        return first if first.normalized else second
    return first if first.confidence >= second.confidence else second


async def _anpr_ocr(
    detection: dict[str, Any], frame: np.ndarray, camera_id: str, camera_code: str,
) -> "dict[str, Any] | None":
    """The OCR half of ANPR: the slow part, no database.

    Run BEFORE the detection row is flushed. The flush opens SQLite's single
    write transaction until the per-detection commit, and OCR sitting inside it
    held the write lock for seconds per read. API requests and clip writes then
    hit the 30s busy timeout ("database is locked"), seen live as a 500 on
    camera creation and as lost event clips.

    Returns None for an empty crop, else the inputs _run_anpr needs.
    """
    x1, y1, x2, y2 = [max(0, int(v)) for v in detection["bbox"]]
    crop = frame[y1:y2, x1:x2]
    if crop.size == 0:
        return None
    raw_track_id = detection.get("track_id")
    if not settings.plate_pipeline_v2 or raw_track_id is None:
        raw, normalized, conf = await asyncio.to_thread(read_plate, crop)
        # The snapshot _run_anpr will attach, saved now for the same reason as
        # the OCR: inside the transaction this thread hop held the write lock
        # while it queued behind other cameras' inference.
        snapshot_path = (
            await asyncio.to_thread(_save_snapshot, frame, camera_code)
            if passes_anpr_gate(normalized, conf) else None
        )
        return {"legacy_read": (raw, normalized, conf), "snapshot_path": snapshot_path}

    track_id = str(raw_track_id)
    state = plate_tracker.touch(camera_id, track_id)
    new_read = False
    if plate_tracker.should_ocr(camera_id, track_id):
        read, plate_bbox, plate_crop_image = await _read_plate_for_track(
            crop, camera_code, offset_x=x1, offset_y=y1,
        )
        if passes_read_gate(read):
            metrics.PLATE_OCR_ACCEPTED.labels(camera_code=camera_code).inc()
            metrics.OCR_CONFIDENCE.labels(camera_code=camera_code).observe(read.confidence)
            metrics.PLATE_VARIANTS_AGREEING.labels(camera_code=camera_code).observe(read.variants_agreeing)
            state = plate_tracker.record_read(
                camera_id, track_id, read.normalized, read.confidence, read.raw, plate_bbox,
                variant=read.variant, variants_agreeing=read.variants_agreeing,
                plate_crop=plate_crop_image,
            )
            new_read = True
        else:
            metrics.PLATE_OCR_REJECTED.labels(
                camera_code=camera_code, reason=_rejection_reason(read),
            ).inc()
            # so an unreadable track (truck rear, plate out of frame) waits for
            # the reverify interval instead of retrying every cycle
            plate_tracker.mark_ocr_attempt(camera_id, track_id)

    # A new sighting gets one snapshot (see _run_anpr). Decided from tracker
    # state only, the same conditions _run_anpr checks before it saves: a
    # winning read, consensus if strict mode wants it, and no row yet (no row
    # means should_persist is True).
    snapshot_path = plate_crop_path = None
    best = state.best()
    if (
        best is not None and state.plate_row_id is None
        and (plate_tracker.has_consensus(camera_id, track_id) or not settings.plate_require_consensus)
    ):
        snapshot_path = await asyncio.to_thread(_save_snapshot, frame, camera_code)
        if settings.plate_debug_crops and state.last_plate_crop is not None:
            plate_crop_path = await asyncio.to_thread(
                _save_snapshot, state.last_plate_crop, f"{camera_code}_plate",
            )
    return {"state": state, "new_read": new_read, "snapshot_path": snapshot_path, "plate_crop_path": plate_crop_path}


async def _run_anpr(
    db: Session, detection: dict[str, Any], det_row: models.Detection, frame: np.ndarray,
    camera_id: str, camera_code: str, frame_source_ts: datetime | None,
    ocr: "dict[str, Any] | None | bool" = False,
) -> "tuple[models.Vehicle | None, models.Plate | None, str | None]":
    """ANPR for one vehicle detection. Returns (vehicle, plate_row, snapshot_path).

    V2 (default) needs a ByteTrack id: OCR runs only while the track still
    needs a read, reads vote instead of overwriting, and there's one sighting
    row per (camera, track) that gets updated.

    Legacy (PLATE_PIPELINE_V2=false, or no track id yet on an object's first
    frames): whole crop to OCR, one row per passing frame, same as before V2.
    """
    # ocr is _anpr_ocr's result when the caller ran it outside the write
    # transaction (the frame loop always does). False = run it here
    if ocr is False:
        ocr = await _anpr_ocr(detection, frame, camera_id, camera_code)
    if ocr is None:
        return None, None, None
    x1, y1, x2, y2 = [max(0, int(v)) for v in detection["bbox"]]
    vehicle_bbox = [float(x1), float(y1), float(x2), float(y2)]
    raw_track_id = detection.get("track_id")

    # legacy path
    if "legacy_read" in ocr:
        raw, normalized, conf = ocr["legacy_read"]
        if not passes_anpr_gate(normalized, conf):
            return None, None, None
        snapshot_path = ocr.get("snapshot_path") or await asyncio.to_thread(_save_snapshot, frame, camera_code)
        vehicle = await upsert_vehicle_for_plate(db, normalized, conf)
        plate_row = models.Plate(
            vehicle_id=vehicle.id, camera_id=camera_id, detection_id=det_row.id,
            plate_text_raw=raw, plate_text_normalized=normalized,
            confidence=conf, snapshot_path=snapshot_path,
            source_timestamp=frame_source_ts,
            track_id=(str(raw_track_id) if raw_track_id is not None else None),
            reads_count=1, vehicle_class=detection["cls"],
            detection_confidence=detection["confidence"], vehicle_bbox=vehicle_bbox,
            review_status=review_status_for(conf),
        )
        db.add(plate_row)
        return vehicle, plate_row, snapshot_path

    # V2 path
    track_id = str(raw_track_id)
    state, new_read = ocr["state"], ocr["new_read"]

    best = state.best()
    if best is None:
        return None, None, None
    plate_text, peak_confidence, reads = best
    # A plate is only trusted once enough frames agree. An uncorroborated read
    # is still recorded (a car crossing in one cycle is real) but goes to
    # pending_review, so one lucky frame can't pose as a settled identity.
    corroborated = plate_tracker.has_consensus(camera_id, track_id)
    if not corroborated and settings.plate_require_consensus:
        # strict mode, operator wants nothing uncorroborated
        return None, None, None
    if not plate_tracker.should_persist(camera_id, track_id, new_read):
        # nothing worth writing, but still return the vehicle so watchlist
        # rules keep firing
        vehicle = (
            db.query(models.Vehicle).filter(models.Vehicle.id == state.vehicle_id).first()
            if state.vehicle_id else None
        )
        return vehicle, None, None

    vehicle = await upsert_vehicle_for_plate(db, plate_text, peak_confidence, corroborated)
    snapshot_path = None
    plate_crop_path = None
    if state.plate_row_id is None:
        # one snapshot per sighting, not per OCR frame. Normally already saved
        # by _anpr_ocr before the write transaction opened.
        if "snapshot_path" in ocr:
            snapshot_path, plate_crop_path = ocr["snapshot_path"], ocr.get("plate_crop_path")
        if snapshot_path is None:
            snapshot_path = await asyncio.to_thread(_save_snapshot, frame, camera_code)
            # the plate region OCR actually read, so a reviewer can check it.
            # opt-in, and a failure here mustn't cost the sighting
            if settings.plate_debug_crops and state.last_plate_crop is not None:
                plate_crop_path = await asyncio.to_thread(
                    _save_snapshot, state.last_plate_crop, f"{camera_code}_plate",
                )
    metrics.PLATE_CONSENSUS_REACHED.labels(
        camera_code=camera_code, outcome="corroborated" if corroborated else "uncorroborated",
    ).inc()
    plate_row = await upsert_plate_sighting(
        db, vehicle=vehicle, camera_id=camera_id, track_id=track_id,
        detection_id=str(det_row.id), raw_text=state.votes[plate_text].raw,
        normalized_text=plate_text, confidence=peak_confidence, reads_count=reads,
        vehicle_class=detection["cls"], detection_confidence=detection["confidence"],
        vehicle_bbox=vehicle_bbox, plate_bbox=state.last_plate_bbox,
        snapshot_path=snapshot_path, source_timestamp=frame_source_ts,
        existing_plate_id=state.plate_row_id,
        corroborated=corroborated,
        ocr_variant=state.votes[plate_text].variant,
        variants_agreeing=state.votes[plate_text].variants_agreeing,
        plate_crop_path=plate_crop_path,
    )
    plate_tracker.bind_plate_row(camera_id, track_id, str(plate_row.id), str(vehicle.id), plate_text)
    metrics.VEHICLE_SIGHTINGS.labels(camera_code=camera_code).inc()
    return vehicle, plate_row, plate_row.snapshot_path


async def _process_frame(
    db: Session, camera: models.Camera, frame: np.ndarray, frame_idx: int, w: int, h: int,
    frame_source_ts: datetime | None, last_detections: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Inference (every detect_every_n_frames), ANPR, persistence and alerting
    for one frame. Returns the detections to draw on the MJPEG frame and pass
    back in next time."""
    # Column()-style models type attributes as Column[T] for a checker; at
    # runtime they're plain values. The casts are for the checker only.
    camera_id = str(camera.id)
    camera_code = str(camera.camera_code)
    want_person = bool(camera.ai_person)
    want_vehicle = bool(camera.ai_vehicle)
    ai_enabled = want_person or want_vehicle

    # Connected with AI off must cost nothing past decode/MJPEG, so skip
    # detect_and_track entirely.
    # No free AI slot (pipeline/ai_capacity.py) = stream without inference.
    if ai_enabled and not ai_capacity.try_acquire(camera_id):
        ai_enabled = want_person = want_vehicle = False
        if not _stats(camera_id).get("ai_blocked"):
            # Lost (or never had) a slot: drop its tracker. Track ids restart
            # when it gets a slot back, so the plate votes keyed on them go too.
            release_model(camera_id)
            plate_tracker.release_camera(camera_id)
        _stats(camera_id)["ai_blocked"] = True
    elif not ai_enabled:
        ai_capacity.release(camera_id)
        _stats(camera_id)["ai_blocked"] = False
    else:
        _stats(camera_id)["ai_blocked"] = False
    if ai_enabled and frame_idx % settings.detect_every_n_frames == 0:
        t0 = time.monotonic()
        detections = await asyncio.to_thread(
            detect_and_track, frame, camera_id, want_person, want_vehicle
        )
        inference_ms = (time.monotonic() - t0) * 1000
        st = _stats(camera_id)
        st["last_inference_ms"] = inference_ms
        st["inference_ms_ema"] = _ema(st["inference_ms_ema"], inference_ms)
        st["frames_processed"] += 1
        metrics.INFERENCE_SECONDS.labels(camera_code=camera_code).observe(inference_ms / 1000.0)
        last_detections = detections

        for d in detections:
            snapshot_path = None
            anpr_wanted = bool(camera.ai_anpr) and d["cls"] in VEHICLE_CLASSES
            # OCR first, while no write transaction is open (see _anpr_ocr)
            ocr = await _anpr_ocr(d, frame, camera_id, camera_code) if anpr_wanted else None
            # Cross-camera appearance signature, also before the write: an
            # await inside the transaction holds SQLite's write lock while it
            # waits for a thread. Visual similarity only, never identity; a
            # failure leaves it null and the detection stays.
            signature = None
            if d["cls"] == "person":
                try:
                    px1, py1, px2, py2 = [max(0, int(v)) for v in d["bbox"]]
                    signature = await asyncio.to_thread(compute_signature, frame[py1:py2, px1:px2])
                except Exception:
                    logger.exception("camera %s: appearance signature failed", camera_code)
            det_row = models.Detection(
                camera_id=camera_id, cls=d["cls"], confidence=d["confidence"],
                bbox=d["bbox"], track_id=(str(d["track_id"]) if d["track_id"] is not None else None),
                model_version=settings.model_version,
                source_timestamp=frame_source_ts,
                appearance_signature=signature,
            )
            db.add(det_row)
            # A real write (assigns det_row's id). Unguarded, a lock here fell
            # through to the loop's outer except and dropped the detection.
            # If it still fails after retries, skip this one detection and
            # keep the rest of the frame.
            flushed = await _safe_flush(db, camera_code, reapply=lambda _det_row=det_row: db.add(_det_row))
            if not flushed:
                continue
            metrics.DETECTIONS_TOTAL.labels(camera_code=camera_code, cls=d["cls"]).inc()

            vehicle = None
            plate_row = None
            track_row = None
            if anpr_wanted:
                vehicle, plate_row, anpr_snapshot = await _run_anpr(
                    db, d, det_row, frame, camera_id, camera_code, frame_source_ts, ocr=ocr,
                )
                if anpr_snapshot:
                    snapshot_path = anpr_snapshot
                    det_row.snapshot_path = snapshot_path  # type: ignore[assignment]

            # A tracked vehicle is worth a Track row whether or not its plate
            # was read; vehicle_id gets filled in once the plate identifies it.
            # Throttled by should_persist_track.
            if settings.plate_pipeline_v2 and d["cls"] in VEHICLE_CLASSES and d.get("track_id") is not None:
                track_key = str(d["track_id"])
                plate_tracker.touch(camera_id, track_key)
                if plate_tracker.should_persist_track(camera_id, track_key):
                    try:
                        track_row = await upsert_track(
                            db, camera_id, int(d["track_id"]), d["cls"],
                            det_row.timestamp or datetime.utcnow(),
                            vehicle_id=(str(vehicle.id) if vehicle is not None else None),
                            plate_reads=(plate_row.reads_count or 0) if plate_row is not None else 0,
                        )
                        plate_tracker.bind_track_row(camera_id, track_key, str(track_row.id))
                    except Exception:
                        # track bookkeeping is metadata, mustn't cost the
                        # detection/plate/alert
                        logger.exception("camera %s: track upsert failed for track %s", camera_code, track_key)
                        track_row = None

            # One commit per detection, not per frame. Tried per-frame to cut
            # WAL growth; holding the transaction across a whole frame's OCR and
            # snapshot awaits caused real "database is locked" with 2 cameras.
            # WAL growth is handled by wal_autocheckpoint in db.py instead.
            #
            # Retry-with-reapply (see db_retry.py): det_row/plate_row may be
            # new objects, which rollback just detaches, so re-add() restores
            # them. vehicle can be an existing row whose last_seen and
            # plate_confidence were just changed (correlate.py); rollback
            # expires those, so reapply sets them from values captured here,
            # never by re-reading vehicle.* (could be the stale value).
            vehicle_target_last_seen = vehicle.last_seen if vehicle is not None else None
            vehicle_target_confidence = vehicle.plate_confidence if vehicle is not None else None
            # V2 plate_row/track_row can be existing rows updated in place too,
            # same trap, so capture their fields now
            plate_target_values = _snapshot_attrs(plate_row, _PLATE_REAPPLY_FIELDS)
            track_target_values = _snapshot_attrs(track_row, _TRACK_REAPPLY_FIELDS)

            def _reapply_detection_commit(
                _det_row=det_row, _plate_row=plate_row, _vehicle=vehicle, _track_row=track_row,
                _last_seen=vehicle_target_last_seen, _confidence=vehicle_target_confidence,
                _plate_values=plate_target_values, _track_values=track_target_values,
            ):
                db.add(_det_row)
                _restore_row(db, _plate_row, _plate_values)
                _restore_row(db, _track_row, _track_values)
                if _vehicle is not None:
                    db.add(_vehicle)  # no-op if already attached
                    _vehicle.last_seen = _last_seen
                    _vehicle.plate_confidence = _confidence

            await _safe_commit(db, camera_code, reapply=_reapply_detection_commit)

            # A watchlisted vehicle's detection needs a snapshot for the alert
            # evaluate() is about to raise. Saved after the commit: taken
            # between the flush and the commit, this thread hop held the write
            # lock while it waited for a free thread.
            if not snapshot_path and vehicle is not None and bool(vehicle.watchlist_flag):
                snapshot_path = await asyncio.to_thread(_save_snapshot, frame, camera_code)
                det_row.snapshot_path = snapshot_path  # type: ignore[assignment]
                await _safe_commit(db, camera_code, reapply=lambda _det_row=det_row, _path=snapshot_path: (
                    db.add(_det_row), setattr(_det_row, "snapshot_path", _path),
                ))
            alerts = await evaluate(db, camera, det_row, w, h, vehicle)
            for alert in alerts:
                # not a direct Incident.alert_id query: an alert correlated into
                # an existing incident is linked via IncidentAlert
                incident = find_incident_for_alert(db, str(alert.id))
                event_type = "watchlist_match" if bool(alert.vehicle_id) else "zone_entry"

                # rules_engine only attaches a snapshot if the detection already
                # had one (ANPR/watchlist path). A plain zone_entry alert needs
                # one from this same frame. Existing snapshot_path skips this.
                if not alert.snapshot_path:
                    evidence_snapshot_path = await asyncio.to_thread(_save_snapshot, frame, f"{camera_code}_{alert.id}")
                    # alert and det_row are both committed by now, so rollback
                    # would expire these changes. reapply uses the locals.
                    det_snapshot_target = det_row.snapshot_path or evidence_snapshot_path
                    alert.snapshot_path = evidence_snapshot_path  # type: ignore[assignment]
                    det_row.snapshot_path = det_snapshot_target  # type: ignore[assignment]
                    evidence_row = models.Evidence(
                        incident_id=incident.id if incident else None,
                        evidence_type="snapshot",
                        camera_id=camera_id,
                        file_path=evidence_snapshot_path,
                        # hash it now while it's exactly as captured, a later
                        # hash proves nothing
                        sha256=await asyncio.to_thread(sha256_file, evidence_snapshot_path),
                        alert_id=alert.id,
                        detection_id=det_row.id,
                        event_type=event_type,
                        source_timestamp=frame_source_ts,
                        verification_status="unverified",
                        # versions active at capture, never updated afterwards
                        model_version=settings.model_version,
                        rule_version=settings.rule_version,
                    )
                    db.add(evidence_row)

                    def _reapply_evidence_commit(
                        _alert=alert, _det_row=det_row, _evidence_row=evidence_row,
                        _alert_snapshot=evidence_snapshot_path, _det_snapshot=det_snapshot_target,
                    ):
                        db.add(_alert)  # no-op if already attached
                        db.add(_det_row)
                        db.add(_evidence_row)
                        _alert.snapshot_path = _alert_snapshot
                        _det_row.snapshot_path = _det_snapshot

                    await _safe_commit(db, camera_code, reapply=_reapply_evidence_commit)

                # Registered so shutdown drains it. It waits up to
                # clip_post_event_seconds before writing Evidence, and an
                # untracked task got killed by the closing loop.
                background.spawn(
                    clips.build_event_clip(
                        camera_id, camera_code, str(alert.id), str(det_row.id),
                        str(incident.id) if incident else None, event_type, frame_source_ts,
                    ),
                    name=f"clip:{camera_code}:{alert.id}",
                )
            # batched by the manager (ws.py), otherwise every dashboard gets N
            # cameras x inference rate messages per second
            await manager.publish(EventType.DETECTION_CREATED, {
                "detection_id": str(det_row.id),
                "camera_id": camera_id, "camera_code": camera_code,
                "cls": d["cls"], "confidence": d["confidence"],
                "track_id": (str(d["track_id"]) if d.get("track_id") is not None else None),
                "bbox": d["bbox"],
                "timestamp": det_row.timestamp.isoformat(),
            })
            # Recognized plate is its own event. Only sent when the sighting
            # row was written this frame, so a parked car doesn't re-announce.
            if plate_row is not None and vehicle is not None:
                await manager.publish(EventType.VEHICLE_SIGHTING, {
                    "plate_id": str(plate_row.id),
                    "vehicle_id": str(vehicle.id),
                    "plate_text": str(plate_row.plate_text_normalized or ""),
                    "plate_confidence": float(plate_row.confidence or 0.0),
                    "reads_count": int(plate_row.reads_count or 1),
                    "track_id": plate_row.track_id,
                    "vehicle_class": str(plate_row.vehicle_class or ""),
                    "camera_id": camera_id, "camera_code": camera_code,
                    "watchlist_flag": bool(vehicle.watchlist_flag),
                    "detection_confidence": float(plate_row.detection_confidence or 0.0),
                    "timestamp": (plate_row.last_seen or plate_row.timestamp).isoformat(),
                })
    elif not ai_enabled:
        # AI switched off (maybe mid-session via PATCH): drop old boxes
        # instead of overlaying them forever
        last_detections = []

    return last_detections


# Sources that give frames as fast as asked. Live streams are paced by the
# camera, these get paced by the reader at their nominal fps.
_SELF_PACED_SOURCE_TYPES = ("video_file", "mock_vms")
# no frame and no failure for this long = stalled stream, treated as a failed read
_FRAME_WAIT_TIMEOUT_S = 10.0


def _start_reader(source: CameraSource, camera_code: str, source_type: str, fps: float) -> LatestFrameReader:
    reader = LatestFrameReader(
        source, camera_code, pace_fps=fps if source_type in _SELF_PACED_SOURCE_TYPES else None,
    )
    reader.start()
    return reader


async def _camera_loop(camera_id: str) -> None:
    db: Session = SessionLocal()
    source = None
    reader: LatestFrameReader | None = None
    try:
        camera = db.query(models.Camera).filter(models.Camera.id == camera_id).first()
        if not camera or bool(getattr(camera, "retired", False)):
            return
        # reverse lookup for self-heal logging (db_helpers._self_heal_camera_id),
        # the commit helpers only get camera_code
        _stats(camera_id)["camera_code"] = str(camera.camera_code)
        source = CameraSource(str(camera.source_type), str(camera.source_uri))
        _set_grid_state(camera_id, "CONNECTING")
        opened = await _open_with_timeout(source, camera_id)
        if not opened:
            # retry the initial connect too (RTSP hiccup, webcam busy)
            opened = await _reopen_with_backoff(source, camera, db, reason="initial_connect")
        if not opened:
            offline_error_count = camera.error_count + 1  # type: ignore[operator]
            new_state = "DISCONNECTED"
            camera.status = _DB_STATUS_FOR_GRID_STATE[new_state]  # type: ignore[assignment]
            camera.error_count = offline_error_count  # type: ignore[assignment]
            await _safe_commit(db, str(camera.camera_code), reapply=lambda: (
                setattr(camera, "status", _DB_STATUS_FOR_GRID_STATE[new_state]),
                setattr(camera, "error_count", offline_error_count),
            ))
            # AUTH_ERROR is the more specific diagnosis, don't overwrite it.
            # DB status is "offline" either way.
            if _stats(camera_id)["grid_state"] not in ("AUTH_ERROR",):
                _set_grid_state(camera_id, new_state)
            return
        initial_fps = source.fps() or 15.0
        initial_resolution = source.resolution()
        # grid_state is still CONNECTING/RECONNECTING here. The loop would fix
        # it on the first good read, but that read isn't guaranteed, so set it now.
        new_state = "CONNECTED"
        camera.status = _DB_STATUS_FOR_GRID_STATE[new_state]  # type: ignore[assignment]
        camera.fps = initial_fps  # type: ignore[assignment]
        camera.resolution = initial_resolution  # type: ignore[assignment]
        _set_grid_state(camera_id, new_state)
        await _safe_commit(db, str(camera.camera_code), reapply=lambda: (
            setattr(camera, "status", "online"),
            setattr(camera, "fps", initial_fps),
            setattr(camera, "resolution", initial_resolution),
        ))
        # Cached instead of read off camera.* each iteration: rollback expires
        # every attribute, and a bare read then triggers an implicit reload.
        # These don't change for the session (fps is refreshed on reconnect).
        camera_fps_cached = float(camera.fps)  # type: ignore[arg-type]  # Column() attr, plain float at runtime
        camera_source_type_cached = str(camera.source_type)
        camera_code_cached = str(camera.camera_code)
        # PTS is stream-relative, anchor it to when this session opened.
        # reset on every reconnect
        session_opened_at = datetime.now(timezone.utc)
        last_pos_msec: float | None = None

        st = _stats(camera_id)
        st["started_at"] = session_opened_at.isoformat()
        last_loop_end = time.monotonic()
        # Committing last_frame_at/status every frame was the main source of
        # SQLite write pressure with 2+ cameras. 1s was still too much with 5+,
        # hence 2s. Detections/alerts still commit immediately.
        HEARTBEAT_MIN_INTERVAL_S = 2.0
        last_heartbeat_commit_at = 0.0
        # With expire_on_commit=False (db.py) `camera` never sees another
        # session's commit, so a PATCH to ai_person/ai_vehicle would stay
        # invisible until reconnect. Refreshed on the heartbeat cadence.
        last_ai_refresh_at = 0.0
        last_preview_at = 0.0
        last_ring_at = 0.0

        frame_idx = 0
        consecutive_failures = 0
        last_detections: list[dict[str, Any]] = []
        # the reader thread owns `source` from here (frame_reader.py)
        reader = _start_reader(source, camera_code_cached, camera_source_type_cached, camera_fps_cached)
        last_seq = 0
        last_fail_seq = 0
        st["frames_decoded"] = 0
        st["frames_dropped"] = 0
        st["frame_age_ms"] = None
        while True:
            # The whole iteration is guarded. With only the processing half
            # wrapped, a worker could still die silently: an implicit SELECT
            # from a bare attribute read after commit could hit "database is
            # locked" with 2+ cameras. Root cause fixed in db.py
            # (expire_on_commit=False); this keeps any new call site from
            # bringing it back.
            loop_sleep_s = 1.0
            try:
                t_read0 = time.monotonic()
                if reader is None:
                    # a reconnect raised part-way and left no reader, go
                    # straight back to reconnecting
                    got = FrameResult(frame=None, seq=0, fail_seq=last_fail_seq)
                    consecutive_failures = max(consecutive_failures, settings.read_failures_before_reconnect - 1)
                else:
                    got = await asyncio.to_thread(reader.next_frame, last_seq, last_fail_seq, _FRAME_WAIT_TIMEOUT_S)
                    st["frames_decoded"] = reader.frames_read
                read_ms = (time.monotonic() - t_read0) * 1000
                st["last_read_ms"] = read_ms
                st["read_ms_ema"] = _ema(st["read_ms_ema"], read_ms)
                frame = got.frame
                last_fail_seq = got.fail_seq

                if frame is None:
                    consecutive_failures += 1
                    read_fail_error_count = camera.error_count + 1  # type: ignore[operator]
                    camera.error_count = read_fail_error_count  # type: ignore[assignment]
                    st["read_failures"] += 1
                    if consecutive_failures < settings.read_failures_before_reconnect:
                        new_state = "DEGRADED"
                        status_changed = camera.status != _DB_STATUS_FOR_GRID_STATE[new_state]
                        camera.status = _DB_STATUS_FOR_GRID_STATE[new_state]  # type: ignore[assignment]
                        _set_grid_state(camera_id, new_state)
                        # The status change commits now. A failing stream then
                        # retries every second, and committing each error_count
                        # bump was one write transaction per second per camera
                        # (29 degraded grid cameras, ~29/s); the count rides the
                        # heartbeat cadence instead and lands with the next commit.
                        now_fail = time.monotonic()
                        if status_changed or now_fail - last_heartbeat_commit_at >= HEARTBEAT_MIN_INTERVAL_S:
                            await _safe_commit(db, camera_code_cached, reapply=lambda: (
                                setattr(camera, "status", _DB_STATUS_FOR_GRID_STATE[new_state]),
                                setattr(camera, "error_count", read_fail_error_count),
                            ))
                            last_heartbeat_commit_at = now_fail
                        loop_sleep_s = 1.0
                    else:
                        # Stream really dropped, reconnect with backoff.
                        # The old reader releases its capture itself once its
                        # current read returns; a fresh source object means
                        # nothing here touches a capture another thread may
                        # still be reading.
                        reader.stop()
                        reader = None
                        source = CameraSource(camera_source_type_cached, str(camera.source_uri))
                        reopened = await _reopen_with_backoff(source, camera, db)
                        if not reopened:
                            new_state = "DISCONNECTED"
                            camera.status = _DB_STATUS_FOR_GRID_STATE[new_state]  # type: ignore[assignment]
                            # same AUTH_ERROR rule as the initial connect
                            if _stats(camera_id)["grid_state"] not in ("AUTH_ERROR",):
                                _set_grid_state(camera_id, new_state)
                            await _safe_commit(db, camera_code_cached, reapply=lambda: setattr(camera, "status", _DB_STATUS_FOR_GRID_STATE[new_state]))
                            return  # stop this worker; operator can Restart the camera
                        consecutive_failures = 0
                        session_opened_at = datetime.now(timezone.utc)
                        last_pos_msec = None
                        camera_fps_cached = float(camera.fps)  # type: ignore[arg-type]  # Column() attr, plain float at runtime
                        st["reconnects"] += 1
                        metrics.CAMERA_RECONNECTS.labels(camera_code=camera_code_cached).inc()
                        st["started_at"] = session_opened_at.isoformat()
                        reader = _start_reader(source, camera_code_cached, camera_source_type_cached, camera_fps_cached)
                        last_seq = 0
                        last_fail_seq = 0
                else:
                    consecutive_failures = 0
                    # frames the reader decoded that we never took got
                    # superseded; dropped on purpose to stay current
                    if last_seq:
                        st["frames_dropped"] += max(0, got.seq - last_seq - 1)
                    last_seq = got.seq
                    st["frame_age_ms"] = round(got.age_ms, 1)
                    # CONNECTED = frames flowing, AI off. PROCESSING = frames +
                    # AI on. Throttled refresh so a PATCH from another request
                    # shows up; _process_frame reads the same camera object.
                    now_mono_ai = time.monotonic()
                    if now_mono_ai - last_ai_refresh_at >= HEARTBEAT_MIN_INTERVAL_S:
                        db.refresh(camera, attribute_names=["ai_person", "ai_vehicle", "ai_anpr"])
                        last_ai_refresh_at = now_mono_ai
                    ai_currently_enabled = (bool(camera.ai_person) or bool(camera.ai_vehicle)) and ai_capacity.try_acquire(camera_id)
                    desired_state = "PROCESSING" if ai_currently_enabled else "CONNECTED"
                    if st["grid_state"] != desired_state:
                        _set_grid_state(camera_id, desired_state)
                    h, w = frame.shape[:2]
                    frame_idx += 1
                    st["frames_read"] += 1
                    now_mono = time.monotonic()
                    st["loop_gap_ms_ema"] = _ema(st["loop_gap_ms_ema"], (now_mono - last_loop_end) * 1000)
                    last_loop_end = now_mono
                    st["last_loop_at"] = datetime.now(timezone.utc).isoformat()

                    # Clip ring buffer: raw frames at the preview rate, only
                    # while AI runs (only AI raises the alerts clips are for).
                    if ai_currently_enabled and now_mono - last_ring_at >= _PREVIEW_INTERVAL_S:
                        last_ring_at = now_mono
                        raw_jpeg = await asyncio.to_thread(_encode_jpeg, frame)
                        if raw_jpeg is not None:
                            clips.push_frame(str(camera.id), raw_jpeg)
                    watched = is_watched(camera_id)
                    preview_due = now_mono - last_preview_at >= (
                        _PREVIEW_INTERVAL_S if watched else _IDLE_PREVIEW_INTERVAL_S
                    )
                    if preview_due:
                        last_preview_at = now_mono

                    pos_msec = got.pos_msec
                    frame_source_ts = compute_source_timestamp(camera_source_type_cached, session_opened_at, pos_msec, last_pos_msec)
                    if pos_msec is not None:
                        last_pos_msec = pos_msec

                    last_detections = await _process_frame(db, camera, frame, frame_idx, w, h, frame_source_ts, last_detections)
                    if preview_due:
                        annotated_jpeg = await asyncio.to_thread(_encode_annotated, frame, last_detections)
                        if annotated_jpeg is not None:
                            LATEST_FRAMES[camera_id] = annotated_jpeg
                    # Busiest commit in the pipeline, and the top source of
                    # "database is locked" in real logs. Retried in place so a
                    # transient lock doesn't reach the outer except and flip a
                    # healthy camera to ERROR over a heartbeat write.
                    heartbeat_last_frame_at = datetime.now(timezone.utc)
                    camera.last_frame_at = heartbeat_last_frame_at  # type: ignore[assignment]
                    # No _set_grid_state here: desired_state above already set
                    # CONNECTED or PROCESSING this iteration, both "online".
                    heartbeat_status = _DB_STATUS_FOR_GRID_STATE["CONNECTED"]
                    camera.status = heartbeat_status  # type: ignore[assignment]
                    if now_mono - last_heartbeat_commit_at >= HEARTBEAT_MIN_INTERVAL_S:
                        await _safe_commit(db, camera_code_cached, reapply=lambda: (
                            setattr(camera, "last_frame_at", heartbeat_last_frame_at),
                            setattr(camera, "status", heartbeat_status),
                        ))
                        last_heartbeat_commit_at = now_mono
                    # With AI, no pacing: next_frame blocks until a newer frame
                    # and inference sets the pace. Without AI, only a viewer
                    # needs the preview rate; otherwise idle along.
                    if ai_currently_enabled:
                        loop_sleep_s = 0.0
                    else:
                        period = _PREVIEW_INTERVAL_S if watched else _IDLE_LOOP_S
                        loop_sleep_s = max(0.0, period - (time.monotonic() - now_mono))
            except Exception as exc:
                logger.exception("camera %s: loop iteration failed, continuing", camera_code_cached)
                st["last_error"] = f"{type(exc).__name__}: {exc}"
                st["recovered_errors"] += 1
                # through _set_grid_state so the transition gets checked/recorded
                _set_grid_state(camera_id, "ERROR")  # next successful iteration flips this back
                _error_type, _severity = self_heal.classify_exception(exc)
                background.spawn(
                    self_heal.record_event(
                        component="worker", camera_id=camera_id, error_type=_error_type, severity=_severity,
                        message=f"camera {camera_code_cached}: {exc}", recovery_action="CONTINUE_LOOP",
                        attempt=1, max_attempts=1, status="RECOVERED",
                    ),
                    name=f"self-heal:{camera_code_cached}",
                )  # fire-and-forget, diagnostics must never hold up recovery
                try:
                    db.rollback()
                except Exception:
                    logger.exception("camera %s: rollback after error also failed", camera_code_cached)
                else:
                    # reading camera.error_count after rollback is a fresh
                    # SELECT that can itself hit a lock, so it's guarded too
                    try:
                        error_count_target = camera.error_count + 1  # type: ignore[operator]
                        camera.error_count = error_count_target  # type: ignore[assignment]
                        await _safe_commit(db, camera_code_cached, reapply=lambda: setattr(camera, "error_count", error_count_target))
                    except Exception:
                        logger.exception("camera %s: error-count bump also failed, continuing", camera_code_cached)
                loop_sleep_s = 0.5

            await asyncio.sleep(loop_sleep_s)
    except asyncio.CancelledError:
        pass
    finally:
        if reader is not None:
            # Reader owns `source` and releases it on its own thread. Wait a
            # bit so a normal stop really frees the capture; a read stuck on a
            # dead stream finishes and releases after we return.
            reader.stop()
            await asyncio.to_thread(reader.join, 2.0)
        elif source is not None:
            source.release()
        # Not db.close(): on cancel, a DB call already handed to a thread by
        # to_thread keeps running, and close() raced it with
        # IllegalStateChangeError. From a finally that escaped to the
        # supervisor and marked a healthy camera OFFLINE on a normal stop.
        # close_session waits for the thread and never raises.
        close_session(db)


async def _camera_loop_supervised(camera_id: str) -> None:
    """Last safety net around _camera_loop. If anything still escapes, log it
    with a traceback and mark the camera offline instead of letting the task
    vanish."""
    try:
        await _camera_loop(camera_id)
    except Exception as exc:
        logger.exception("camera %s: _camera_loop exited via an unguarded exception", camera_id)
        st = _stats(camera_id)
        st["last_error"] = "top-level crash — see server log for traceback"
        _error_type, _ = self_heal.classify_exception(exc)
        await self_heal.record_event(
            component="worker", camera_id=camera_id, error_type=_error_type, severity="critical",
            message=f"camera {camera_id}: worker crashed at the top level: {exc}",
            recovery_action="MARK_OFFLINE", attempt=1, max_attempts=1, status="FAILED",
        )
        try:
            new_state = "DISCONNECTED"

            def _mark_offline() -> None:
                # off the event loop: a commit waiting on a locked database
                # here froze every other camera for the busy_timeout
                db: Session = SessionLocal()
                try:
                    camera = db.query(models.Camera).filter(models.Camera.id == camera_id).first()
                    if camera:
                        camera.status = _DB_STATUS_FOR_GRID_STATE[new_state]  # type: ignore[assignment]
                        camera.error_count += 1  # type: ignore[assignment]
                        db.commit()
                finally:
                    db.close()

            await run_db(_mark_offline)
            # the task is dead, nothing else will correct grid_state, it'd
            # sit at PROCESSING forever
            _set_grid_state(camera_id, new_state)
        except Exception:
            logger.exception("camera %s: could not mark offline after top-level crash", camera_id)
    finally:
        # A worker that ends on its own must free its AI slot, or AI stays
        # blocked everywhere (seen live: a camera that failed to open kept the
        # only slot). Skip if a newer worker for this camera already started,
        # a restart cancels us after the new task may hold the slot.
        if RUNNING.get(camera_id) in (None, asyncio.current_task()):
            ai_capacity.release(camera_id)


def start_worker(camera_id: str) -> None:
    existing = RUNNING.get(camera_id)
    if existing and not existing.done():
        return
    RUNNING[camera_id] = asyncio.create_task(_camera_loop_supervised(camera_id))


def stop_worker(camera_id: str) -> "asyncio.Task[None] | None":
    """Cancel the camera's task and release its per-camera resources.

    Returns the cancelled task (None if not running) so shutdown can await it.
    cancel() only requests cancellation; the task's finally (source release)
    runs when it's next scheduled, which may never happen if the loop is torn
    down first, leaving a VideoCapture orphaned."""
    task = RUNNING.pop(camera_id, None)
    if task:
        task.cancel()
    LATEST_FRAMES.pop(camera_id, None)
    release_model(camera_id)  # this camera's YOLO/ByteTrack instance
    ai_capacity.release(camera_id)
    clips.release_camera(camera_id)
    plate_tracker.release_camera(camera_id)
    recorder.request_stop(camera_id, "camera stopped")  # the file is finished and kept
    # the loop's cancel path never touches grid_state, so a stopped camera
    # kept showing CONNECTED/PROCESSING in the grid. Only this call knows the
    # operator asked to stop.
    if camera_id in CAMERA_STATS:
        _set_grid_state(camera_id, "DISCONNECTED")
    return task
