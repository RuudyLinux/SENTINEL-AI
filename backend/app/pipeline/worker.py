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

from .. import models, metrics, background
from ..db import SessionLocal
from ..config import settings
from ..ws import manager, EventType
from .source import CameraSource
from .detector import detect_and_track, release_model
from .anpr import (
    read_plate, passes_anpr_gate, review_status_for, better_read,
    read_plate_structured, passes_read_gate, looks_like_plate,
)
from .appearance import compute_signature
from .correlate import upsert_vehicle_for_plate, upsert_plate_sighting, upsert_track
from . import plate_detect, plate_detector, plate_preprocess, plate_tracker
from .rules_engine import evaluate, find_incident_for_alert
from .timing import compute_source_timestamp
from .db_retry import safe_commit, safe_flush, close_session
from ..evidence_hash import sha256_file
from ..self_heal import engine as self_heal
from . import clips

LATEST_FRAMES: dict[str, bytes] = {}
RUNNING: dict[str, "asyncio.Task[None]"] = {}

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
        #
        # None, not "CONNECTING": this dict is created by `setdefault` the
        # first time ANYTHING asks about a camera, which is not the same
        # moment a worker starts one. A prior version primed this to
        # "CONNECTING", which meant a camera's first REAL transition was
        # checked as if it were "CONNECTING -> whatever" once transition
        # legality started being checked — every fresh camera looked like an
        # illegal transition on its very first move. None means "no lifecycle
        # observed yet", and _set_grid_state treats a None previous state as
        # unconditionally legal, which is the only correct rule for a state
        # that was never really entered.
        "grid_state": None,
    })


# Valid values for CAMERA_STATS[...]["grid_state"].
GRID_STATES = {
    "DISCOVERING", "CONNECTING", "CONNECTED", "PROCESSING", "DEGRADED",
    "RECONNECTING", "DISCONNECTED", "AUTH_ERROR", "ERROR",
}

# Which transitions this lifecycle actually makes. The previous guard checked
# that the NEW state was a known name, which is the weaker half of the
# question — "PROCESSING" is a valid name and a nonsense destination from
# DISCONNECTED, and nothing said so. It was also an `assert`, and asserts are
# stripped under `python -O`, so the one check there was could vanish in an
# optimised run.
#
# Self-transitions are listed because the loop re-asserts its state on most
# iterations; leaving them out would make the common case the noisy one.
# Every state can reach DISCONNECTED (an operator can stop a camera at any
# point) and the two failure states (a source can fail at any point), so those
# are added to every row rather than repeated by hand.
_ALWAYS_REACHABLE = {"DISCONNECTED", "AUTH_ERROR", "ERROR"}
_TRANSITIONS: dict[str, set[str]] = {
    "DISCOVERING": {"DISCOVERING", "CONNECTING"},
    "CONNECTING": {"CONNECTING", "CONNECTED", "PROCESSING", "RECONNECTING"},
    "CONNECTED": {"CONNECTED", "PROCESSING", "DEGRADED", "RECONNECTING"},
    "PROCESSING": {"PROCESSING", "CONNECTED", "DEGRADED", "RECONNECTING"},
    "DEGRADED": {"DEGRADED", "CONNECTED", "PROCESSING", "RECONNECTING"},
    "RECONNECTING": {"RECONNECTING", "CONNECTED", "PROCESSING", "DEGRADED"},
    # A stopped or failed camera comes back only by being started again.
    "DISCONNECTED": {"DISCONNECTED", "DISCOVERING", "CONNECTING"},
    "AUTH_ERROR": {"AUTH_ERROR", "DISCOVERING", "CONNECTING"},
    # ERROR is set by the loop's catch-all; the comment at that call site says
    # the next successful iteration flips it straight back, so it reaches the
    # running states directly rather than via CONNECTING.
    "ERROR": {"ERROR", "DISCOVERING", "CONNECTING", "RECONNECTING",
              "CONNECTED", "PROCESSING", "DEGRADED"},
}
for _from, _to in _TRANSITIONS.items():
    _to |= _ALWAYS_REACHABLE

#: Illegal transitions observed at runtime, keyed by (from, to). Read by the
#: diagnostics endpoint and by tests. A count here is a bug report about this
#: table or about the lifecycle, and it is deliberately a COUNT rather than an
#: exception — see _set_grid_state.
ILLEGAL_TRANSITIONS: dict[tuple[str, str], int] = {}


