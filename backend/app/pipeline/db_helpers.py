"""Self-heal-aware DB commit/flush wrappers, extracted from worker.py.

Every camera worker commit and flush goes through these rather than calling
db_retry.safe_commit/safe_flush directly, so a transient SQLite lock during
retry is recorded as a Self-Heal event automatically -- callers throughout
the camera loop, the connection/reconnect logic, and frame processing get
that for free by using `_safe_commit`/`_safe_flush` instead of the bare
db_retry functions.
"""
from sqlalchemy.orm import Session

from .db_retry import safe_commit, safe_flush
from ..self_heal import engine as self_heal
from .camera_state import CAMERA_STATS


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
