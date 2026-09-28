"""Camera Control Center: bulk camera operations.

Runs the same per-camera actions as the single-camera endpoints, with bounded
concurrency, progress broadcasts and one audit entry per call.

Actions:
  connect    open the stream/worker; AI unchanged (= POST /{id}/start)
  start      same as connect
  start_ai   connect if needed, then enable ai_person/ai_vehicle/ai_anpr
  stop       disable AI; the stream stays connected
  restart    disconnect then reconnect (= POST /{id}/restart)
  disconnect stop the worker (= POST /{id}/stop)
"""
import asyncio
import uuid
from typing import Literal

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy.orm import Session

from .. import models
from ..db import get_db, SessionLocal
from ..security import require_roles
from ..pipeline import ai_capacity
from ..audit import log_action
from ..pipeline.db_retry import close_session
from ..ws import manager
from ..pipeline.worker import start_worker, stop_worker
from ..pipeline import supervisor
from ..pipeline.db_retry import safe_commit

router = APIRouter(prefix="/api/cameras/bulk", tags=["camera-control"])

BulkAction = Literal["connect", "start", "start_ai", "restart", "stop", "disconnect"]

# actions the UI must confirm; exposed so it doesn't keep its own copy
DISRUPTIVE_ACTIONS = {"restart", "disconnect", "stop"}

MAX_CONCURRENT = 5  # never fire dozens of operations at once

# Cameras an in-flight bulk call is working on; overlapping calls skip them
# rather than race on one worker.
_IN_PROGRESS: set[str] = set()


class BulkRequest(BaseModel):
    action: BulkAction
    camera_ids: list[str] | None = None  # None/omitted = every registered camera


async def _set_ai(db: Session, camera: models.Camera, enabled: bool, camera_code: str) -> bool:
    """Commit through safe_commit, since a bulk call runs several concurrent
    writers."""
    camera.ai_person = enabled  # type: ignore[assignment]
    camera.ai_vehicle = enabled  # type: ignore[assignment]
    camera.ai_anpr = enabled  # type: ignore[assignment]

    def reapply():
        camera.ai_person = enabled  # type: ignore[assignment]
        camera.ai_vehicle = enabled  # type: ignore[assignment]
        camera.ai_anpr = enabled  # type: ignore[assignment]

    return await safe_commit(db, f"bulk camera {camera_code}", reapply=reapply)