def _set_grid_state(camera_id: str, state: str) -> None:
    """Move a camera to `state`, recording the move if it is not a legal one.

    An illegal transition is applied, not refused. Refusing would leave
    CAMERA_STATS asserting something the camera is no longer doing, which is
    the exact class of bug this lifecycle exists to remove — and raising here
    would kill a live camera worker over a gap in the table above. So the
    transition happens, the violation is counted where a test and the
    diagnostics endpoint can both see it, and the log line names the pair so
    it can be fixed. `test_camera_state_machine.py` asserts the counter is
    empty after driving the real lifecycle, which is what turns this from a
    log nobody reads into a failing build.
    """
    if state not in GRID_STATES:
        raise ValueError(f"unknown grid_state: {state}")
    stats = _stats(camera_id)
    previous = stats.get("grid_state")
    if previous is not None and state not in _TRANSITIONS.get(previous, set()):
        ILLEGAL_TRANSITIONS[(previous, state)] = ILLEGAL_TRANSITIONS.get((previous, state), 0) + 1
        logger.warning(
            "camera %s: illegal state transition %s -> %s (applied anyway)",
            camera_id, previous, state,
        )
    stats["grid_state"] = state


def _ema(prev: float | None, sample: float, alpha: float = 0.2) -> float:
    return sample if prev is None else (alpha * sample + (1 - alpha) * prev)


def _self_heal_camera_id(camera_code: str) -> str | None:
    # CAMERA_STATS is keyed by camera.id (not camera_code) — cheap reverse
    # lookup only used for the self-heal event's camera_id field, purely
    # informational (never on any hot path: only called when a lock was
    # actually hit, i.e. already the rare/slow path).
    for cid, stats in CAMERA_STATS.items():
        if stats.get("camera_code") == camera_code:
            return cid
    return None


def _db_self_heal_on_result(camera_code: str, op_name: str):
    """Builds the `on_result` hook passed to safe_commit/safe_flush —
    records a Self-Heal event ONLY when a lock actually happened (the
    overwhelming common case is a clean first-try write, which would be
    pure noise to log every time). See self_heal/engine.py's module
    docstring for why this observes rather than re-implements db_retry.py's
    real retry logic.

    Final-review audit finding: this used to be declared `async def` purely
    to build and return a plain closure (it performs no `await` itself),
    forcing an unnecessary coroutine creation + await on EVERY commit/flush
    across every running camera — a real hot path this same PR's own
    concurrency work targets. Now a plain sync function; the returned
    closure itself is still `async def` (it genuinely awaits
    self_heal.record_event) and is `await`ed normally by db_retry.py."""
    async def _on_result(attempt: int, max_attempts: int, success: bool, was_lock: bool, duration_s: float):
        if not was_lock:
            return
        await self_heal.record_event(
            component="database", camera_id=_self_heal_camera_id(camera_code),
            error_type="SQLITE_LOCK", severity="warning" if success else "critical",
            message=f"{op_name} hit a locked database for camera {camera_code}",
            recovery_action="ROLLBACK_RETRY", attempt=attempt, max_attempts=max_attempts,
            status="RECOVERED" if success else "FAILED", duration_seconds=duration_s,
        )
    return _on_result


async def _safe_commit(db: Session, camera_code: str, reapply=None) -> bool:
    """Thin camera-labeled wrapper around db_retry.safe_commit — see that
    module for the full rationale (retry-with-reapply on a transient SQLite
    lock, verified empirically; no retry without `reapply`, to avoid a
    retry-with-nothing-pending silently reporting success on a lost write)."""
    return await safe_commit(db, f"camera {camera_code}", reapply=reapply, on_result=_db_self_heal_on_result(camera_code, "commit"))


async def _safe_flush(db: Session, camera_code: str, reapply=None) -> bool:
    """Same as _safe_commit above, for db.flush() — see db_retry.safe_flush."""
    return await safe_flush(db, f"camera {camera_code}", reapply=reapply, on_result=_db_self_heal_on_result(camera_code, "flush"))


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


