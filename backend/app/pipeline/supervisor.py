"""24/7 real Sentinel Camera Grid connection supervisor.

Keeps eligible REAL Sentinel Grid cameras' RTSP connection alive automatically
— discovers the catalogue at startup, connects eligible cameras up to a
resource-safety cap, and reconnects any that drop, on a periodic sweep. It
never enables AI: "connected/LIVE" and "AI processing" are and remain two
independent concerns (see worker.py._process_frame's ai_enabled gate) — a
camera this supervisor connects stays exactly whatever `ai_person`/
`ai_vehicle`/`ai_anpr` it already has (False by default for a freshly
discovered camera, see sentinel_grid.upsert_grid_cameras); AI is always an
explicit, separate operator action (`PATCH /api/cameras/{id}`).

Concurrency optimization finding (staged real-camera testing): starting N
eligible cameras' workers back-to-back in one sweep opens N simultaneous new
RTSP TCP handshakes against the external grid at once — measured as
meaningfully less reliable than the same N cameras brought up one at a time.
Local CPU/RAM were NOT the limiting factor at 10 concurrent; the external
grid's tolerance for a simultaneous connection burst was. `_connect_eligible`
below therefore staggers successive worker starts within one sweep by
`settings.sentinel_grid_stagger_seconds`, real backend restarts included
(there is no separate "burst" code path to disable) — see
CONCURRENCY_OPTIMIZATION.md for the staged 5/8/10/... test results this is
based on.

Deliberately reuses the existing worker lifecycle (`worker.start_worker`/
`stop_worker`/`RUNNING`/`CAMERA_STATS`) rather than a second parallel worker
system — this module only decides *when* to call those, one thin layer above
the tested per-camera loop, backoff, and diagnostics that already exist.

Never applies to any future simulated/logical-scale camera source — only
`source_type == "sentinel_grid"` rows are ever touched here.
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

# camera_ids the supervisor is responsible for keeping connected. Populated at
# startup for every eligible real grid camera, and updated by an operator's
# explicit connect()/disconnect() (see below) — so a manual Disconnect isn't
# immediately undone by the next sweep. In-memory only: a full backend
# restart re-derives this from the DB (every eligible camera again), which is
# the documented "reconnect automatically after a restart" behavior — a
# pre-restart manual Disconnect does not survive a restart.
AUTO_MANAGED: set[str] = set()

# camera_ids an operator explicitly disconnected — excluded from the sweep's
# blanket re-add of every eligible camera into AUTO_MANAGED (see
# _connect_eligible) until the operator explicitly connects it again. Without
# this, a manual Disconnect was undone by the very next sweep: the sweep
# unconditionally re-added every eligible camera to AUTO_MANAGED regardless of
# operator intent, so "stopped, then immediately reconnected" instead of
# staying DISCONNECTED — found via the real browser test of the Disconnect
# button, contradicting disconnect()'s own docstring below. In-memory only,
# same lifetime as AUTO_MANAGED: a full restart re-derives eligibility fresh.
OPERATOR_DISCONNECTED: set[str] = set()

_last_restart_attempt: dict[str, float] = {}
# Floor between the supervisor's OWN restart attempts for a given camera —
# independent of and in addition to worker.py's per-connection-attempt
# backoff (2/4/8/16/30s) inside a single _camera_loop run. This is what turns
# "gives up after its own retry budget" into "24/7, reconnect eventually"
# without making the low-level loop itself infinite.
_MIN_RESTART_INTERVAL_S = 20.0

_supervisor_task: "asyncio.Task[None] | None" = None


def _grid_credentials_configured() -> bool:
    return bool(settings.sentinel_grid_email and settings.sentinel_grid_password)


async def discover_and_register() -> None:
    """Startup discovery: fetch the real catalogue, upsert into the registry
    (register-only, same contract as the manual sync endpoint — never starts
    a worker). A missing/misconfigured/unreachable grid is logged, never
    raised, so it can't prevent the rest of the application from starting."""
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
        .filter(models.Camera.source_type == "sentinel_grid", models.Camera.catalog_stale == False)  # noqa: E712
        .order_by(models.Camera.camera_code.asc())
        .all()
    )
    return [str(c.id) for c in rows]


