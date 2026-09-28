"""Self-Heal: recovery event log and open-problem index.

Records what the recovery code does (db_retry.py lock retries, camera
reconnect/backoff, the grid supervisor, http_retry.py) so operators see it in
one place (GET /api/self-heal/*). It performs no recovery itself.
"""
import asyncio
import logging
from datetime import datetime, timedelta
from typing import Any

from .. import models, metrics
from ..db import SessionLocal
from .. import runtime_state
from ..config import settings
from ..ws import manager, EventType

logger = logging.getLogger("sentinel.self_heal")

# Components with no camera_id use this key in _LATEST below.
_GLOBAL = "_global"

# Latest event per (component, camera_id), so /problems doesn't rescan the
# table on every poll. Rebuilt from the DB at startup.
_LATEST: dict[tuple[str, str], "models.SelfHealEvent"] = {}

# latest status in here = not an open problem. anything else (RECOVERING,
# FAILED, CONFIG_REQUIRED, DEGRADED) is
_RESOLVED_STATUSES = {"RECOVERED"}

# Repeats of the same RECOVERED event (component, camera_id, error_type) inside
# this window are skipped, so steady lock contention doesn't flood the log.
# FAILED, CONFIG_REQUIRED and critical events are always written.
_DEDUP_WINDOW_S = 10.0
# Shared through runtime_state.py so the window holds across processes and
# restarts. Synchronous calls are fine: record_event_sync never runs on the
# event loop.
_recovered_claims = runtime_state.build_claims_store("self_heal_dedup", settings)


def _key(component: str, camera_id: str | None) -> tuple[str, str]:
    return (component, camera_id or _GLOBAL)


def _is_noisy_duplicate(component: str, camera_id: str | None, error_type: str, status: str, severity: str) -> bool:
    if status != "RECOVERED" or severity == "critical":
        return False
    # A database lock is one condition however many cameras hit it; with 30
    # cameras a storm wrote 30 rows, each a write fighting the same lock.
    scope = _GLOBAL if component == "database" else (camera_id or _GLOBAL)
    dedup_key = (component, scope, error_type)
    return not _recovered_claims.claim(dedup_key, _DEDUP_WINDOW_S)


def record_event_sync(
    *, component: str, error_type: str, message: str,
    camera_id: str | None = None, severity: str = "warning",
    recovery_action: str = "", attempt: int = 1, max_attempts: int = 1,
    status: str = "RECOVERED", duration_seconds: float = 0.0,
    endpoint: str = "", metadata: dict[str, Any] | None = None,
) -> "models.SelfHealEvent | None":
    """Best-effort synchronous write that never raises. Uses its own session so
    it can't interfere with, or be rolled back by, the operation it describes.
    Returns None for a suppressed duplicate."""
    if _is_noisy_duplicate(component, camera_id, error_type, status, severity):
        return None
    db = SessionLocal()
    try:
        row = models.SelfHealEvent(
            component=component, camera_id=camera_id, error_type=error_type,
            severity=severity, message=(message or "")[:2000], recovery_action=recovery_action,
            attempt=attempt, max_attempts=max_attempts, status=status,
            duration_seconds=duration_seconds, endpoint=endpoint,
            event_metadata=metadata or {},
        )
        db.add(row)
        db.commit()
        db.refresh(row)
        _LATEST[_key(component, camera_id)] = row
        return row
    except Exception:
        logger.exception(
            "self-heal: failed to record event (component=%s, error_type=%s) — continuing", component, error_type,
        )
        try:
            db.rollback()
        except Exception:
            pass  # best effort; the failure is already logged above
        return None
    finally:
        db.close()


async def record_event(**kwargs) -> "models.SelfHealEvent | None":
    """Write on a thread and broadcast the event so the Self-Heal screens update
    live."""
    row = await asyncio.to_thread(record_event_sync, **kwargs)
    if row is not None:
        try:
            # publish sends self_heal.recovery plus the legacy self_heal_event
            # name the existing pages listen for
            metrics.SELF_HEAL_EVENTS.labels(
                component=str(row.component or "unknown"), status=str(row.status or "unknown"),
            ).inc()
            await manager.publish(EventType.SELF_HEAL_RECOVERY, serialize(row))
        except Exception:
            logger.exception("self-heal: broadcast failed, continuing")
    return row


def serialize(row: "models.SelfHealEvent") -> dict[str, Any]:
    return {
        "id": row.id, "timestamp": row.timestamp.isoformat() if row.timestamp else None,
        "component": row.component, "camera_id": row.camera_id, "error_type": row.error_type,
        "severity": row.severity, "message": row.message, "recovery_action": row.recovery_action,
        "attempt": row.attempt, "max_attempts": row.max_attempts, "status": row.status,
        "duration_seconds": row.duration_seconds, "endpoint": row.endpoint,
        "metadata": row.event_metadata or {},
    }


def classify_exception(exc: BaseException) -> tuple[str, str]:
    """(error_type, severity) for a generic exception. Anything unrecognised is
    UNKNOWN/warning."""
    name = type(exc).__name__
    msg = str(exc).lower()
    if "operational" in name.lower() and ("locked" in msg or "busy" in msg):
        return "SQLITE_LOCK", "warning"
    if isinstance(exc, asyncio.TimeoutError) or "timeout" in msg:
        return "TIMEOUT", "warning"
    if isinstance(exc, ConnectionError) or "connection" in msg:
        return "CONNECTION_ERROR", "warning"
    return "UNKNOWN", "warning"


def rebuild_open_problems() -> None:
    """Reload the latest event per (component, camera_id) from the last 24 h so
    open problems survive a restart."""
    db = SessionLocal()
    try:
        cutoff = datetime.utcnow() - timedelta(hours=24)
        rows = (
            db.query(models.SelfHealEvent)
            .filter(models.SelfHealEvent.timestamp >= cutoff)
            .order_by(models.SelfHealEvent.timestamp.asc())
            .all()
        )
        for row in rows:
            _LATEST[_key(row.component, row.camera_id)] = row
        logger.info("self-heal: rebuilt open-problem index from %d event(s) in the last 24h", len(rows))
    except Exception:
        logger.exception("self-heal: rebuild_open_problems failed, continuing with empty state")
    finally:
        db.close()


def forget_camera(camera_id: str) -> None:
    """Forget a deleted camera so it no longer appears under open problems."""
    for key in [k for k in _LATEST if k[1] == camera_id]:
        _LATEST.pop(key, None)


def open_problems() -> list["models.SelfHealEvent"]:
    """Latest event per tracked (component, camera_id) whose status isn't
    resolved."""
    return [row for row in _LATEST.values() if row.status not in _RESOLVED_STATUSES]
