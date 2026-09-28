"""Self-Heal: recovery event log and open-problem index.

Doesn't do any recovering itself. The real recovery code is db_retry.py
(lock rollback + retry), worker.py / camera_connection.py (reconnect and
backoff), supervisor.py (24/7 reconnect sweep) and self_heal/http_retry.py.
This records what those do so operators see it in one place
(GET /api/self-heal/*, the SELF-HEAL UI).
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
# events table on every poll. Rebuilt from the DB at startup
# (rebuild_open_problems); the DB is the source of truth.
_LATEST: dict[tuple[str, str], "models.SelfHealEvent"] = {}

# latest status in here = not an open problem. anything else (RECOVERING,
# FAILED, CONFIG_REQUIRED, DEGRADED) is
_RESOLVED_STATUSES = {"RECOVERED"}

# A camera under steady lock contention can hit and recover a lock on almost
# every heartbeat commit, and Error Logs drowned in identical "recovered" rows.
# Repeats of the same (component, camera_id, error_type) RECOVERED event
# inside this window are skipped. Never FAILED, CONFIG_REQUIRED or critical
# events, and the first one of a burst is always written.
_DEDUP_WINDOW_S = 10.0
# Shared via app/runtime_state.py; a monotonic dict can't be read by another
# process and resets on restart, so the burst got written anyway.
#
# Sync is fine here: record_event_sync runs either in a caller that's already
# off the loop or via record_event's to_thread, so a blocking Redis call never
# hits the event loop.
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
    """Best-effort synchronous write, never raises. Own short-lived session so
    logging can't interfere with or get rolled back by the operation it
    describes. Losing the odd row under heavy contention is fine.

    None (nothing written) for a suppressed duplicate; record_event already
    treats None as nothing to broadcast."""
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
            pass
        return None
    finally:
        db.close()


async def record_event(**kwargs) -> "models.SelfHealEvent | None":
    """Write on a thread (like db_retry.py) and push it over the websocket so
    Recovery Activity / Problems update live."""
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
    """(error_type, severity) for a generic exception, for callers that don't
    know better (worker's per-iteration except). Anything unrecognized is
    UNKNOWN/warning rather than a guess."""
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
    """Startup: reload the latest event per (component, camera_id) from the
    last 24h so open problems survive a restart."""
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
    """Forget a deleted camera. Its SelfHealEvent rows go with it
    (routers/cameras.py), but this index kept the last one and the Problems
    page showed a camera that no longer existed until the next restart.
    """
    for key in [k for k in _LATEST if k[1] == camera_id]:
        _LATEST.pop(key, None)


def open_problems() -> list["models.SelfHealEvent"]:
    """Latest event per tracked (component, camera_id) whose status isn't
    resolved."""
    return [row for row in _LATEST.values() if row.status not in _RESOLVED_STATUSES]
