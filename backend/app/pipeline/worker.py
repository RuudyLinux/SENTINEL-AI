"""Per-camera background task: frames -> detection -> ANPR -> persist -> rules
-> broadcast, for any source type (see source.py).

State, reconnect and frame helpers live in camera_state.py, camera_connection.py,
db_helpers.py and frame_utils.py and are re-exported here for existing imports.
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

# Upper bound on how often a frame is JPEG-encoded for preview and clips; MJPEG
# serves at most 10 frames/s and encoding every source frame is expensive.
_PREVIEW_INTERVAL_S = 0.1
# Preview refresh interval when nobody is watching, so snapshots stay recent.
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

# Fields an upsert may change on an existing row. Rollback reverts them to the
# committed values, so a retry reassigns them from values captured beforehand.
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

    Returns (OcrRead, full-frame plate bbox or None, plate crop or None). Only
    the best region is read. If the localized read fails the gate, the whole
    vehicle crop is read as a fallback, since localization can return a partial
    plate (see docs/ANPR_ACCURACY.md).
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
        # The trained detector found no plate; whole-crop OCR is rarely worth its
        # cost here (config.plate_whole_crop_fallback_with_model).
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
    """The OCR half of ANPR, with no database access.

    Runs before the detection is flushed so slow OCR never holds SQLite's write
    lock. Returns None for an empty crop, else the inputs _run_anpr needs.
    """
    x1, y1, x2, y2 = [max(0, int(v)) for v in detection["bbox"]]
    crop = frame[y1:y2, x1:x2]
    if crop.size == 0:
        return None
    raw_track_id = detection.get("track_id")
    if not settings.plate_pipeline_v2 or raw_track_id is None:
        raw, normalized, conf = await asyncio.to_thread(read_plate, crop)
        # Saved here, outside the write transaction, like the OCR.
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

    # A new sighting gets one snapshot, saved before any write. These are the
    # conditions _run_anpr checks: a winning read, consensus in strict mode,
    # and no sighting row yet.
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

    V2 (default, needs a track id): OCR only while the track needs a read, reads
    are voted, one sighting row per (camera, track). Legacy (V2 off, or no track
    id yet): one row per passing frame.
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
    # An uncorroborated read is recorded but goes to pending_review, so a single
    # lucky frame can't pose as a settled identity.
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
    """Inference, ANPR, persistence and alerting for one frame. Returns the
    detections to draw on the preview and pass back in next time."""
    # Column()-style models type attributes as Column[T] for a checker; at
    # runtime they're plain values. The casts are for the checker only.
    camera_id = str(camera.id)
    camera_code = str(camera.camera_code)
    want_person = bool(camera.ai_person)
    want_vehicle = bool(camera.ai_vehicle)
    ai_enabled = want_person or want_vehicle

    # AI off, or no free AI slot (ai_capacity.py): stream without inference.
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
            # Appearance signature (visual similarity only, never identity),
            # computed before the write for the same reason as OCR. A failure
            # leaves it null.
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
            # Flush assigns det_row's id. If it still fails after retries, skip
            # this detection and keep the rest of the frame.
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

            # Tracked vehicles get a Track row whether or not the plate is read;
            # vehicle_id is filled in later. Throttled by should_persist_track.
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

            # One commit per detection keeps each write transaction short.
            # On retry, new objects are re-added and changed fields on existing
            # rows are restored from the values captured here (see db_retry.py).
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

            # Watchlist alerts need a snapshot; taken after the commit so the
            # file write never happens inside the transaction.
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

                # A zone alert without an ANPR/watchlist snapshot gets one from
                # this frame.
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

                # Tracked so shutdown waits for it; it writes Evidence after the
                # post-event window.
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
        # Cached so a read after rollback doesn't trigger an implicit reload.
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
        # Heartbeat writes are throttled; detections and alerts commit at once.
        HEARTBEAT_MIN_INTERVAL_S = 2.0
        last_heartbeat_commit_at = 0.0
        # expire_on_commit is off (db.py), so AI flags changed by another
        # session are refreshed explicitly on the heartbeat cadence.
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
            # Every iteration is guarded so one failure never ends the worker.
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
                        # Commit status changes immediately; error_count bumps
                        # ride the heartbeat cadence to limit write volume.
                        now_fail = time.monotonic()
                        if status_changed or now_fail - last_heartbeat_commit_at >= HEARTBEAT_MIN_INTERVAL_S:
                            await _safe_commit(db, camera_code_cached, reapply=lambda: (
                                setattr(camera, "status", _DB_STATUS_FOR_GRID_STATE[new_state]),
                                setattr(camera, "error_count", read_fail_error_count),
                            ))
                            last_heartbeat_commit_at = now_fail
                        loop_sleep_s = 1.0
                    else:
                        # Stream dropped: reconnect with backoff. The old reader
                        # releases its capture itself, so use a fresh source.
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
                    # CONNECTED = frames, AI off; PROCESSING = frames with AI.
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
                    # Retried in place so a transient lock doesn't flip a
                    # healthy camera to ERROR.
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
                    # With AI, inference sets the pace. Without it, only a viewer
                    # needs the preview rate.
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
            # The reader releases the source on its own thread; wait briefly so
            # a normal stop frees the capture.
            reader.stop()
            await asyncio.to_thread(reader.join, 2.0)
        elif source is not None:
            source.release()
        # close_session waits for any DB call still running in a thread; a bare
        # close() can race it on cancellation.
        close_session(db)


async def _camera_loop_supervised(camera_id: str) -> None:
    """Safety net around _camera_loop: log anything that escapes and mark the
    camera offline."""
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
        # Free the AI slot when the worker ends on its own, unless a newer worker
        # for this camera already holds it.
        if RUNNING.get(camera_id) in (None, asyncio.current_task()):
            ai_capacity.release(camera_id)


def start_worker(camera_id: str) -> None:
    existing = RUNNING.get(camera_id)
    if existing and not existing.done():
        return
    RUNNING[camera_id] = asyncio.create_task(_camera_loop_supervised(camera_id))


def stop_worker(camera_id: str) -> "asyncio.Task[None] | None":
    """Cancel the camera's task and release its resources.

    Returns the cancelled task (or None) so shutdown can await its cleanup.
    """
    task = RUNNING.pop(camera_id, None)
    if task:
        task.cancel()
    LATEST_FRAMES.pop(camera_id, None)
    release_model(camera_id)  # this camera's YOLO/ByteTrack instance
    ai_capacity.release(camera_id)
    clips.release_camera(camera_id)
    plate_tracker.release_camera(camera_id)
    recorder.request_stop(camera_id, "camera stopped")  # the file is finished and kept
    # Only an operator stop resets grid_state; the cancel path doesn't.
    if camera_id in CAMERA_STATS:
        _set_grid_state(camera_id, "DISCONNECTED")
    return task
