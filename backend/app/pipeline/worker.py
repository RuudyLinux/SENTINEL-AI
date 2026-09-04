"""Per-camera background task: read real frames -> real YOLO detection ->
real ANPR -> persist -> evaluate rules -> broadcast. This is the whole
pipeline described in doc §54, running against a webcam or an uploaded
video file rather than a real CCTV/VMS source (see source.py header).
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

from .. import models
from ..db import SessionLocal
from ..config import settings
from ..ws import manager
from .source import CameraSource
from .detector import detect_and_track, release_model
from .anpr import read_plate, passes_anpr_gate
from .appearance import compute_signature
from .correlate import upsert_vehicle_for_plate
from .rules_engine import evaluate
from .timing import compute_source_timestamp
from . import clips

LATEST_FRAMES: dict[str, bytes] = {}
RUNNING: dict[str, "asyncio.Task[None]"] = {}

# Consecutive bad reads before we treat the stream as dropped and attempt a
# real reconnect (release + reopen), rather than reacting to one blip.
READ_FAILURES_BEFORE_RECONNECT = 3

# Phase 4 diagnostics — per-camera runtime counters for the concurrency
# investigation (frame/inference latency, drops, reconnects, last error).
# In-memory, process-local, intentionally lightweight (a temporary
# diagnostic surface per the Phase 4 brief, not a metrics system).
CAMERA_STATS: dict[str, dict[str, Any]] = {}


def _stats(camera_id: str) -> dict[str, Any]:
    return CAMERA_STATS.setdefault(camera_id, {
        "started_at": None,
        "frames_read": 0,
        "frames_processed": 0,
        "read_failures": 0,
        "reconnects": 0,
        "recovered_errors": 0,
        "last_loop_at": None,
        "last_read_ms": None,
        "read_ms_ema": None,
        "last_inference_ms": None,
        "inference_ms_ema": None,
        "loop_gap_ms_ema": None,  # wall-clock time between consecutive loop iterations
        "last_error": None,
        # Richer connection-lifecycle state (final integration task), surfaced
        # via GET /api/cameras/{id}/diagnostics. Deliberately kept separate from
        # Camera.status (DB column, only ever online/offline/degraded — many
        # other call sites already depend on that 3-value contract) rather than
        # migrating it, per "reuse existing, don't redesign."
        "grid_state": "CONNECTING",
    })


# Valid values for CAMERA_STATS[...]["grid_state"].
GRID_STATES = {
    "DISCOVERING", "CONNECTING", "CONNECTED", "PROCESSING", "DEGRADED",
    "RECONNECTING", "DISCONNECTED", "AUTH_ERROR", "ERROR",
}


def _set_grid_state(camera_id: str, state: str) -> None:
    assert state in GRID_STATES, f"unknown grid_state: {state}"
    _stats(camera_id)["grid_state"] = state


def _ema(prev: float | None, sample: float, alpha: float = 0.2) -> float:
    return sample if prev is None else (alpha * sample + (1 - alpha) * prev)


def _safe_commit(db: Session, camera_code: str) -> bool:
    """A db.commit() that can never itself throw and kill the calling task.
    Every commit outside the main per-iteration block (which has its own
    dedicated try/except covering the whole iteration) goes through this
    instead. Returns False on failure (already rolled back) so the caller
    can decide whether to retry."""
    try:
        db.commit()
        return True
    except Exception:
        logger.exception("camera %s: commit failed", camera_code)
        try:
            db.rollback()
        except Exception:
            logger.exception("camera %s: rollback after failed commit also failed", camera_code)
        return False


async def _open_with_timeout(source: "CameraSource", camera_id: str | None = None) -> bool:
    """`source.open()` blocks synchronously (a raw cv2.VideoCapture connect)
    and is offloaded to a worker thread via asyncio.to_thread — but
    CAP_PROP_OPEN_TIMEOUT_MSEC (source.py) is not reliably honored by every
    OpenCV/FFmpeg build (confirmed on this build: an unreachable RTSP
    endpoint hung ~30s despite a configured 5s). This enforces our own
    timeout at the asyncio level so a dead source can't tie up a reconnect
    attempt indefinitely — the abandoned thread still runs until cv2's own
    internal timeout eventually fires, but the camera loop itself moves on
    and can keep retrying with backoff instead of blocking on it."""
    try:
        ok = await asyncio.wait_for(asyncio.to_thread(source.open), timeout=settings.source_open_timeout_seconds)
        if camera_id:
            _set_grid_state(camera_id, "CONNECTED" if ok else "DISCONNECTED")
        return ok
    except asyncio.TimeoutError:
        if camera_id:
            _set_grid_state(camera_id, "DISCONNECTED")
        return False
    except Exception as exc:
        # An adapter can now fail loudly by design (e.g. the ONVIF stub, or
        # SentinelGridAdapter when credentials aren't configured — see
        # pipeline/adapters.py) instead of silently returning False. That must
        # still fail this camera safely (offline, logged) rather than crash the
        # worker/task with an unguarded exception.
        logger.exception("camera source failed to open")
        if camera_id:
            _set_grid_state(camera_id, "AUTH_ERROR" if "credentials not configured" in str(exc) else "ERROR")
        return False


async def _reopen_with_backoff(source: "CameraSource", camera: models.Camera, db: Session) -> bool:
    """Attempts to release+reopen a dropped source with exponential backoff.
    Returns True once reopened, False after exhausting the retry budget
    (caller marks the camera offline and stops the worker)."""
    _set_grid_state(str(camera.id), "RECONNECTING")
    for attempt in range(1, settings.reconnect_max_attempts + 1):
        # These are legacy Column()-style declarative model attributes
        # (models.py) — Pylance sees them as Column[T], not T, so a plain
        # T assignment shows as a false-positive type error; at runtime an
        # ORM instance attribute is always the plain value, matching every
        # other read/write of `camera.*` throughout this module.
        camera.status = "degraded"  # type: ignore[assignment]
        camera.error_count += 1  # type: ignore[assignment]
        _safe_commit(db, str(camera.camera_code))
        delay = min(settings.reconnect_backoff_max, settings.reconnect_backoff_base * (2 ** (attempt - 1)))
        await asyncio.sleep(delay)
        await asyncio.to_thread(source.release)
        opened = await _open_with_timeout(source, str(camera.id))
        if opened:
            ok, _ = await asyncio.to_thread(source.read)
            if ok:
                camera.status = "online"  # type: ignore[assignment]
                camera.fps = source.fps() or camera.fps or 15.0  # type: ignore[assignment]
                camera.resolution = source.resolution() or camera.resolution  # type: ignore[assignment]
                _safe_commit(db, str(camera.camera_code))
                return True
    return False


def _draw_boxes(frame: np.ndarray, detections: list[dict[str, Any]]) -> np.ndarray:
    for d in detections:
        x1, y1, x2, y2 = [int(v) for v in d["bbox"]]
        color = (0, 255, 0) if d["cls"] == "person" else (0, 165, 255)
        cv2.rectangle(frame, (x1, y1), (x2, y2), color, 2)
        label = f'{d["cls"]} {d["confidence"]:.2f}'
        cv2.putText(frame, label, (x1, max(0, y1 - 6)), cv2.FONT_HERSHEY_SIMPLEX, 0.5, color, 1, cv2.LINE_AA)
    return frame


def _save_snapshot(frame: np.ndarray, prefix: str) -> str:
    fname = f"{prefix}_{datetime.now(timezone.utc).strftime('%Y%m%d%H%M%S%f')}.jpg"
    path = settings.evidence_dir / fname
    cv2.imwrite(str(path), frame)
    return str(path)


async def _process_frame(
    db: Session, camera: models.Camera, frame: np.ndarray, frame_idx: int, w: int, h: int,
    frame_source_ts: datetime | None, last_detections: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Runs inference (throttled) + ANPR + persistence + alerting for one
    already-successfully-read frame, and refreshes the annotated MJPEG
    snapshot. Returns the (possibly updated) `last_detections` list the
    caller should pass back in next iteration — the annotated overlay is
    redrawn every frame, but inference only runs every `detect_every_n_frames`."""
    # SQLAlchemy's legacy Column()-style declarative model (models.py) types
    # every attribute as Column[T] rather than T for a static checker; at
    # runtime, an attribute read on an instance always returns the plain
    # value. These casts are honest about that — no behavior change, just
    # giving the functions below the plain str/bool they already receive.
    camera_id = str(camera.id)
    camera_code = str(camera.camera_code)
    want_person = bool(camera.ai_person)
    want_vehicle = bool(camera.ai_vehicle)
    ai_enabled = want_person or want_vehicle

    # A camera can be connected (real frames flowing, e.g. via the 24/7
    # auto-connect supervisor) with AI fully off — that must cost nothing
    # beyond decode/MJPEG. `detect_and_track` itself no-ops on empty
    # class_ids, but reaching it still calls `detector.get_model()`, which
    # loads/caches a real per-camera YOLO instance — real memory/init cost
    # even when it would detect nothing. Skipping the call entirely when
    # neither class is wanted is what makes "connected, AI off" genuinely
    # lightweight rather than just "inference skipped."
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
        last_detections = detections

        for d in detections:
            snapshot_path = None
            det_row = models.Detection(
                camera_id=camera_id, cls=d["cls"], confidence=d["confidence"],
                bbox=d["bbox"], track_id=(str(d["track_id"]) if d["track_id"] is not None else None),
                model_version=settings.model_version,
                source_timestamp=frame_source_ts,
            )
            db.add(det_row)
            db.flush()

            # Cross-camera person appearance signature (Phase 5) — visual-similarity
            # only, never biometric/identity. A failure here must never break
            # detection persistence; leaves the field null, never fabricated.
            if d["cls"] == "person":
                try:
                    px1, py1, px2, py2 = [max(0, int(v)) for v in d["bbox"]]
                    person_crop = frame[py1:py2, px1:px2]
                    det_row.appearance_signature = await asyncio.to_thread(compute_signature, person_crop)  # type: ignore[assignment]
                except Exception:
                    logger.exception("camera %s: appearance signature failed for detection %s", camera_code, det_row.id)

            vehicle = None
            if bool(camera.ai_anpr) and d["cls"] in ("car", "truck", "bus", "motorbike"):
                x1, y1, x2, y2 = [max(0, int(v)) for v in d["bbox"]]
                crop = frame[y1:y2, x1:x2]
                raw, normalized, conf = await asyncio.to_thread(read_plate, crop)
                # Quality gate (P0-C): a single noisy OCR frame is not
                # trusted — only plausible-format, sufficiently-confident
                # reads become a Vehicle/Plate correlation record.
                if passes_anpr_gate(normalized, conf):
                    snapshot_path = await asyncio.to_thread(_save_snapshot, frame, camera_code)
                    det_row.snapshot_path = snapshot_path  # type: ignore[assignment]
                    vehicle = upsert_vehicle_for_plate(db, normalized, conf)
                    db.add(models.Plate(
                        vehicle_id=vehicle.id, camera_id=camera_id, detection_id=det_row.id,
                        plate_text_raw=raw, plate_text_normalized=normalized,
                        confidence=conf, snapshot_path=snapshot_path,
                        source_timestamp=frame_source_ts,
                    ))

            if not snapshot_path and vehicle is not None and bool(vehicle.watchlist_flag):
                snapshot_path = await asyncio.to_thread(_save_snapshot, frame, camera_code)
                det_row.snapshot_path = snapshot_path  # type: ignore[assignment]

            # Tried batching this into one commit per frame instead of per
            # detection (to shrink the WAL growth behind the read-latency
            # finding below) — reverted: with the transaction held open
            # across a whole frame's OCR/snapshot awaits instead of just one
            # detection's, live 2-camera testing produced actual "database
            # is locked" errors that hadn't been observed before. Per-
            # detection commit keeps each write transaction's held-open
            # window short and is the version validated by Phase 4's
            # concurrency hardening — not touched further. The real fix for
            # the latency finding is db.py's wal_autocheckpoint tuning
            # (checkpoint more often, in smaller increments, instead of
            # letting the WAL grow large between checkpoints).
            db.commit()
            alerts = await evaluate(db, camera, det_row, w, h, vehicle)
            for alert in alerts:
                incident = db.query(models.Incident).filter(models.Incident.alert_id == alert.id).first()
                event_type = "watchlist_match" if bool(alert.vehicle_id) else "zone_entry"

                # Evidence backfill: rules_engine.py only attaches a snapshot at
                # alert-creation time when the triggering detection already had one
                # (the ANPR/watchlist path sets it earlier in this function) — a
                # bare zone_entry alert (no plate match) previously got NO
                # snapshot/Evidence at all. This captures one here, from the SAME
                # real frame this alert fired on, for any alert that doesn't
                # already have one — covers zone_entry uniformly without touching
                # the existing ANPR/watchlist behavior (already-set snapshot_path
                # short-circuits this, so nothing changes for that path).
                if not alert.snapshot_path:
                    evidence_snapshot_path = await asyncio.to_thread(_save_snapshot, frame, f"{camera_code}_{alert.id}")
                    alert.snapshot_path = evidence_snapshot_path  # type: ignore[assignment]
                    if not det_row.snapshot_path:
                        det_row.snapshot_path = evidence_snapshot_path  # type: ignore[assignment]
                    db.add(models.Evidence(
                        incident_id=incident.id if incident else None,
                        evidence_type="snapshot",
                        camera_id=camera_id,
                        file_path=evidence_snapshot_path,
                        alert_id=alert.id,
                        detection_id=det_row.id,
                        event_type=event_type,
                        source_timestamp=frame_source_ts,
                        verification_status="unverified",
                    ))
                    db.commit()

                asyncio.create_task(clips.build_event_clip(
                    camera_id, camera_code, str(alert.id), str(det_row.id),
                    str(incident.id) if incident else None, event_type, frame_source_ts,
                ))
            await manager.broadcast("detection", {
                "camera_id": camera_id, "camera_code": camera_code,
                "cls": d["cls"], "confidence": d["confidence"],
                "timestamp": det_row.timestamp.isoformat(),
            })
    elif not ai_enabled:
        # AI was toggled off (possibly mid-session, via PATCH) — drop any
        # boxes from when it was last on rather than overlaying stale ones
        # on an otherwise-live connect-only feed indefinitely.
        last_detections = []

    annotated = _draw_boxes(frame.copy(), last_detections)
    ok2, buf = cv2.imencode(".jpg", annotated, [int(cv2.IMWRITE_JPEG_QUALITY), 70])
    if ok2:
        LATEST_FRAMES[camera_id] = buf.tobytes()

    return last_detections


