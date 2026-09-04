"""Shared db.commit() retry/backoff helper for the camera pipeline
(worker.py + rules_engine.py both write to the same SQLite file from
concurrently-running per-camera asyncio tasks, plus FastAPI's sync route
handlers on their own threadpool threads — see db.py for the WAL +
busy_timeout config this builds on).

Real bug this fixes: `sqlite3.OperationalError: database is locked` reaching
Python at all already means SQLite's own `busy_timeout` (db.py, 30s) gave up
waiting — a genuinely contended commit, not a sub-second blip — and the
previous code (a single try/commit/except/rollback) simply gave up right
there, discarding whatever was pending.

Why a bare "retry commit() in a loop" is WRONG (verified empirically before
writing this, against a real on-disk SQLite file with a genuine second
connection holding a competing write lock — see scratchpad
lock_experiment*.py from this session): once `db.commit()` raises, the
session's transaction is unusable — the *next* call MUST be `db.rollback()`,
or any further use raises `sqlalchemy.exc.PendingRollbackError`. And
`rollback()` is destructive to the very state a plain retry would need:
- A still-pending (`db.add()`-ed, never-committed) object is detached from
  the session's write-set by rollback, though its already-set Python
  attribute values (including a client-side-generated primary key, e.g.
  `models.Detection.id`'s `default=lambda: uid(...)`) survive untouched in
  memory (verified: PK stayed identical across rollback+retry, and a foreign
  key captured from it before the failed commit still linked correctly to
  the retried row).
- An already-persistent object's mutated attributes are *expired* by
  rollback and silently revert to their last-committed DB value on next
  read — re-reading `camera.status` after rollback to "reapply" it would
  just reapply the OLD value.

So a correct retry must, after each rollback, "reapply" the exact pending
write — re-`db.add()` a still-transient object (its field values are already
correct in memory) and/or re-assign a persistent object's attribute *from a
value captured before the first attempt*, never from re-reading the object.
`reapply` below is exactly that hook; the retry loop itself only replays it.
"""
import asyncio
import logging
from typing import Callable

from sqlalchemy.exc import OperationalError
from sqlalchemy.orm import Session

logger = logging.getLogger("sentinel.worker")

# SQLite's own message for this transient condition; matched loosely (not
# with a code) since sqlite3 doesn't expose one for it. Deliberately narrow —
# any OTHER OperationalError (a real schema/constraint/misuse bug) must
# never be silently retried or hidden.
_LOCK_MARKERS = ("locked", "busy")


def _is_lock_error(exc: OperationalError) -> bool:
    msg = str(exc).lower()
    return any(marker in msg for marker in _LOCK_MARKERS)


async def safe_commit(
    db: Session,
    label: str,
    reapply: Callable[[], None] | None = None,
    max_attempts: int = 4,
) -> bool:
    """A db.commit() that can never itself throw and kill the calling task.

    Returns True on success, False if it ultimately gave up (already rolled
    back — the caller decides whether that's fatal for this iteration; the
    per-iteration/per-request guards around every call site already treat a
    commit failure as non-fatal, matching the pre-existing behavior for a
    caller that doesn't need retry).

    Retries (rollback -> reapply -> short backoff -> commit again) ONLY when
    both (a) the error is specifically a lock/busy condition and (b) a
    `reapply` callback was given. Without `reapply`, this is a single
    attempt — identical to the pre-fix behavior — because retrying a bare
    commit with nothing re-added/re-assigned after a rollback would just be
    a no-op that reports success while having silently lost the write, which
    is worse than today's visible, logged failure.

    The actual commit()/rollback() calls are offloaded via asyncio.to_thread:
    they block synchronously on SQLite's busy_timeout wait, and running that
    on the event loop thread would stall every other camera's asyncio task
    sharing it for the whole wait.
    """
    attempts = max_attempts if reapply is not None else 1
    for attempt in range(1, attempts + 1):
        try:
            await asyncio.to_thread(db.commit)
            return True
        except OperationalError as exc:
            is_lock = _is_lock_error(exc)
            if is_lock:
                logger.warning(
                    "%s: commit hit a locked database (attempt %d/%d)%s",
                    label, attempt, attempts, "" if attempt < attempts else " — giving up",
                )
            else:
                logger.exception("%s: commit failed (not a lock — not retrying)", label)
        except Exception:
            logger.exception("%s: commit failed", label)
            is_lock = False

        try:
            await asyncio.to_thread(db.rollback)
        except Exception:
            logger.exception("%s: rollback after failed commit also failed", label)
            return False

        if is_lock and reapply is not None and attempt < attempts:
            try:
                reapply()
            except Exception:
                logger.exception("%s: reapply before commit retry failed", label)
                return False
            await asyncio.sleep(min(0.5, 0.05 * (2 ** (attempt - 1))))
            continue
        return False
    return False
