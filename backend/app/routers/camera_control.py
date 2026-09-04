"""CAMERA CONTROL CENTER — bulk camera operations (Self-Heal spec Parts 3-6).

Reuses the exact same per-camera actions the existing single-camera
endpoints already use (routers/cameras.py's start_camera/stop_camera/
restart_camera, pipeline.worker.start_worker/stop_worker,
pipeline.supervisor.connect/disconnect) — this router adds no new camera
lifecycle logic, only a bounded-concurrency loop over the existing one, plus
progress broadcast and one audit-log entry per bulk call.

Action semantics (mapped onto what this codebase actually has — no
fabricated states):
  connect   — open the camera's stream/worker; AI stays whatever it's
              already configured to (identical to POST /{id}/start).
  start     — alias of connect. This codebase has no real distinction
              between "connect" and "start" for a camera worker (both mean
              "make the stream flow") — aliased rather than inventing a
              fake difference.
  start_ai  — ensures the camera is connected, then enables AI
              (ai_person/ai_vehicle/ai_anpr = True). Never starts a second
              AI worker for an already-running camera (worker.py's
              start_worker/get_model already no-op/reuse per camera).
  stop      — disables AI (ai_person/ai_vehicle/ai_anpr = False) while
              KEEPING the stream connected — "AI STOPPED", camera stays
              online. Distinct from disconnect.
  restart   — disconnect then reconnect (identical to POST /{id}/restart).
  disconnect— fully stops the worker (identical to POST /{id}/stop).
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
from ..audit import log_action
from ..ws import manager
from ..pipeline.worker import start_worker, stop_worker
from ..pipeline import supervisor

router = APIRouter(prefix="/api/cameras/bulk", tags=["camera-control"])

BulkAction = Literal["connect", "start", "start_ai", "restart", "stop", "disconnect"]

# Disruptive actions the frontend must show a confirmation dialog for
# (Part 4) — exposed so the UI doesn't have to hardcode its own copy of
# this list.
DISRUPTIVE_ACTIONS = {"restart", "disconnect", "stop"}

MAX_CONCURRENT = 5  # bounded batching (Part 4) — never fire dozens of simultaneous operations

# Duplicate-click / overlapping-bulk-op guard: camera_ids currently being
# acted on by ANY in-flight bulk call. A camera already in here is skipped
# (not double-actioned) rather than racing two bulk operations against the
# same worker. In-memory, process-local — cleared as each camera finishes,
# regardless of the outcome.
_IN_PROGRESS: set[str] = set()


class BulkRequest(BaseModel):
    action: BulkAction
    camera_ids: list[str] | None = None  # None/omitted = every registered camera


def _set_ai(db: Session, camera: models.Camera, enabled: bool) -> None:
    camera.ai_person = enabled  # type: ignore[assignment]
    camera.ai_vehicle = enabled  # type: ignore[assignment]
    camera.ai_anpr = enabled  # type: ignore[assignment]
    db.commit()


async def _apply_one(action: BulkAction, camera_id: str) -> dict:
    """Runs one camera's action against a SHORT-LIVED session of its own
    (never the request's shared session — these run concurrently under the
    semaphore below) and never raises; every outcome (including an
    exception) becomes a result dict so one camera's failure can never abort
    the batch."""
    db = SessionLocal()
    try:
        camera = db.query(models.Camera).filter(models.Camera.id == camera_id).first()
        if not camera:
            return {"camera_id": camera_id, "camera_code": None, "ok": False, "skipped": False, "detail": "Camera not found"}
        code = str(camera.camera_code)

        if action in ("connect", "start"):
            if camera.source_type == "sentinel_grid":
                supervisor.connect(camera_id)
            else:
                start_worker(camera_id)
            detail = "Connected"
        elif action == "start_ai":
            if camera.source_type == "sentinel_grid":
                supervisor.connect(camera_id)
            else:
                start_worker(camera_id)
            _set_ai(db, camera, True)
            detail = "AI started"
        elif action == "stop":
            _set_ai(db, camera, False)
            detail = "AI stopped"
        elif action == "restart":
            stop_worker(camera_id)
            start_worker(camera_id)
            detail = "Restarted"
        elif action == "disconnect":
            if camera.source_type == "sentinel_grid":
                supervisor.disconnect(camera_id)
            else:
                stop_worker(camera_id)
            camera.status = "offline"  # type: ignore[assignment]
            db.commit()
            detail = "Disconnected"
        else:
            return {"camera_id": camera_id, "camera_code": code, "ok": False, "skipped": False, "detail": f"Unknown action {action}"}

        return {"camera_id": camera_id, "camera_code": code, "ok": True, "skipped": False, "detail": detail}
    except Exception as exc:
        return {"camera_id": camera_id, "camera_code": None, "ok": False, "skipped": False, "detail": f"{type(exc).__name__}: {exc}"}
    finally:
        db.close()


@router.post("")
async def bulk_camera_action(
    payload: BulkRequest,
    db: Session = Depends(get_db),
    user: models.User = Depends(require_roles("Administrator", "Control Room Operator")),
):
    if payload.camera_ids is not None:
        target_ids = payload.camera_ids
    else:
        target_ids = [c.id for c in db.query(models.Camera.id).all()]
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
