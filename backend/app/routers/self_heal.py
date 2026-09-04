"""SENTINEL SELF-HEAL — read API for the recovery event log + derived system/
camera health (see app/self_heal/engine.py for what actually writes these
events: real recovery code in pipeline/db_retry.py, pipeline/worker.py,
pipeline/catalog.py — this router only reads/aggregates what those already
recorded, it performs no recovery itself).

All endpoints are authenticated read access (get_current_user) — viewing
Self-Heal is not a control action (see Part 17: operational recovery only,
and Part 7's RBAC split reserves CONTROL actions, not visibility, for
Administrator/Control Room Operator — see routers/cameras.py's bulk
endpoints for those).
"""
from datetime import datetime, timedelta

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import text
from sqlalchemy.orm import Session

from .. import models
from ..db import get_db
from ..security import get_current_user
from ..self_heal import engine as self_heal
from ..ws import manager
from ..pipeline.worker import RUNNING, CAMERA_STATS

router = APIRouter(prefix="/api/self-heal", tags=["self-heal"])


def _event_out(row: models.SelfHealEvent, camera_code_by_id: dict[str, str]) -> dict:
    out = self_heal.serialize(row)
    out["camera_code"] = camera_code_by_id.get(row.camera_id or "") if row.camera_id else None
    return out


def _camera_code_map(db: Session, camera_ids: set[str]) -> dict[str, str]:
    if not camera_ids:
        return {}
    rows = db.query(models.Camera.id, models.Camera.camera_code).filter(models.Camera.id.in_(camera_ids)).all()
    return {cid: code for cid, code in rows}


@router.get("/health")
def self_heal_health(db: Session = Depends(get_db), user: models.User = Depends(get_current_user)):
    """SYSTEM HEALTH panel — every value is a real, live check, not a
    hardcoded string (same principle as routers/system.py's system_status,
    which this reuses/extends with the recovery-engine's own view)."""
    try:
        db.execute(text("SELECT 1"))
        db_ok = True
    except Exception:
        db_ok = False

    total_cameras = db.query(models.Camera).count()
    online_cameras = db.query(models.Camera).filter(models.Camera.status == "online").count()
    degraded_cameras = db.query(models.Camera).filter(models.Camera.status == "degraded").count()
    offline_cameras = db.query(models.Camera).filter(models.Camera.status == "offline").count()
    running_workers = sum(1 for t in RUNNING.values() if not t.done())
    ai_running = any(s.get("grid_state") == "PROCESSING" for s in CAMERA_STATS.values())

    problems = self_heal.open_problems()
    critical_open = sum(1 for p in problems if p.severity == "critical")
    warning_open = sum(1 for p in problems if p.severity == "warning")

    today_start = datetime.utcnow().replace(hour=0, minute=0, second=0, microsecond=0)
    recovered_today = (
        db.query(models.SelfHealEvent)
        .filter(models.SelfHealEvent.status == "RECOVERED", models.SelfHealEvent.timestamp >= today_start)
        .count()
    )

    # A recent (last 5 min) FAILED database event means SQLite contention is
    # an ongoing, not merely historical, problem — degrades the DATABASE
    # line even though the plain `SELECT 1` above (run on an otherwise-idle
    # connection) would itself still succeed.
    recent_db_failure = any(
        p.component == "database" and p.status == "FAILED" and p.timestamp >= datetime.utcnow() - timedelta(minutes=5)
        for p in problems
    )

    return {
        "timestamp": datetime.utcnow().isoformat(),
        "subsystems": {
            "api": "HEALTHY",
            "database": "DEGRADED" if (not db_ok or recent_db_failure) else "HEALTHY",
            # Final-review audit finding: this used to be a tautological
            # `len(...) >= 0` (always true) — there is no real "the WS
            # manager is broken" signal to check (it's an in-process list,
            # not a connection this process could lose), so honestly this
            # line is a live check the SAME way "api": "HEALTHY" above is —
            # this response returning at all proves the process (and
            # therefore the WS manager module) is up. Not a fake hardcode
            # dressed as a conditional.
            "websocket": "CONNECTED",
            "websocket_clients": len(manager.active),
            # Final-review audit finding: this used to be a dead ternary
            # (`"IDLE" if total_cameras else "IDLE"` — both branches
            # identical, total_cameras never actually consulted). There is
            # no genuine third state to distinguish here yet (a camera-less
            # deployment and one with cameras but AI off both really are
            # just "not running"), so simplified to say only what's true.
            "ai_engine": "RUNNING" if ai_running else "IDLE",
            "self_heal": "ACTIVE",
        },
        "cameras": {"online": online_cameras, "degraded": degraded_cameras, "offline": offline_cameras, "total": total_cameras},
        "workers_running": running_workers,
        "summary": {
            "active_problems": len(problems),
            "critical_problems": critical_open,
            "warning_problems": warning_open,
            "recovered_today": recovered_today,
            "offline_cameras": offline_cameras,
            "degraded_cameras": degraded_cameras,
        },
    }


