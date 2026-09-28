"""Sentinel Camera Grid connection supervisor.

Discovers the catalogue at startup, connects eligible grid cameras and
reconnects drops on a periodic sweep. Connections only: it never changes
ai_* flags. Starts within a sweep are staggered (sentinel_grid_stagger_seconds)
because simultaneous RTSP handshakes are less reliable. It only decides when
to call worker.start_worker/stop_worker.
"""
import asyncio
import logging
import time

from sqlalchemy.orm import Session

from .. import models
from ..config import settings
from ..db import SessionLocal
from . import worker
from .sentinel_grid import fetch_grid_cameras, upsert_grid_cameras, SentinelGridError

logger = logging.getLogger("sentinel.supervisor")

# Cameras the supervisor keeps connected. In memory only, so a restart
# reconnects everything eligible.
AUTO_MANAGED: set[str] = set()

# Cameras an operator disconnected; the sweep leaves these alone.
OPERATOR_DISCONNECTED: set[str] = set()

_last_restart_attempt: dict[str, float] = {}
# Minimum gap between supervisor restarts of one camera, on top of the worker's
# own reconnect backoff.
_MIN_RESTART_INTERVAL_S = 20.0

# Grid-wide circuit breaker. OpenCV's FFmpeg backend hides the RTSP response,
# so a rejected login looks like a network blip. A shared-login failure has a
# recognisable shape (many distinct cameras attempted, none connected); when it
# appears, all grid connects back off at once so the account isn't hammered.
_GRID_WIDE_FAILURE_THRESHOLD = 5
_grid_wide_cooldown_until = 0.0
# Consecutive trips: each doubles the pause (up to _MAX_GRID_COOLDOWN_S), and
# after a pause one probe camera must connect before the rest retry.
_grid_trips = 0
_MAX_GRID_COOLDOWN_S = 3600.0
_probe_camera: "str | None" = None
_probe_started = 0.0
# a probe that hasn't connected within this long counts as failed
_PROBE_TIMEOUT_S = 90.0

_supervisor_task: "asyncio.Task[None] | None" = None


def _grid_credentials_configured() -> bool:
    return bool(settings.sentinel_grid_email and settings.sentinel_grid_password)


async def discover_and_register() -> None:
    """Fetch and register the catalogue without starting workers. Grid errors
    are logged, never raised, so startup continues."""
    if not _grid_credentials_configured():
        logger.info("Sentinel Grid credentials not configured — skipping catalogue discovery at startup")
        return
    db: Session = SessionLocal()
    try:
        records = await fetch_grid_cameras()
        summary = upsert_grid_cameras(db, records)
        logger.info("Sentinel Grid startup discovery: %s", summary)
    except SentinelGridError as exc:
        logger.warning("Sentinel Grid startup discovery failed (will retry on the next supervisor sweep): %s", exc)
    finally:
        db.close()


def _eligible_camera_ids(db: Session) -> list[str]:
    rows = (
        db.query(models.Camera)
        .filter(
            models.Camera.source_type == "sentinel_grid",
            models.Camera.catalog_stale == False,  # noqa: E712
            models.Camera.retired == False,  # noqa: E712
        )
        .order_by(models.Camera.camera_code.asc())
        .all()
    )
    return [str(c.id) for c in rows]


def _is_running(camera_id: str) -> bool:
    task = worker.RUNNING.get(camera_id)
    return bool(task and not task.done())


def _grid_wide_rejection_detected() -> "int | None":
    """Number of grid cameras attempted if it looks like a shared-login
    rejection (enough attempted, none connected), else None. Only cameras whose
    worker has run count, so a fresh start can't trip it."""
    attempted = [
        cid for cid in AUTO_MANAGED
        if worker.CAMERA_STATS.get(cid, {}).get("started_at") is not None
    ]
    if len(attempted) < _GRID_WIDE_FAILURE_THRESHOLD:
        return None
    connected = sum(
        1 for cid in attempted
        if worker.CAMERA_STATS.get(cid, {}).get("grid_state") in ("CONNECTED", "PROCESSING")
    )
    return len(attempted) if connected == 0 else None


def _is_connected(camera_id: str) -> bool:
    return worker.CAMERA_STATS.get(camera_id, {}).get("grid_state") in ("CONNECTED", "PROCESSING")


def _trip(now: float, reason: str) -> None:
    """Pause all grid connects, longer on each trip, and stop the workers still
    retrying."""
    global _grid_wide_cooldown_until, _grid_trips, _probe_camera
    _grid_trips += 1
    pause = min(_MAX_GRID_COOLDOWN_S, settings.sentinel_grid_auth_cooldown_seconds * 2 ** (_grid_trips - 1))
    _grid_wide_cooldown_until = now + pause
    _probe_camera = None
    for cid in list(AUTO_MANAGED):
        if _is_running(cid) and not _is_connected(cid):
            worker.stop_worker(cid)
    logger.warning(
        "Sentinel Grid supervisor: %s. Treating it as a shared-login rejection, stopping the "
        "cameras still trying, and pausing ALL grid connects for %.0fs (trip %d); after that one "
        "camera tests the login before the rest reconnect.",
        reason, pause, _grid_trips,
    )


