"""Commit/flush wrappers for the camera loop that log lock recoveries as
Self-Heal events.
"""
from sqlalchemy.orm import Session

from .db_retry import safe_commit, safe_flush
from ..self_heal import engine as self_heal
from .camera_state import CAMERA_STATS


def _self_heal_camera_id(camera_code: str) -> str | None:
    # CAMERA_STATS is keyed by camera.id; this reverse lookup only runs after
    # a lock was hit, never on the hot path
    for cid, stats in CAMERA_STATS.items():
        if stats.get("camera_code") == camera_code:
            return cid
    return None


def _db_self_heal_on_result(camera_code: str, op_name: str):
    """on_result hook for safe_commit/safe_flush; logs only when a lock actually
    occurred."""
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
    """db_retry.safe_commit labelled with the camera (see db_retry for the
    retry-with-reapply rules)."""
    return await safe_commit(db, f"camera {camera_code}", reapply=reapply, on_result=_db_self_heal_on_result(camera_code, "commit"))


async def _safe_flush(db: Session, camera_code: str, reapply=None) -> bool:
    """Same for db.flush()."""
    return await safe_flush(db, f"camera {camera_code}", reapply=reapply, on_result=_db_self_heal_on_result(camera_code, "flush"))