async def _reopen_with_backoff(source: "CameraSource", camera: models.Camera, db: Session, reason: str = "stream_read_failure") -> bool:
    """Attempts to release+reopen a dropped source with exponential backoff.
    Returns True once reopened, False after exhausting the retry budget
    (caller marks the camera offline and stops the worker).

    `reason` is honesty-only labeling for the Self-Heal event this records —
    "initial_connect" (never opened this session) vs "stream_read_failure"
    (was flowing, then N consecutive bad reads — which folds in whatever a
    real dead RTSP/H264 stream looks like to cv2/FFmpeg: cv2 exposes no
    structured decode-error signal, only read() returning False, so this is
    never labeled as a fake "H264 decoder" diagnosis)."""
    camera_id = str(camera.id)
    camera_code = str(camera.camera_code)
    error_type = "CAMERA_CONNECT_FAILURE" if reason == "initial_connect" else "STREAM_READ_FAILURE"
    _set_grid_state(camera_id, "RECONNECTING")
    reconnect_started = time.monotonic()
    max_attempts = settings.reconnect_max_attempts
    for attempt in range(1, max_attempts + 1):
        # These are legacy Column()-style declarative model attributes
        # (models.py) — Pylance sees them as Column[T], not T, so a plain
        # T assignment shows as a false-positive type error; at runtime an
        # ORM instance attribute is always the plain value, matching every
        # other read/write of `camera.*` throughout this module.
        # Target values captured into locals BEFORE assignment/commit —
        # `reapply` below must reassign FROM these, never from re-reading
        # `camera.*` after a rollback, since rollback expires a persistent
        # object's mutated attributes back to their last-committed DB value
        # (verified empirically; see db_retry.py's module docstring).
        degraded_error_count = camera.error_count + 1  # type: ignore[operator]
        camera.status = "degraded"  # type: ignore[assignment]
        camera.error_count = degraded_error_count  # type: ignore[assignment]
        await _safe_commit(db, str(camera.camera_code), reapply=lambda: (
            setattr(camera, "status", "degraded"),
            setattr(camera, "error_count", degraded_error_count),
        ))
        delay = min(settings.reconnect_backoff_max, settings.reconnect_backoff_base * (2 ** (attempt - 1)))
        await asyncio.sleep(delay)
        await asyncio.to_thread(source.release)
        opened = await _open_with_timeout(source, str(camera.id))
        if opened:
            ok, _ = await asyncio.to_thread(source.read)
            if ok:
                online_fps = source.fps() or camera.fps or 15.0
                online_resolution = source.resolution() or camera.resolution
                camera.status = "online"  # type: ignore[assignment]
                camera.fps = online_fps  # type: ignore[assignment]
                camera.resolution = online_resolution  # type: ignore[assignment]
                await _safe_commit(db, str(camera.camera_code), reapply=lambda: (
                    setattr(camera, "status", "online"),
                    setattr(camera, "fps", online_fps),
                    setattr(camera, "resolution", online_resolution),
                ))
                await self_heal.record_event(
                    component="camera", camera_id=camera_id, error_type=error_type,
                    severity="info", message=f"Camera {camera_code} stream reopened",
                    recovery_action="RECONNECT", attempt=attempt, max_attempts=max_attempts,
                    status="RECOVERED", duration_seconds=time.monotonic() - reconnect_started,
                )
                return True
    await self_heal.record_event(
        component="camera", camera_id=camera_id, error_type=error_type,
        severity="critical", message=f"Camera {camera_code} stream unavailable after {max_attempts} reconnect attempts",
        recovery_action="RECONNECT", attempt=max_attempts, max_attempts=max_attempts,
        status="FAILED", duration_seconds=time.monotonic() - reconnect_started,
    )
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


VEHICLE_CLASSES = ("car", "truck", "bus", "motorbike")

# Fields that a V2 upsert may mutate on an ALREADY-PERSISTENT row. A rollback
# expires a persistent object's mutations back to their last-committed values
# (it does not merely detach the object, the way it does for a never-committed
# one), so a retry has to reassign these from values captured before the
# rollback — re-add() alone would restore the stale row. Same contract the
# vehicle/alert reapply closures in this module and correlate.py already follow.
_PLATE_REAPPLY_FIELDS = (
    "confidence", "plate_text_normalized", "plate_text_raw", "reads_count",
    "last_seen", "snapshot_path", "plate_bbox", "vehicle_bbox",
    # The ANPR explainability fields are mutated on the same persistent row by
    # upsert_plate_sighting, so they need the same rollback treatment — without
    # this a retry would restore the last-committed provenance onto a row whose
    # text and confidence were reassigned from the new read.
    "ocr_variant", "variants_agreeing", "corroborated", "plate_crop_path",
)
_TRACK_REAPPLY_FIELDS = ("last_seen", "detection_count", "vehicle_id", "plate_reads")


def _snapshot_attrs(row: Any, fields: "tuple[str, ...]") -> "dict[str, Any] | None":
    """Capture a row's current values for the fields a retry must restore."""
    if row is None:
        return None
    return {name: getattr(row, name) for name in fields}


def _restore_row(db: Session, row: Any, values: "dict[str, Any] | None") -> None:
    """Re-attach a row and reassign the captured values. `db.add` is a no-op for
    a row that is still attached, and re-attaches one a rollback detached."""
    if row is None:
        return
    db.add(row)
    for name, value in (values or {}).items():
        setattr(row, name, value)