async def _camera_loop(camera_id: str) -> None:
    db: Session = SessionLocal()
    source = None
    try:
        camera = db.query(models.Camera).filter(models.Camera.id == camera_id).first()
        if not camera:
            return
        source = CameraSource(str(camera.source_type), str(camera.source_uri))
        _set_grid_state(camera_id, "CONNECTING")
        opened = await _open_with_timeout(source, camera_id)
        if not opened:
            # Real reconnect attempt on initial failure too (transient RTSP/
            # webcam-busy failures), not just an immediate give-up.
            opened = await _reopen_with_backoff(source, camera, db)
        if not opened:
            camera.status = "offline"  # type: ignore[assignment]  # legacy Column() declarative model — plain-value assignment is correct at runtime
            camera.error_count += 1  # type: ignore[assignment]
            _safe_commit(db, str(camera.camera_code))
            if _stats(camera_id)["grid_state"] not in ("AUTH_ERROR",):
                _set_grid_state(camera_id, "DISCONNECTED")
            return
        camera.status = "online"  # type: ignore[assignment]
        camera.fps = source.fps() or 15.0  # type: ignore[assignment]
        camera.resolution = source.resolution()  # type: ignore[assignment]
        _safe_commit(db, str(camera.camera_code))
        # Cached rather than re-read from `camera.*` every loop iteration:
        # a rollback (unconditionally, regardless of expire_on_commit)
        # expires every attribute on the object, so a later bare read can
        # trigger an implicit reload — these call sites removed from that
        # whole risky category, on values static for the camera's lifetime
        # (or, for fps, between reconnects — refreshed there too).
        camera_fps_cached = float(camera.fps)  # type: ignore[arg-type]  # legacy Column() declarative attribute — plain float at runtime
        camera_source_type_cached = str(camera.source_type)
        camera_code_cached = str(camera.camera_code)
        # SOURCE PTS is stream-relative — anchor it to the wall-clock time
        # this capture session was opened. Reset on every reconnect below,
        # since position resets relative to the new session too.
        session_opened_at = datetime.now(timezone.utc)
        last_pos_msec: float | None = None

        st = _stats(camera_id)
        st["started_at"] = session_opened_at.isoformat()
        last_loop_end = time.monotonic()
        # Phase 4 perf finding: committing `last_frame_at`/`status` on every
        # single frame (up to ~camera.fps times/sec) was the dominant source
        # of SQLite write pressure once a second camera ran concurrently —
        # far more frequent than actually needed for a liveness heartbeat.
        # Throttled to at most once/second; real detection/alert data still
        # commits immediately wherever it's written (unaffected, see
        # _process_frame).
        HEARTBEAT_MIN_INTERVAL_S = 1.0
        last_heartbeat_commit_at = 0.0
        # `camera` is loaded once per connection (below) and, with
        # expire_on_commit=False (db.py), never picks up another session's
        # commit on its own — a PATCH to ai_person/ai_vehicle from the API
        # would otherwise sit invisible to this already-running loop until a
        # full reconnect. Refreshed at the same throttle cadence as the
        # heartbeat commit (see below) so Start AI/Stop AI take effect within
        # about a second, not never.
        last_ai_refresh_at = 0.0

        frame_idx = 0
        consecutive_failures = 0
        last_detections: list[dict[str, Any]] = []
        while True:
            # Phase 4 hardening: the ENTIRE iteration — read, failure/
            # reconnect handling, and frame processing — is now one guarded
            # region. An earlier, narrower version of this guard (wrapping
            # only the processing half) still let a camera's worker task die
            # silently: SQLAlchemy's default expire_on_commit=True means any
            # bare attribute read on `camera` after a commit can trigger an
            # implicit, unguarded SELECT, and under 2+ concurrent cameras
            # writing to the same SQLite file that SELECT can itself hit
            # "database is locked" — from a call site with no db.commit()
            # nearby and therefore easy to miss. That's fixed at the root in
            # db.py (expire_on_commit=False), and this wraps the rest so no
            # future call site can reintroduce the same failure mode.
            loop_sleep_s = 1.0
            try:
                t_read0 = time.monotonic()
                ok, frame = await asyncio.to_thread(source.read)
                read_ms = (time.monotonic() - t_read0) * 1000
                st["last_read_ms"] = read_ms
                st["read_ms_ema"] = _ema(st["read_ms_ema"], read_ms)

                if not ok or frame is None:
                    consecutive_failures += 1
                    camera.error_count += 1  # type: ignore[assignment]
                    st["read_failures"] += 1
                    if consecutive_failures < READ_FAILURES_BEFORE_RECONNECT:
                        camera.status = "degraded"  # type: ignore[assignment]
                        _set_grid_state(camera_id, "DEGRADED")
                        _safe_commit(db, camera_code_cached)
                        loop_sleep_s = 1.0
                    else:
                        # Stream is actually dropped: attempt a real
                        # reconnect with backoff rather than looping
                        # "degraded" forever.
                        reopened = await _reopen_with_backoff(source, camera, db)
                        if not reopened:
                            camera.status = "offline"  # type: ignore[assignment]
                            if _stats(camera_id)["grid_state"] not in ("AUTH_ERROR",):
                                _set_grid_state(camera_id, "DISCONNECTED")
                            _safe_commit(db, camera_code_cached)
                            return  # stop this worker; operator can Restart the camera
                        consecutive_failures = 0
                        session_opened_at = datetime.now(timezone.utc)
                        last_pos_msec = None
                        camera_fps_cached = float(camera.fps)  # type: ignore[arg-type]  # legacy Column() declarative attribute — plain float at runtime
                        st["reconnects"] += 1
                        st["started_at"] = session_opened_at.isoformat()
                else:
                    consecutive_failures = 0
                    # CONNECTED = real frames flowing, AI off (e.g. the 24/7
                    # auto-connect supervisor's default state). PROCESSING =
                    # frames flowing AND AI actually enabled for this camera.
                    # Throttled refresh (not every frame) so ai_person/
                    # ai_vehicle reflect a PATCH from another request instead
                    # of this session's stale in-memory copy — _process_frame
                    # reads the same `camera` object right below, so this
                    # covers both call sites.
                    now_mono_ai = time.monotonic()
                    if now_mono_ai - last_ai_refresh_at >= HEARTBEAT_MIN_INTERVAL_S:
                        db.refresh(camera, attribute_names=["ai_person", "ai_vehicle", "ai_anpr"])
                        last_ai_refresh_at = now_mono_ai
                    ai_currently_enabled = bool(camera.ai_person) or bool(camera.ai_vehicle)
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

                    # Bounded event-clip ring buffer — every frame, raw
                    # (unannotated), independent of the AI-inference
                    # throttle below so clips stay smooth.
                    ok_raw, raw_buf = cv2.imencode(".jpg", frame, [int(cv2.IMWRITE_JPEG_QUALITY), 70])
                    if ok_raw:
                        clips.push_frame(str(camera.id), raw_buf.tobytes())

                    pos_msec = await asyncio.to_thread(source.pos_msec)
                    frame_source_ts = compute_source_timestamp(camera_source_type_cached, session_opened_at, pos_msec, last_pos_msec)
                    if pos_msec is not None:
                        last_pos_msec = pos_msec

                    last_detections = await _process_frame(db, camera, frame, frame_idx, w, h, frame_source_ts, last_detections)
                    camera.last_frame_at = datetime.now(timezone.utc)  # type: ignore[assignment]
                    camera.status = "online"  # type: ignore[assignment]
                    if now_mono - last_heartbeat_commit_at >= HEARTBEAT_MIN_INTERVAL_S:
                        db.commit()
                        last_heartbeat_commit_at = now_mono
                    loop_sleep_s = max(0.01, 1.0 / max(camera_fps_cached, 1.0))
            except Exception as exc:
                logger.exception("camera %s: loop iteration failed, continuing", camera_code_cached)
                st["last_error"] = f"{type(exc).__name__}: {exc}"
                st["recovered_errors"] += 1
                st["grid_state"] = "ERROR"  # next successful iteration flips this back to CONNECTED/PROCESSING
                try:
                    db.rollback()
                except Exception:
                    logger.exception("camera %s: rollback after error also failed", camera_code_cached)
                try:
                    camera.error_count += 1  # type: ignore[assignment]
                    db.commit()
                except Exception:
                    logger.exception("camera %s: error-count commit also failed", camera_code_cached)
                    try:
                        db.rollback()
                    except Exception:
                        pass
                loop_sleep_s = 0.5

            await asyncio.sleep(loop_sleep_s)
    except asyncio.CancelledError:
        pass
    finally:
        if source is not None:
            source.release()
        db.close()