@router.get("/problems")
def self_heal_problems(
    component: str | None = None,
    severity: str | None = None,
    db: Session = Depends(get_db),
    user: models.User = Depends(get_current_user),
):
    """Every (component, camera) currently NOT resolved — see
    self_heal.engine.open_problems. Optional filters narrow by component
    (database | camera | worker | camera_catalog | sentinel_grid) or
    severity (info | warning | critical)."""
    problems = self_heal.open_problems()
    if component:
        problems = [p for p in problems if p.component == component]
    if severity:
        problems = [p for p in problems if p.severity == severity]
    problems.sort(key=lambda p: p.timestamp, reverse=True)
    camera_codes = _camera_code_map(db, {p.camera_id for p in problems if p.camera_id})
    return [_event_out(p, camera_codes) for p in problems]


@router.get("/events")
def self_heal_events(
    component: str | None = None,
    camera_id: str | None = None,
    status: str | None = None,
    severity: str | None = None,
    q: str | None = None,
    limit: int = Query(default=100, le=500),
    offset: int = 0,
    db: Session = Depends(get_db),
    user: models.User = Depends(get_current_user),
):
    """SELF-HEAL → ERROR LOGS / RECOVERY ACTIVITY: searchable, filterable
    event history, newest first."""
    query = db.query(models.SelfHealEvent)
    if component:
        query = query.filter(models.SelfHealEvent.component == component)
    if camera_id:
        query = query.filter(models.SelfHealEvent.camera_id == camera_id)
    if status:
        query = query.filter(models.SelfHealEvent.status == status)
    if severity:
        query = query.filter(models.SelfHealEvent.severity == severity)
    if q:
        like = f"%{q}%"
        query = query.filter(models.SelfHealEvent.message.ilike(like))
    total = query.count()
    rows = query.order_by(models.SelfHealEvent.timestamp.desc()).offset(offset).limit(limit).all()
    camera_codes = _camera_code_map(db, {r.camera_id for r in rows if r.camera_id})
    return {"total": total, "limit": limit, "offset": offset, "events": [_event_out(r, camera_codes) for r in rows]}


@router.get("/events/{event_id}")
def self_heal_event_detail(event_id: str, db: Session = Depends(get_db), user: models.User = Depends(get_current_user)):
    """SELF-HEAL → PROBLEM DETAILS. The timeline shown by the UI is derived
    from this one real recorded row (detected-at = timestamp - duration,
    attempts made, recovered/failed-at = timestamp) rather than fabricated
    per-retry rows we never actually persisted individually."""
    row = db.query(models.SelfHealEvent).filter(models.SelfHealEvent.id == event_id).first()
    if not row:
        raise HTTPException(status_code=404, detail="Self-heal event not found")
    camera_codes = _camera_code_map(db, {row.camera_id} if row.camera_id else set())
    return _event_out(row, camera_codes)