def _rejection_reason(read) -> str:
    """Why the quality gate turned a read down. One of a small fixed set, so it
    can be a Prometheus label without unbounded cardinality."""
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

    Returns `(OcrRead, full_frame_plate_bbox_or_None, plate_crop_or_None)`.

    Sequence, and why each step is where it is:

    1. **Detect** candidate plate regions. Only the best is read — each extra
       region is a full OCR pass, the most expensive operation in the loop.
    2. **Crop + perspective-correct + preprocess** that region. Perspective
       correction only fires when the detector recovered a genuinely skewed
       quad, so a front-on plate does not pay for a warp.
    3. **Read** every configured preprocessing variant and select between them
       by agreement (`anpr.select_candidate`). With the default single-variant
       configuration this is one OCR pass, exactly as before.
    4. **Fall back to the whole vehicle crop** if the localized read fails the
       gate. Measured (docs/ANPR_ACCURACY.md): localization sometimes returns a
       sub-region of the plate, after which OCR reads nothing — on a labelled
       corpus that cut exact match from 0.16 to 0.04. The fallback pass is paid
       ONLY on failure, so a successful localization keeps its ~3x speed win.
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
            # The localizer works in the crop's coordinate space; the stored
            # bbox is full-frame so it can be drawn on a frame or an evidence
            # snapshot without the caller needing the vehicle box to offset it.
            plate_bbox = [
                box.x1 + offset_x, box.y1 + offset_y, box.x2 + offset_x, box.y2 + offset_y,
            ]
    if not variants:
        # No plate region found — read the whole vehicle crop, i.e. the
        # pre-localization behavior. A localization miss degrades the read's
        # quality, it does not discard it. plate_bbox stays None, which is the
        # honest record that this read was NOT localized.
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
            # The winning read came from the whole crop, so the localized box
            # does not describe it — recorded as null rather than attaching a
            # bbox that points at the wrong region.
            read, plate_bbox, plate_crop_image = fallback, None, None
    metrics.OCR_SECONDS.labels(camera_code=camera_code).observe(time.monotonic() - ocr_started)
    return read, plate_bbox, plate_crop_image


def _better_structured_read(first, second):
    """Pick the more trustworthy of two structured reads of the same plate.

    Same ranking as `anpr.better_read` — gate-pass beats non-pass, then
    non-empty beats empty, then higher confidence — lifted to `OcrRead` so the
    variant/agreement provenance travels with the winner instead of being
    flattened back to a tuple and lost.
    """
    first_passes, second_passes = passes_read_gate(first), passes_read_gate(second)
    if first_passes != second_passes:
        return first if first_passes else second
    if bool(first.normalized) != bool(second.normalized):
        return first if first.normalized else second
    return first if first.confidence >= second.confidence else second