async def _connect_eligible(db: Session) -> int:
    """One sweep: start eligible cameras that aren't running, staggered.
    Returns how many were started."""
    global _grid_trips, _probe_camera, _probe_started
    if not settings.sentinel_grid_autoconnect or not _grid_credentials_configured():
        return 0

    now = time.monotonic()
    if now < _grid_wide_cooldown_until:
        return 0

    eligible = [cid for cid in _eligible_camera_ids(db) if cid not in OPERATOR_DISCONNECTED]

    if _grid_trips:
        # after a pause: one camera probes the login first
        if _probe_camera is None:
            if not eligible:
                return 0
            _probe_camera, _probe_started = eligible[0], now
            AUTO_MANAGED.add(_probe_camera)
            _last_restart_attempt[_probe_camera] = now
            worker.start_worker(_probe_camera)
            logger.info("Sentinel Grid supervisor: probing the grid login with one camera")
            return 1
        if _is_connected(_probe_camera):
            logger.info("Sentinel Grid supervisor: probe connected, bringing every camera back")
            _grid_trips, _probe_camera = 0, None
        elif not _is_running(_probe_camera) or now - _probe_started > _PROBE_TIMEOUT_S:
            _trip(now, "the probe camera could not connect")
            return 0
        else:
            return 0
    else:
        rejected_count = _grid_wide_rejection_detected()
        if rejected_count is not None:
            _trip(now, f"{rejected_count} distinct grid cameras attempted, none connected")
            return 0

    # operator-disconnected cameras are left out of both the bookkeeping and
    # the connect loop below (filtering only one still reconnected them)
    AUTO_MANAGED.update(eligible)

    running_count = sum(1 for cid in AUTO_MANAGED if _is_running(cid))
    slots = max(0, settings.sentinel_grid_max_autoconnect - running_count)
    if slots <= 0:
        return 0

    started = 0
    for camera_id in eligible:
        if started >= slots:
            break
        if _is_running(camera_id):
            continue
        # rejected credentials fail the same for every camera (one shared
        # login), so back off much longer than for a transient failure
        stats = worker.CAMERA_STATS.get(camera_id, {})
        floor = (
            settings.sentinel_grid_auth_cooldown_seconds
            if stats.get("grid_state") == "AUTH_ERROR"
            else _MIN_RESTART_INTERVAL_S
        )
        last_attempt = _last_restart_attempt.get(camera_id, 0.0)
        # not `now`: the stagger sleeps between cameras, so that's stale
        if time.monotonic() - last_attempt < floor:
            continue
        _last_restart_attempt[camera_id] = time.monotonic()
        worker.start_worker(camera_id)
        started += 1
        # spread the handshakes out; no sleep after the last one
        if started < slots:
            await asyncio.sleep(settings.sentinel_grid_stagger_seconds)

    if started:
        logger.info(
            "Sentinel Grid supervisor: (re)connected %d camera(s) this sweep (cap=%d, now running=%d)",
            started, settings.sentinel_grid_max_autoconnect, running_count + started,
        )
    return started


async def _sweep_loop() -> None:
    while True:
        try:
            db: Session = SessionLocal()
            try:
                await _connect_eligible(db)
            finally:
                db.close()
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("Sentinel Grid supervisor sweep failed, continuing")
        await asyncio.sleep(settings.sentinel_grid_supervisor_sweep_seconds)


def start_supervisor() -> None:
    """Call once at startup, after discover_and_register()."""
    global _supervisor_task
    if _supervisor_task is not None and not _supervisor_task.done():
        return
    _supervisor_task = asyncio.create_task(_sweep_loop())


async def stop_supervisor() -> None:
    """Shutdown: cancel the sweep and stop every auto-managed worker."""
    global _supervisor_task
    if _supervisor_task is not None:
        _supervisor_task.cancel()
        try:
            await _supervisor_task
        except asyncio.CancelledError:
            pass
        except Exception:
            logger.exception("Sentinel Grid supervisor sweep task raised on shutdown")
        _supervisor_task = None
    # stop_worker only requests cancellation; await the tasks so their
    # release actually runs before the loop goes away
    tasks = [t for t in (worker.stop_worker(camera_id) for camera_id in list(AUTO_MANAGED)) if t is not None]
    if tasks:
        await asyncio.gather(*tasks, return_exceptions=True)
    AUTO_MANAGED.clear()
    OPERATOR_DISCONNECTED.clear()


def connect(camera_id: str) -> None:
    """Operator Connect: auto-managed from now on, and started."""
    OPERATOR_DISCONNECTED.discard(camera_id)
    AUTO_MANAGED.add(camera_id)
    _last_restart_attempt[camera_id] = time.monotonic()
    worker.start_worker(camera_id)


def disconnect(camera_id: str) -> None:
    """Operator disconnect: removed from auto-management first so the next
    sweep leaves it alone, then stopped."""
    AUTO_MANAGED.discard(camera_id)
    OPERATOR_DISCONNECTED.add(camera_id)
    worker.stop_worker(camera_id)


async def restart(camera_id: str, source_type: str) -> None:
    """Operator restart, shared by the single-camera and bulk endpoints.

    Grid cameras go back through connect() so they stay auto-managed. The old
    task is awaited before starting so start_worker never sees it still running.
    """
    task = worker.stop_worker(camera_id)
    if task is not None:
        await asyncio.gather(task, return_exceptions=True)
    if source_type == "sentinel_grid":
        connect(camera_id)
    else:
        worker.start_worker(camera_id)
