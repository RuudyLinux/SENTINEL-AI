"""Self-Heal read API: the recovery event log and derived system/camera health.
Events are written by self_heal/engine.py; this router only reads. Any logged-in
user may read it.
"""
from datetime import datetime, timedelta

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import text
from sqlalchemy.orm import Session

from .. import models
from ..db import LIKE_ESCAPE, get_db, like_pattern
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
    return dict(rows)


@router.get("/health")
def self_heal_health(db: Session = Depends(get_db), user: models.User = Depends(get_current_user)):
    """System health panel. Every value is a live check (like
    routers/system.py's system_status) plus the recovery engine's view."""
    try:
        db.execute(text("SELECT 1"))
        db_ok = True
    except Exception:
        db_ok = False

    active = db.query(models.Camera).filter(models.Camera.retired == False)  # noqa: E712  (retired = history, not fleet)
    total_cameras = active.count()
    online_cameras = active.filter(models.Camera.status == "online").count()
    degraded_cameras = active.filter(models.Camera.status == "degraded").count()
    offline_cameras = active.filter(models.Camera.status == "offline").count()
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

    # a FAILED database event in the last 5 min means contention is ongoing,
    # so degrade DATABASE even though SELECT 1 on an idle connection passes
    recent_db_failure = any(
        p.component == "database" and p.status == "FAILED" and p.timestamp >= datetime.utcnow() - timedelta(minutes=5)
        for p in problems
    )

    return {
        "timestamp": datetime.utcnow().isoformat(),
        "subsystems": {
            "api": "HEALTHY",
            "database": "DEGRADED" if (not db_ok or recent_db_failure) else "HEALTHY",
            # There's nothing that can break about the WS manager (in-process
            # list), so like "api" above, answering at all is the check.
            "websocket": "CONNECTED",
            "websocket_clients": len(manager.active),
            # no real third state yet: no cameras and cameras with AI off
            # are both just not running
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
    """Every (component, camera) not resolved (engine.open_problems).
    Filter by component (database | camera | worker | camera_catalog |
    sentinel_grid) or severity (info | warning | critical)."""
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
    # ge=1 matters: SQLite reads LIMIT -1 as no limit, and ?limit=-1 handed
    # any logged-in user the whole table (tests/test_list_limits.py)
    limit: int = Query(default=100, ge=1, le=500),
    offset: int = 0,
    db: Session = Depends(get_db),
    user: models.User = Depends(get_current_user),
):
    """Error logs / recovery activity: searchable event history, newest first."""
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
        query = query.filter(models.SelfHealEvent.message.ilike(like_pattern(q), escape=LIKE_ESCAPE))
    total = query.count()
    rows = query.order_by(models.SelfHealEvent.timestamp.desc()).offset(offset).limit(limit).all()
    camera_codes = _camera_code_map(db, {r.camera_id for r in rows if r.camera_id})
    return {"total": total, "limit": limit, "offset": offset, "events": [_event_out(r, camera_codes) for r in rows]}


@router.get("/events/{event_id}")
def self_heal_event_detail(event_id: str, db: Session = Depends(get_db), user: models.User = Depends(get_current_user)):
    """Problem details. The UI derives its timeline from this one row (detected =
    timestamp - duration)."""
    row = db.query(models.SelfHealEvent).filter(models.SelfHealEvent.id == event_id).first()
    if not row:
        raise HTTPException(status_code=404, detail="Self-heal event not found")
    camera_codes = _camera_code_map(db, {row.camera_id} if row.camera_id else set())
    return _event_out(row, camera_codes)