async def _run_anpr(
    db: Session, detection: dict[str, Any], det_row: models.Detection, frame: np.ndarray,
    camera_id: str, camera_code: str, frame_source_ts: datetime | None,
) -> "tuple[models.Vehicle | None, models.Plate | None, str | None]":
    """ANPR for one vehicle detection. Returns (vehicle, plate_row, snapshot_path).

    Two paths, chosen per detection:

    **V2 (default)** — requires a ByteTrack track id, because everything it
    does is anchored to "this specific tracked vehicle". The vehicle crop is
    narrowed to an actual plate region (plate_detect), OCR runs only when this
    track still needs a read (plate_tracker.should_ocr), reads VOTE rather than
    overwrite, and the result is ONE sighting row per (camera, track) that gets
    updated — not one row per frame.

    **Legacy** — `PLATE_PIPELINE_V2=false`, or a detection with no track id
    (ByteTrack has not assigned one yet on the object's first frames). Whole
    vehicle crop straight to OCR, one row per passing frame: the exact pre-V2
    behavior, preserved rather than approximated, so the escape hatch is real.
    """
    x1, y1, x2, y2 = [max(0, int(v)) for v in detection["bbox"]]
    crop = frame[y1:y2, x1:x2]
    if crop.size == 0:
        return None, None, None
    vehicle_bbox = [float(x1), float(y1), float(x2), float(y2)]
    raw_track_id = detection.get("track_id")

    # ---------------- Legacy path ----------------
    if not settings.plate_pipeline_v2 or raw_track_id is None:
        raw, normalized, conf = await asyncio.to_thread(read_plate, crop)
        if not passes_anpr_gate(normalized, conf):
            return None, None, None
        snapshot_path = await asyncio.to_thread(_save_snapshot, frame, camera_code)
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

    # ---------------- V2 path ----------------
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
            # Nothing usable this pass. Recorded so an unreadable track (rear of
            # a truck, plate out of frame) is retried on the reverify interval
            # rather than on every single inference cycle forever.
            plate_tracker.mark_ocr_attempt(camera_id, track_id)

    best = state.best()
    if best is None:
        return None, None, None
    plate_text, peak_confidence, reads = best
    # Temporal gate: a plate only becomes TRUSTED intelligence once enough
    # independent frames agreed on it. An uncorroborated read is still recorded
    # (a vehicle crossing the frame in one inference cycle is real and must not
    # be lost) but is forced to `pending_review` below, so one lucky frame can
    # no longer present itself as a settled vehicle identity.
    corroborated = plate_tracker.has_consensus(camera_id, track_id)
    if not corroborated and settings.plate_require_consensus:
        # Strict mode: the operator has chosen to hold nothing uncorroborated.
        return None, None, None
    if not plate_tracker.should_persist(camera_id, track_id, new_read):
        # Nothing changed worth a write; the caller still gets the vehicle so
        # rule evaluation (watchlist) keeps firing on every frame it should.
        vehicle = (
            db.query(models.Vehicle).filter(models.Vehicle.id == state.vehicle_id).first()
            if state.vehicle_id else None
        )
        return vehicle, None, None

    vehicle = await upsert_vehicle_for_plate(db, plate_text, peak_confidence, corroborated)
    snapshot_path = None
    plate_crop_path = None
    if state.plate_row_id is None:
        # One evidence snapshot per sighting, taken at the moment the vehicle is
        # first confidently identified here — not one per OCR frame.
        snapshot_path = await asyncio.to_thread(_save_snapshot, frame, camera_code)
        # The plate region that produced the read, saved alongside the full
        # frame so an operator reviewing a sighting can see what OCR actually
        # looked at rather than having to trust the text. Best-effort and
        # opt-in: a failure here must never cost the sighting itself.
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
        metrics.INFERENCE_SECONDS.labels(camera_code=camera_code).observe(inference_ms / 1000.0)
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
            # Root-cause fix: this is a real write against SQLite (assigns
            # det_row's identity for the rest of this function) and was
            # previously unguarded — a lock here escaped to _camera_loop's
            # outer except, silently dropping this detection instead of
            # being retried like every other write in this pipeline (see
            # db_retry.safe_flush). Bounded (max_attempts default 4,
            # matching safe_commit); permanent failure here means this one
            # detection could not be persisted — skip it and continue with
            # the rest of the frame's detections rather than losing them too.
            flushed = await _safe_flush(db, camera_code, reapply=lambda _det_row=det_row: db.add(_det_row))
            if not flushed:
                continue
            metrics.DETECTIONS_TOTAL.labels(camera_code=camera_code, cls=d["cls"]).inc()

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
            plate_row = None
            track_row = None
            if bool(camera.ai_anpr) and d["cls"] in VEHICLE_CLASSES:
                vehicle, plate_row, anpr_snapshot = await _run_anpr(
                    db, d, det_row, frame, camera_id, camera_code, frame_source_ts,
                )
                if anpr_snapshot:
                    snapshot_path = anpr_snapshot
                    det_row.snapshot_path = snapshot_path  # type: ignore[assignment]

            # A tracked vehicle is real, followable intelligence whether or not
            # its plate was ever read — so the Track row is written for every
            # tracked vehicle, and gets its vehicle_id filled in if and when the
            # plate identifies it. Throttled (should_persist_track), not
            # per-frame. models.Track was declared in the original schema but
            # never written by anything until now.
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
                        # Track bookkeeping is intelligence metadata, not the
                        # detection record itself — a failure here must never
                        # cost the detection/plate/alert this frame produced.
                        logger.exception("camera %s: track upsert failed for track %s", camera_code, track_key)
                        track_row = None

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
            #
            # Retry-with-reapply on a transient lock (verified empirically —
            # see db_retry.py): det_row/plate_row are freshly db.add()'d,
            # never-committed objects, so a rollback only detaches them —
            # their already-set Python attributes (including det_row.id,
            # a client-side-generated PK computed at the db.flush() above,
            # and plate_row's FK captured from it) survive untouched, so
            # re-add() alone correctly restores them. `vehicle` may instead
            # be a PRE-EXISTING, persistent row whose last_seen/
            # plate_confidence were just mutated in-place (correlate.py) —
            # rollback expires those back to their last-committed value, so
            # reapply also explicitly re-sets them from the values captured
            # right after they were computed (never by re-reading
            # `vehicle.*`, which could return the stale, reverted value).
            vehicle_target_last_seen = vehicle.last_seen if vehicle is not None else None
            vehicle_target_confidence = vehicle.plate_confidence if vehicle is not None else None
            # V2: plate_row and track_row can now be PRE-EXISTING persistent rows
            # that were just UPDATED in place (a vehicle still in frame), not
            # only freshly-added transient ones. For a persistent row a rollback
            # expires the mutations back to their last-committed values rather
            # than merely detaching the object, so re-add() alone would silently
            # restore the OLD values — the same trap correlate.py and the vehicle
            # branch above already document. Field values are therefore captured
            # here, right after they were set, and reassigned on retry.
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
                    db.add(_vehicle)  # no-op if already persistent/attached
                    _vehicle.last_seen = _last_seen
                    _vehicle.plate_confidence = _confidence

            await _safe_commit(db, camera_code, reapply=_reapply_detection_commit)
            alerts = await evaluate(db, camera, det_row, w, h, vehicle)
            for alert in alerts:
                # Via the helper, not a direct Incident.alert_id query: an alert
                # correlated INTO an existing incident is linked through
                # IncidentAlert, and a direct query would have found nothing and
                # silently orphaned this alert's evidence from its incident.
                incident = find_incident_for_alert(db, str(alert.id))
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
                    # `alert` (committed inside rules_engine.evaluate) and
                    # `det_row` (committed just above) are both already
                    # PERSISTENT by this point — a rollback here would expire
                    # these mutations back to their last-committed value, not
                    # just detach them, so reapply must reassign from these
                    # captured locals, not from re-reading alert.*/det_row.*.
                    det_snapshot_target = det_row.snapshot_path or evidence_snapshot_path
                    alert.snapshot_path = evidence_snapshot_path  # type: ignore[assignment]
                    det_row.snapshot_path = det_snapshot_target  # type: ignore[assignment]
                    evidence_row = models.Evidence(
                        incident_id=incident.id if incident else None,
                        evidence_type="snapshot",
                        camera_id=camera_id,
                        file_path=evidence_snapshot_path,
                        # Baseline digest taken now, while the file is exactly
                        # as captured. Hashing it later would only record what
                        # the file contained at that point and prove nothing.
                        sha256=await asyncio.to_thread(sha256_file, evidence_snapshot_path),
                        alert_id=alert.id,
                        detection_id=det_row.id,
                        event_type=event_type,
                        source_timestamp=frame_source_ts,
                        verification_status="unverified",
                        # Provenance (10/10 roadmap P8): the model/rule versions
                        # ACTIVE at capture, stamped once — never updated later,
                        # so this answers "what produced this" even after
                        # settings.model_version subsequently changes.
                        model_version=settings.model_version,
                        rule_version=settings.rule_version,
                    )
                    db.add(evidence_row)

                    def _reapply_evidence_commit(
                        _alert=alert, _det_row=det_row, _evidence_row=evidence_row,
                        _alert_snapshot=evidence_snapshot_path, _det_snapshot=det_snapshot_target,
                    ):
                        db.add(_alert)  # no-op if already persistent/attached
                        db.add(_det_row)
                        db.add(_evidence_row)
                        _alert.snapshot_path = _alert_snapshot
                        _det_row.snapshot_path = _det_snapshot

                    await _safe_commit(db, camera_code, reapply=_reapply_evidence_commit)

                # Registered so shutdown drains it: this task waits up to
                # clip_post_event_seconds before writing its Evidence row, and
                # an untracked one was destroyed by the closing loop, silently
                # losing evidence for a real alert.
                background.spawn(
                    clips.build_event_clip(
                        camera_id, camera_code, str(alert.id), str(det_row.id),
                        str(incident.id) if incident else None, event_type, frame_source_ts,
                    ),
                    name=f"clip:{camera_code}:{alert.id}",
                )
            # Batched by the manager (see ws.py) — N cameras x their inference
            # rate would otherwise be that many WebSocket frames and React state
            # updates per second in every open dashboard.
            await manager.publish(EventType.DETECTION_CREATED, {
                "detection_id": str(det_row.id),
                "camera_id": camera_id, "camera_code": camera_code,
                "cls": d["cls"], "confidence": d["confidence"],
                "track_id": (str(d["track_id"]) if d.get("track_id") is not None else None),
                "bbox": d["bbox"],
                "timestamp": det_row.timestamp.isoformat(),
            })
            # A recognized plate is its own event: the live control room shows
            # it as an identification, not as one more anonymous detection. Sent
            # only when the sighting row was actually written this frame, so a
            # vehicle sitting in view does not re-announce itself every cycle.
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
        # Reverse lookup for self-heal event logging (_self_heal_camera_id
        # below) — _safe_commit/_safe_flush only ever see camera_code, not
        # camera_id, at their existing call sites.
        _stats(camera_id)["camera_code"] = str(camera.camera_code)
        source = CameraSource(str(camera.source_type), str(camera.source_uri))
        _set_grid_state(camera_id, "CONNECTING")
        opened = await _open_with_timeout(source, camera_id)
        if not opened:
            # Real reconnect attempt on initial failure too (transient RTSP/
            # webcam-busy failures), not just an immediate give-up.
            opened = await _reopen_with_backoff(source, camera, db, reason="initial_connect")
        if not opened:
            offline_error_count = camera.error_count + 1  # type: ignore[operator]
            camera.status = "offline"  # type: ignore[assignment]  # legacy Column() declarative model — plain-value assignment is correct at runtime
            camera.error_count = offline_error_count  # type: ignore[assignment]
            await _safe_commit(db, str(camera.camera_code), reapply=lambda: (
                setattr(camera, "status", "offline"),
                setattr(camera, "error_count", offline_error_count),
            ))
            if _stats(camera_id)["grid_state"] not in ("AUTH_ERROR",):
                _set_grid_state(camera_id, "DISCONNECTED")
            return
        initial_fps = source.fps() or 15.0
        initial_resolution = source.resolution()
        camera.status = "online"  # type: ignore[assignment]
        camera.fps = initial_fps  # type: ignore[assignment]
        camera.resolution = initial_resolution  # type: ignore[assignment]
        # grid_state was left at CONNECTING (direct path) or RECONNECTING
        # (via _reopen_with_backoff above) — the main loop's own desired_state
        # check corrects this on the next successful frame read, but that is
        # a window with no guarantee the first read succeeds. Setting it here
        # removes the window instead of hoping the loop closes it quickly.
        _set_grid_state(camera_id, "CONNECTED")
        await _safe_commit(db, str(camera.camera_code), reapply=lambda: (
            setattr(camera, "status", "online"),
            setattr(camera, "fps", initial_fps),
            setattr(camera, "resolution", initial_resolution),
        ))
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
        # Throttled to at most once every 2s (raised from Phase 4's 1s per
        # the concurrency-hardening finding that 1/sec/camera was still the
        # dominant write-pressure source with 5+ concurrent cameras); real
        # detection/alert data still commits immediately wherever it's
        # written (unaffected, see _process_frame).
        HEARTBEAT_MIN_INTERVAL_S = 2.0
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
                    read_fail_error_count = camera.error_count + 1  # type: ignore[operator]
                    camera.error_count = read_fail_error_count  # type: ignore[assignment]
                    st["read_failures"] += 1
                    if consecutive_failures < settings.read_failures_before_reconnect:
                        camera.status = "degraded"  # type: ignore[assignment]
                        _set_grid_state(camera_id, "DEGRADED")
                        await _safe_commit(db, camera_code_cached, reapply=lambda: (
                            setattr(camera, "status", "degraded"),
                            setattr(camera, "error_count", read_fail_error_count),
                        ))
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
                            await _safe_commit(db, camera_code_cached, reapply=lambda: setattr(camera, "status", "offline"))
                            return  # stop this worker; operator can Restart the camera
                        consecutive_failures = 0
                        session_opened_at = datetime.now(timezone.utc)
                        last_pos_msec = None
                        camera_fps_cached = float(camera.fps)  # type: ignore[arg-type]  # legacy Column() declarative attribute — plain float at runtime
                        st["reconnects"] += 1
                        metrics.CAMERA_RECONNECTS.labels(camera_code=camera_code_cached).inc()
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
                    # Highest-frequency commit in the whole pipeline (up to
                    # once/sec per camera, so N concurrent cameras = N/sec
                    # writers to the same SQLite file) — real production logs
                    # showed this specific commit as the dominant source of
                    # "database is locked". Retried in place (reapply just
                    # re-sets these two fields from the locals captured
                    # below) so a transient lock here never falls through to
                    # the outer except below, which would otherwise flip a
                    # perfectly healthy camera to grid_state=ERROR and bump
                    # error_count for a heartbeat write that had nothing to
                    # do with the actual frame processing (which already
                    # succeeded above).
                    heartbeat_last_frame_at = datetime.now(timezone.utc)
                    camera.last_frame_at = heartbeat_last_frame_at  # type: ignore[assignment]
                    camera.status = "online"  # type: ignore[assignment]
                    if now_mono - last_heartbeat_commit_at >= HEARTBEAT_MIN_INTERVAL_S:
                        await _safe_commit(db, camera_code_cached, reapply=lambda: (
                            setattr(camera, "last_frame_at", heartbeat_last_frame_at),
                            setattr(camera, "status", "online"),
                        ))
                        last_heartbeat_commit_at = now_mono
                    loop_sleep_s = max(0.01, 1.0 / max(camera_fps_cached, 1.0))
            except Exception as exc:
                logger.exception("camera %s: loop iteration failed, continuing", camera_code_cached)
                st["last_error"] = f"{type(exc).__name__}: {exc}"
                st["recovered_errors"] += 1
                # Was a direct dict write, which skipped the transition check
                # entirely — the one place most likely to produce a surprising
                # transition was the one place not recording it.
                _set_grid_state(camera_id, "ERROR")  # next successful iteration flips this back
                _error_type, _severity = self_heal.classify_exception(exc)
                background.spawn(
                    self_heal.record_event(
                        component="worker", camera_id=camera_id, error_type=_error_type, severity=_severity,
                        message=f"camera {camera_code_cached}: {exc}", recovery_action="CONTINUE_LOOP",
                        attempt=1, max_attempts=1, status="RECOVERED",
                    ),
                    name=f"self-heal:{camera_code_cached}",
                )  # fire-and-forget: this is diagnostic logging, must never delay/block the loop's own recovery below
                try:
                    db.rollback()
                except Exception:
                    logger.exception("camera %s: rollback after error also failed", camera_code_cached)
                else:
                    # Reading `camera.error_count` here is itself a fresh
                    # SELECT (rollback just expired it) — the exact "implicit
                    # unguarded SELECT can itself hit a locked database" risk
                    # this loop's own guard comment above warns about, so
                    # it's covered by the same broad except as everything
                    # else in this handler rather than being allowed to
                    # escape and kill the task.
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
        if source is not None:
            source.release()
        # close_session, NOT db.close(): a cancellation unwinds the awaits in
        # this loop immediately, but a DB call already handed to a worker
        # thread by asyncio.to_thread keeps running there. A plain close() from
        # this `finally` therefore raced an in-flight commit and raised
        # IllegalStateChangeError ("Method 'close()' can't be called here") —
        # which, coming from a finally, escaped to _camera_loop_supervised and
        # marked a perfectly healthy camera OFFLINE on an ordinary stop.
        # close_session waits for that thread and never raises.
        close_session(db)


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
            db: Session = SessionLocal()
            try:
                camera = db.query(models.Camera).filter(models.Camera.id == camera_id).first()
                if camera:
                    camera.status = "offline"  # type: ignore[assignment]
                    camera.error_count += 1  # type: ignore[assignment]
                    db.commit()
                # Real gap this closed: the worker task is dead once we are
                # here, so unlike every other status write in this file, there
                # is no next loop iteration to self-correct grid_state — it
                # would sit at whatever it last was (e.g. PROCESSING) forever,
                # disagreeing with the DB's "offline" and with the actual
                # (nonexistent) worker, until an operator restarts the camera.
                _set_grid_state(camera_id, "DISCONNECTED")
            finally:
                db.close()
        except Exception:
            logger.exception("camera %s: could not mark offline after top-level crash", camera_id)