def _is_running(camera_id: str) -> bool:
    task = worker.RUNNING.get(camera_id)
    return bool(task and not task.done())


async def _connect_eligible(db: Session) -> int:
    """One sweep: (re)connect eligible, not-currently-running, auto-managed
    cameras, up to settings.sentinel_grid_max_autoconnect concurrent
    connections — started one at a time with a real delay between each
    (settings.sentinel_grid_stagger_seconds), not all at once. Returns how
    many connect attempts were started. This coroutine can run for a while
    at a high cap (N cameras * stagger seconds) — that's the staggering
    working as intended, not a bug; the caller (_sweep_loop) awaits it fully
    before its own next-sweep sleep, same as before."""
    if not settings.sentinel_grid_autoconnect or not _grid_credentials_configured():
        return 0

    # Excludes anything the operator explicitly disconnected from BOTH the
    # AUTO_MANAGED bookkeeping and the actual (re)connect loop below — an
    # earlier version of this fix only filtered the bookkeeping line, while
    # the loop still iterated the raw, unfiltered `eligible` list and started
    # a worker for any not-currently-running eligible camera regardless,
    # silently reconnecting a manually disconnected camera on the very next
    # sweep. Found via the live browser test of the Disconnect button.
    eligible = [cid for cid in _eligible_camera_ids(db) if cid not in OPERATOR_DISCONNECTED]
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
        # A real, rejected credential fails identically for every camera
        # (one shared grid login) — back off far longer than a plain
        # transient-failure retry so we don't hammer the login endpoint once
        # per camera per sweep.
        stats = worker.CAMERA_STATS.get(camera_id, {})
        floor = (
            settings.sentinel_grid_auth_cooldown_seconds
            if stats.get("grid_state") == "AUTH_ERROR"
            else _MIN_RESTART_INTERVAL_S
        )
        last_attempt = _last_restart_attempt.get(camera_id, 0.0)
        # `now` is a single timestamp taken once at the top of this sweep —
        # every camera the stagger below actually waited real seconds for
        # would otherwise still be compared against that same stale `now`,
        # letting the floor under-count real elapsed time. Re-read per
        # candidate instead; cheap (time.monotonic() is not a syscall).
        if time.monotonic() - last_attempt < floor:
            continue
        _last_restart_attempt[camera_id] = time.monotonic()
        worker.start_worker(camera_id)
        started += 1
        # Staggered startup: real delay between successive connection starts
        # within this sweep, not just between sweeps — this is what actually
        # spreads N simultaneous RTSP handshakes into a rollout. Skipped
        # after the last camera started (nothing left to space out) and
        # skipped entirely for a single-camera sweep.
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
    """Call once at application startup, after discover_and_register()."""
    global _supervisor_task
    if _supervisor_task is not None and not _supervisor_task.done():
        return
    _supervisor_task = asyncio.create_task(_sweep_loop())


async def stop_supervisor() -> None:
    """Call at application shutdown — cancels the sweep loop and stops every
    auto-managed worker cleanly, so no camera task or its underlying
    RTSP/cv2 resource is left orphaned at process exit."""
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
    for camera_id in list(AUTO_MANAGED):
        worker.stop_worker(camera_id)
    AUTO_MANAGED.clear()
    OPERATOR_DISCONNECTED.clear()


def connect(camera_id: str) -> None:
    """Explicit operator Connect: marks the camera auto-managed (so the
    supervisor keeps it alive/reconnects it going forward) and starts it."""
    OPERATOR_DISCONNECTED.discard(camera_id)
    AUTO_MANAGED.add(camera_id)
    _last_restart_attempt[camera_id] = time.monotonic()
    worker.start_worker(camera_id)


def disconnect(camera_id: str) -> None:
    """Explicit operator Disconnect: removed from the auto-managed set FIRST
    and marked operator-disconnected, so the next sweep does not immediately
    reconnect it, then the worker is actually stopped."""
    AUTO_MANAGED.discard(camera_id)
    OPERATOR_DISCONNECTED.add(camera_id)
    worker.stop_worker(camera_id)