async def _camera_loop_supervised(camera_id: str) -> None:
    """Final safety net around _camera_loop. Everything inside the loop is
    now guarded (see the per-iteration try/except above), but this exists
    so that if some exception nonetheless escapes — a bug in the guard
    itself, or in code outside the loop — it is impossible for a camera's
    task to disappear without a trace: it's logged with a full traceback
    (proving definitively where it came from, rather than us guessing) and
    the camera is marked offline and observable instead of silently dead."""
    try:
        await _camera_loop(camera_id)
    except Exception:
        logger.exception("camera %s: _camera_loop exited via an unguarded exception", camera_id)
        st = _stats(camera_id)
        st["last_error"] = "top-level crash — see server log for traceback"
        try:
            db: Session = SessionLocal()
            try:
                camera = db.query(models.Camera).filter(models.Camera.id == camera_id).first()
                if camera:
                    camera.status = "offline"  # type: ignore[assignment]
                    camera.error_count += 1  # type: ignore[assignment]
                    db.commit()
            finally:
                db.close()
        except Exception:
            logger.exception("camera %s: could not mark offline after top-level crash", camera_id)


def start_worker(camera_id: str) -> None:
    existing = RUNNING.get(camera_id)
    if existing and not existing.done():
        return
    RUNNING[camera_id] = asyncio.create_task(_camera_loop_supervised(camera_id))


def stop_worker(camera_id: str) -> None:
    task = RUNNING.pop(camera_id, None)
    if task:
        task.cancel()
    LATEST_FRAMES.pop(camera_id, None)
    release_model(camera_id)  # drop this camera's YOLO/ByteTrack instance
    clips.release_camera(camera_id)  # drop this camera's event-clip ring buffer
    # Real bug found via the live browser test of the Disconnect button:
    # _camera_loop's own cancellation path (`except asyncio.CancelledError:
    # pass`) never updates grid_state, so a deliberately stopped camera kept
    # showing its last live value (e.g. CONNECTED/PROCESSING) forever in the
    # Camera Grid — indistinguishable from still being connected. Task
    # cancellation is asynchronous either way (the loop notices and unwinds
    # on its own schedule), so this is set here, at the one place that
    # actually knows the operator asked to stop.
    if camera_id in CAMERA_STATS:
        _set_grid_state(camera_id, "DISCONNECTED")
