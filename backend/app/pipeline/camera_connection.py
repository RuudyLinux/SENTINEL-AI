"""Camera source connect/reconnect, extracted from worker.py.

`_open_with_timeout` and `_reopen_with_backoff` are the whole "Connection
Manager" concern the camera loop delegates to: opening a source with a real
timeout (cv2/FFmpeg's own CAP_PROP_OPEN_TIMEOUT_MSEC is not reliably honoured
-- see `_open_with_timeout`'s own docstring for the measured case), and
retrying a dropped one with exponential backoff. Both take the source/camera/
db they operate on as explicit parameters and return a plain bool -- no
hidden closure state, which is what made them separable from the loop that
calls them.
"""
import asyncio
import logging
import time

from sqlalchemy.orm import Session

from .. import models
from ..config import settings
from ..self_heal import engine as self_heal
from .camera_state import _DB_STATUS_FOR_GRID_STATE, _set_grid_state
from .db_helpers import _safe_commit
from .source import CameraSource

logger = logging.getLogger("sentinel.worker")


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
        # grid_state is RECONNECTING for the whole retry loop (set once at
        # this function's entry); this derives the paired status from the
        # SAME table that says what RECONNECTING means, rather than the two
        # being two independently-written literals a future edit could let
        # drift apart.
        camera.status = _DB_STATUS_FOR_GRID_STATE["RECONNECTING"]  # type: ignore[assignment]
        camera.error_count = degraded_error_count  # type: ignore[assignment]
        _reconnecting_status = camera.status
        await _safe_commit(db, str(camera.camera_code), reapply=lambda: (
            setattr(camera, "status", _reconnecting_status),
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
                # grid_state was left at RECONNECTING (set at this function's
                # entry) up to this point. Both of this function's callers
                # correct it shortly after receiving True back — one via the
                # main loop's own next-iteration desired_state check, the
                # other explicitly a few lines below its own call site — but
                # that left a real window (a diagnostics read, or the Camera
                # Grid UI, between "reopened" and "caller got around to
                # saying so") for no reason: the moment this function itself
                # knows the stream is back is the natural place to say so.
                new_state = "CONNECTED"
                camera.status = _DB_STATUS_FOR_GRID_STATE[new_state]  # type: ignore[assignment]
                camera.fps = online_fps  # type: ignore[assignment]
                camera.resolution = online_resolution  # type: ignore[assignment]
                _reopened_status = camera.status
                await _safe_commit(db, str(camera.camera_code), reapply=lambda: (
                    setattr(camera, "status", _reopened_status),
                    setattr(camera, "fps", online_fps),
                    setattr(camera, "resolution", online_resolution),
                ))
                _set_grid_state(camera_id, new_state)
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