async def _apply_one(action: BulkAction, camera_id: str) -> dict:
    """Run one camera's action on its own short-lived session. Never raises:
    every outcome is a result dict, so one failure can't abort the batch."""
    db = SessionLocal()
    try:
        camera = db.query(models.Camera).filter(models.Camera.id == camera_id).first()
        if not camera:
            return {"camera_id": camera_id, "camera_code": None, "ok": False, "skipped": False, "detail": "Camera not found"}
        code = str(camera.camera_code)
        if bool(camera.retired) and action in ("connect", "start", "start_ai", "restart"):
            return {"camera_id": camera_id, "camera_code": code, "ok": False, "skipped": True, "detail": "Camera is retired"}

        ai_wanted = bool(camera.ai_person) or bool(camera.ai_vehicle)
        if action in ("connect", "start"):
            if camera.source_type == "sentinel_grid":
                supervisor.connect(camera_id)
            else:
                start_worker(camera_id)
            refusal = ai_capacity.blocked(camera_id) if ai_wanted else None
            if ai_wanted and refusal is None:
                ai_capacity.try_acquire(camera_id)  # reserve now, so two quick starts cannot both pass
            detail = "Connected" if refusal is None else f"Connected without AI — {refusal}"
        elif action == "start_ai":
            refusal = ai_capacity.blocked(camera_id)
            if refusal is not None:
                return {"camera_id": camera_id, "camera_code": code, "ok": False, "skipped": False, "detail": refusal}
            got_slot = ai_capacity.try_acquire(camera_id)
            if camera.source_type == "sentinel_grid":
                supervisor.connect(camera_id)
            else:
                start_worker(camera_id)
            if not await _set_ai(db, camera, True, code):
                return {"camera_id": camera_id, "camera_code": code, "ok": False, "skipped": False, "detail": "Connected, but AI-enable write did not persist (database busy) — retry"}
            detail = "AI started" if got_slot else "AI on, waiting for its turn on an AI slot"
        elif action == "stop":
            ai_capacity.release(camera_id)
            if not await _set_ai(db, camera, False, code):
                return {"camera_id": camera_id, "camera_code": code, "ok": False, "skipped": False, "detail": "AI-disable write did not persist (database busy) — retry"}
            detail = "AI stopped"
        elif action == "restart":
            # supervisor.restart keeps the grid bookkeeping right, same as the
            # single-camera restart endpoint
            await supervisor.restart(camera_id, str(camera.source_type))
            detail = "Restarted"
        elif action == "disconnect":
            if camera.source_type == "sentinel_grid":
                supervisor.disconnect(camera_id)
            else:
                stop_worker(camera_id)
            camera.status = "offline"  # type: ignore[assignment]

            def _reapply_offline():
                camera.status = "offline"  # type: ignore[assignment]

            if not await safe_commit(db, f"bulk camera {code}", reapply=_reapply_offline):
                return {"camera_id": camera_id, "camera_code": code, "ok": False, "skipped": False, "detail": "Worker stopped, but status write did not persist (database busy) — retry"}
            detail = "Disconnected"
        else:
            return {"camera_id": camera_id, "camera_code": code, "ok": False, "skipped": False, "detail": f"Unknown action {action}"}

        return {"camera_id": camera_id, "camera_code": code, "ok": True, "skipped": False, "detail": detail}
    except Exception as exc:
        return {"camera_id": camera_id, "camera_code": None, "ok": False, "skipped": False, "detail": f"{type(exc).__name__}: {exc}"}
    finally:
        # close_session waits for a commit still running in a thread.
        close_session(db)


@router.post("")
async def bulk_camera_action(
    payload: BulkRequest,
    db: Session = Depends(get_db),
    user: models.User = Depends(require_roles("Administrator", "Control Room Operator")),
):
    if payload.camera_ids is not None:
        target_ids = payload.camera_ids
    else:
        target_ids = [c.id for c in db.query(models.Camera.id).filter(models.Camera.retired == False).all()]  # noqa: E712
    if not target_ids:
        raise HTTPException(status_code=400, detail="No cameras to operate on")

    op_id = f"bulkop_{uuid.uuid4().hex[:10]}"
    semaphore = asyncio.Semaphore(MAX_CONCURRENT)
    total = len(target_ids)
    completed = 0
    results: list[dict] = []

    async def _run(camera_id: str) -> dict:
        nonlocal completed
        if camera_id in _IN_PROGRESS:
            result = {"camera_id": camera_id, "camera_code": None, "ok": False, "skipped": True, "detail": "Already in progress"}
        else:
            _IN_PROGRESS.add(camera_id)
            async with semaphore:
                try:
                    result = await _apply_one(payload.action, camera_id)
                finally:
                    _IN_PROGRESS.discard(camera_id)
        completed += 1
        await manager.broadcast("bulk_progress", {
            "op_id": op_id, "action": payload.action, "completed": completed, "total": total, "result": result,
        })
        return result

    results = await asyncio.gather(*(_run(cid) for cid in target_ids))

    successful = sum(1 for r in results if r["ok"])
    failed = sum(1 for r in results if not r["ok"] and not r["skipped"])
    skipped = sum(1 for r in results if r["skipped"])

    log_action(
        db, user, f"bulk_{payload.action}_cameras",
        resource=f"{total} cameras: {successful} ok, {failed} failed, {skipped} skipped",
    )
    await manager.broadcast("bulk_complete", {
        "op_id": op_id, "action": payload.action, "total": total,
        "successful": successful, "failed": failed, "skipped": skipped,
    })

    return {
        "op_id": op_id, "action": payload.action, "total": total,
        "successful": successful, "failed": failed, "skipped": skipped,
        "results": results,
    }


@router.get("/disruptive-actions")
def list_disruptive_actions(user: models.User = Depends(require_roles("Administrator", "Control Room Operator"))):
    return sorted(DISRUPTIVE_ACTIONS)
