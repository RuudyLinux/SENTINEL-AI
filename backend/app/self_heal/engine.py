"""Sentinel Self-Heal — centralized recovery event log + open-problem index
for the platform's REAL recovery mechanisms.

This module deliberately does NOT implement a second, parallel recovery
system. The project already has real, tested recovery code:
  - pipeline/db_retry.py (safe_commit/safe_flush) — SQLite lock rollback+retry
  - pipeline/worker.py (_reopen_with_backoff, read-failure counter) — camera
    reconnect/backoff and stream-drop handling
  - pipeline/supervisor.py — 24/7 reconnect sweep
  - self_heal/http_retry.py — bounded retry for this app's own outbound HTTP

Part 17 of the Self-Heal spec is explicit that Self-Heal must be
OPERATIONAL RECOVERY ONLY — this module OBSERVES and LOGS the recovery
paths above (so operators have one place to see them: GET /api/self-heal/*
and the SELF-HEAL UI), it never re-implements or overrides them.
"""
import asyncio
import logging
import time
from datetime import datetime, timedelta
from typing import Any

from .. import models
from ..db import SessionLocal
from ..ws import manager

logger = logging.getLogger("sentinel.self_heal")

# Components with no camera_id use this key in _LATEST below.
_GLOBAL = "_global"

# In-memory "is this thing currently broken" index — one entry per
# (component, camera_id) key, holding the most recent event for that key.
# Powers GET /api/self-heal/problems without re-scanning the whole events
# table on every poll. Rebuilt from the DB at startup (rebuild_open_problems)
# so a backend restart doesn't lose real open problems. Diagnostic/UI state
# only — never the source of truth (the DB row is), safe to lose on crash.
_LATEST: dict[tuple[str, str], "models.SelfHealEvent"] = {}

# Terminal statuses — a component/camera whose latest event has one of these
# is NOT an open problem. Anything else (RECOVERING, FAILED, CONFIG_REQUIRED,
# DEGRADED) is.
_RESOLVED_STATUSES = {"RECOVERED"}

# Noisy-duplicate suppression (audit review finding): a camera under
# sustained-but-transient lock contention can hit-and-recover a lock on
# nearly every heartbeat commit — each one individually correct to log, but
# a real operator's Error Logs/Recovery Activity page would drown in
# identical "recovered" rows for the SAME ongoing condition. Suppresses a
# REPEAT of the exact same (component, camera_id, error_type) RECOVERED
# event within this window — never applied to a FAILED/CONFIG_REQUIRED
# event or a critical-severity one, so a genuine, still-unresolved failure
# is never hidden by this. The DB row for the first occurrence in a burst
# always persists; only the immediate repeats are skipped.
_DEDUP_WINDOW_S = 10.0
_last_recovered_at: dict[tuple[str, str, str], float] = {}


def _key(component: str, camera_id: str | None) -> tuple[str, str]:
    return (component, camera_id or _GLOBAL)


def _is_noisy_duplicate(component: str, camera_id: str | None, error_type: str, status: str, severity: str) -> bool:
    if status != "RECOVERED" or severity == "critical":
        return False
    dedup_key = (component, camera_id or _GLOBAL, error_type)
    now = time.monotonic()
    last = _last_recovered_at.get(dedup_key, 0.0)
    if now - last < _DEDUP_WINDOW_S:
        return True
    _last_recovered_at[dedup_key] = now
    return False


def record_event_sync(
    *, component: str, error_type: str, message: str,
    camera_id: str | None = None, severity: str = "warning",
    recovery_action: str = "", attempt: int = 1, max_attempts: int = 1,
    status: str = "RECOVERED", duration_seconds: float = 0.0,
    endpoint: str = "", metadata: dict[str, Any] | None = None,
) -> "models.SelfHealEvent | None":
    """Best-effort, synchronous write — never raises. Uses its OWN
    short-lived session so recording a self-heal event can never interfere
    with (or get rolled back by) the transaction of the real operation it's
    describing. Losing an occasional self-heal row under extreme contention
    is acceptable; the operation it describes already succeeded/failed on
    its own, independently of this log entry.

    Returns None (no row written) for a suppressed noisy-duplicate RECOVERED
    event — see _is_noisy_duplicate above. Callers already treat None as
    "no event to broadcast" (record_event), so this needs no special
    handling at any call site."""
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
    """Async wrapper — offloads the blocking DB write to a thread (same
    reasoning as db_retry.py) and broadcasts over the existing WebSocket so
    the Recovery Activity / Problems pages update live, no polling."""
    row = await asyncio.to_thread(record_event_sync, **kwargs)
    if row is not None:
        try:
            await manager.broadcast("self_heal_event", serialize(row))
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
    """Best-effort (error_type, severity) for a generic caught exception —
    for call sites that don't already know a more specific type (e.g.
    worker.py's outer per-iteration except). Narrow and honest: anything not
    recognized is UNKNOWN/warning, never guessed into a specific category
    it might not actually be."""
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
    last 24h so GET /api/self-heal/problems reflects real state across a
    restart, not just this process's in-memory history since boot."""
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


def open_problems() -> list["models.SelfHealEvent"]:
    """Every tracked (component, camera_id)'s latest event, where that
    latest status is not a resolved one — i.e. genuinely still open."""
    return [row for row in _LATEST.values() if row.status not in _RESOLVED_STATUSES]
