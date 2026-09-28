"""Camera connect/reconnect, split out of worker.py.

_open_with_timeout opens a source with our own timeout (FFmpeg doesn't
reliably honour CAP_PROP_OPEN_TIMEOUT_MSEC) and _reopen_with_backoff retries
a dropped one with exponential backoff. Both take source/camera/db as
arguments and return a bool, no hidden state.
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
    """source.open() blocks on a raw VideoCapture connect, so it runs in a
    thread with our own asyncio timeout: CAP_PROP_OPEN_TIMEOUT_MSEC isn't
    honoured by every build (a dead RTSP host hung ~30s with 5s set). The
    abandoned thread runs until cv2 gives up, but the loop moves on and keeps
    backing off."""
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
        # adapters can raise on purpose (ONVIF stub, grid adapter without
        # credentials); still fail the camera cleanly, offline and logged
        logger.exception("camera source failed to open")
        if camera_id:
            _set_grid_state(camera_id, "AUTH_ERROR" if "credentials not configured" in str(exc) else "ERROR")
        return False


async def _reopen_with_backoff(source: "CameraSource", camera: models.Camera, db: Session, reason: str = "stream_read_failure") -> bool:
    """Release and reopen a dropped source with exponential backoff. True
    once reopened, False when out of retries (caller marks offline and stops).

    `reason` just labels the Self-Heal event: "initial_connect" or
    "stream_read_failure". cv2 only gives us read() == False, no decode error
    detail, so we don't pretend to diagnose H264 problems."""
    camera_id = str(camera.id)
    camera_code = str(camera.camera_code)
    error_type = "CAMERA_CONNECT_FAILURE" if reason == "initial_connect" else "STREAM_READ_FAILURE"
    _set_grid_state(camera_id, "RECONNECTING")
    reconnect_started = time.monotonic()
    max_attempts = settings.reconnect_max_attempts
    for attempt in range(1, max_attempts + 1):
        # Column()-style attrs look like Column[T] to Pylance; plain values at
        # runtime. Targets captured first so reapply sets them from these and
        # never re-reads camera.* after a rollback expired them (db_retry.py).
        degraded_error_count = camera.error_count + 1  # type: ignore[operator]
        # grid_state is RECONNECTING for the whole loop; take the status from
        # the same table so the two can't drift
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
                # set CONNECTED here, this is the moment we know the stream is
                # back; waiting for the caller left a window where diagnostics
                # and the grid still showed RECONNECTING
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
