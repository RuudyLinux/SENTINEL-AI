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
import time
from typing import Awaitable, Callable

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


async def _safe_write(
    db: Session,
    op_name: str,
    op: Callable[[], None],
    label: str,
    reapply: Callable[[], None] | None,
    max_attempts: int,
    on_result: "Callable[[int, int, bool, bool, float], Awaitable[None]] | None" = None,
) -> bool:
    """Shared retry body for both safe_commit and safe_flush below — a lock
    on `db.flush()` (which itself issues real INSERT/UPDATE statements
    against SQLite, same as commit) is the identical failure mode with the
    identical fix, just at a different point in the transaction. See this
    module's docstring for why a bare retry loop is wrong and what
    `reapply` must do.

    The actual flush()/commit()/rollback() calls are offloaded via
    asyncio.to_thread: they block synchronously on SQLite's busy_timeout
    wait, and running that on the event loop thread would stall every other
    camera's asyncio task sharing it for the whole wait.

    `on_result`, if given, is awaited exactly once right before returning,
    as `(final_attempt, max_attempts, success, was_lock_error, duration_s)`
    — purely observational (e.g. self_heal.engine.record_event); this
    function's own retry/rollback/return behavior never depends on it, and
    a failure inside it is caught by the caller (self_heal's own record_event
    is itself best-effort/never-raises), never here.
    """
    started = time.monotonic()
    attempts = max_attempts if reapply is not None else 1
    ever_lock = False
    for attempt in range(1, attempts + 1):
        try:
            await asyncio.to_thread(op)
            if on_result is not None:
                await on_result(attempt, attempts, True, ever_lock, time.monotonic() - started)
            return True
        except OperationalError as exc:
            is_lock = _is_lock_error(exc)
            ever_lock = ever_lock or is_lock
            if is_lock:
                logger.warning(
                    "%s: %s hit a locked database (attempt %d/%d)%s",
                    label, op_name, attempt, attempts, "" if attempt < attempts else " — giving up",
                )
            else:
                logger.exception("%s: %s failed (not a lock — not retrying)", label, op_name)
        except Exception:
            logger.exception("%s: %s failed", label, op_name)
            is_lock = False

        try:
            await asyncio.to_thread(db.rollback)
        except Exception:
            logger.exception("%s: rollback after failed %s also failed", label, op_name)
            if on_result is not None:
                await on_result(attempt, attempts, False, ever_lock, time.monotonic() - started)
            return False

        if is_lock and reapply is not None and attempt < attempts:
            try:
                reapply()
            except Exception:
                logger.exception("%s: reapply before %s retry failed", label, op_name)
                if on_result is not None:
                    await on_result(attempt, attempts, False, ever_lock, time.monotonic() - started)
                return False
            await asyncio.sleep(min(0.5, 0.05 * (2 ** (attempt - 1))))
            continue
        if on_result is not None:
            await on_result(attempt, attempts, False, ever_lock, time.monotonic() - started)
        return False
    return False


async def safe_commit(
    db: Session,
    label: str,
    reapply: Callable[[], None] | None = None,
    max_attempts: int = 4,
    on_result: "Callable[[int, int, bool, bool, float], Awaitable[None]] | None" = None,
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

    `on_result`: see _safe_write above — optional observational hook.
    """
    return await _safe_write(db, "commit", db.commit, label, reapply, max_attempts, on_result)


async def safe_flush(
    db: Session,
    label: str,
    reapply: Callable[[], None] | None = None,
    max_attempts: int = 4,
    on_result: "Callable[[int, int, bool, bool, float], Awaitable[None]] | None" = None,
) -> bool:
    """Same contract as safe_commit, for db.flush(). Root-cause fix for a real
    gap found in production logs: worker.py's `db.add(det_row); db.flush()`
    (assigns the detection's client-generated id and makes it visible for the
    rest of the frame's processing, well before the eventual safe_commit)
    issued a real write against SQLite completely unguarded — a lock there
    surfaced as an uncaught OperationalError that killed the whole per-frame
    detection loop iteration (rolled back further up in worker.py's outer
    except, silently dropping that detection) instead of being retried in
    place like every other write in this pipeline.

    `reapply` for a flush is almost always just re-`db.add()`-ing the still-
    transient object(s) pending in this flush — rollback only detaches them,
    their already-set Python attributes (including a client-side-generated
    PK) survive untouched, same as safe_commit's docstring above."""
    return await _safe_write(db, "flush", db.flush, label, reapply, max_attempts, on_result)