def start_worker(camera_id: str) -> None:
    existing = RUNNING.get(camera_id)
    if existing and not existing.done():
        return
    RUNNING[camera_id] = asyncio.create_task(_camera_loop_supervised(camera_id))


def stop_worker(camera_id: str) -> "asyncio.Task[None] | None":
    """Cancels the camera's task and releases its per-camera resources.

    Returns the cancelled task (or None if it wasn't running) so a caller
    that needs a DETERMINISTIC guarantee that cleanup actually finished —
    e.g. process shutdown — can `await asyncio.gather(...)` on it.
    `task.cancel()` alone only *requests* cancellation; the task's own
    `finally: source.release()` (see _camera_loop) only runs once the task
    is next scheduled, which never happens on its own if nothing yields
    control back to it before the event loop is torn down (audit finding:
    confirmed neither the previous _on_shutdown nor stop_supervisor actually
    awaited this, so a shutdown racing the ASGI server's own teardown could
    leave a camera's asyncio task/cv2.VideoCapture orphaned)."""
    task = RUNNING.pop(camera_id, None)
    if task:
        task.cancel()
    LATEST_FRAMES.pop(camera_id, None)
    release_model(camera_id)  # drop this camera's YOLO/ByteTrack instance
    clips.release_camera(camera_id)  # drop this camera's event-clip ring buffer
    plate_tracker.release_camera(camera_id)  # drop this camera's per-track plate votes
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
    return task
