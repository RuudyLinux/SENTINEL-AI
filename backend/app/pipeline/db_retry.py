"""Commit/flush retry with backoff for the camera pipeline. Per-camera tasks
(worker.py, rules_engine.py) and FastAPI's sync handlers all write the same
SQLite file; see db.py for WAL and busy_timeout.

"database is locked" reaching Python means busy_timeout (30s) already ran
out, so it's real contention, and a single try/commit/rollback just dropped
the write.

A bare retry loop doesn't work (checked against a real file with a second
connection holding the lock). After commit() raises, the next call must be
rollback() or you get PendingRollbackError, and rollback wrecks what a retry
needs:
- a new, never-committed object is detached, though its attributes survive
  in memory, client-generated PK included (a FK taken from it still linked
  fine after rollback + retry);
- a persistent object's changed attributes are expired and revert to the
  committed value, so re-reading camera.status to "reapply" it gives back
  the old value.

So after each rollback `reapply` has to redo the pending write: re-add new
objects, and reassign persistent attributes from values captured before the
first attempt, never re-read.
"""
import asyncio
from concurrent.futures import ThreadPoolExecutor
import contextvars
import functools
import logging
import threading
import time
from typing import Any, Awaitable, Callable
from weakref import WeakKeyDictionary

from sqlalchemy.exc import OperationalError
from sqlalchemy.orm import Session

from .. import metrics
from ..config import settings
from ..db import SQLITE_BUSY_TIMEOUT_SECONDS

logger = logging.getLogger("sentinel.worker")

# Session operations get their own threads, not asyncio's default executor.
#
# A camera's flush takes SQLite's single write lock and holds it until the
# commit, and the commit is another thread hop. On the shared default pool
# that hop queued behind YOLO/OCR calls waiting on the model locks, frame
# waits and RTSP opens, all while the lock stayed held. Measured with 30 grid
# cameras: detection transactions held the lock 37-67 s, and every other
# writer (camera status, self-heal, the login audit row) sat out the 30 s
# busy_timeout and retried, so a login took 121-149 s.
#
# One thread per pooled connection: every in-flight session operation holds
# a connection, so this is never the bottleneck and a transaction holder's
# next step starts immediately.
_DB_EXECUTOR = ThreadPoolExecutor(
    max_workers=settings.db_pool_size + settings.db_max_overflow,
    thread_name_prefix="sentinel-db",
)


async def run_db(func: Callable[..., Any], *args: Any) -> Any:
    """asyncio.to_thread, but on the DB executor. For session work that must
    not wait behind inference, and for the few non-DB steps that sit inside
    an open write transaction."""
    loop = asyncio.get_running_loop()
    ctx = contextvars.copy_context()
    return await loop.run_in_executor(_DB_EXECUTOR, functools.partial(ctx.run, func, *args))

# Session locking. Every DB call here goes through a worker thread (run_db), and the
# thread keeps going if the awaiting task is cancelled. Cancelling a worker
# mid-commit then ran `db.close()` on the loop thread while the other thread
# was still in commit() (about 2 in 5 runs of the 12-worker stress test):
#
#   sqlalchemy.exc.IllegalStateChangeError: Method 'close()' can't be called
#   here; method '_prepare_impl()' is already in progress
#
# Sessions aren't thread-safe, and that escaped to _camera_loop_supervised and
# marked a healthy camera OFFLINE on any stop or shutdown.
#
# One lock per Session around each threaded call; close_session takes the
# same lock and waits for the in-flight commit. Weak keys so the lock goes
# away with the session.
_SESSION_LOCKS: "WeakKeyDictionary[Session, threading.Lock]" = WeakKeyDictionary()
_LOCKS_GUARD = threading.Lock()


def _lock_for(db: Session) -> threading.Lock:
    with _LOCKS_GUARD:
        lock = _SESSION_LOCKS.get(db)
        if lock is None:
            lock = threading.Lock()
            _SESSION_LOCKS[db] = lock
        return lock


def _locked(db: Session, op: "Callable[[], None]") -> None:
    with _lock_for(db):
        op()


# How long teardown waits on the calling thread for an in-flight call. A
# contended commit can legitimately sit in the whole busy_timeout, so a
# shorter bound fires under exactly the load this is for (10s tripped every
# full test run). Derived from the timeout so they can't drift.
_CLOSE_LOCK_TIMEOUT_SECONDS = SQLITE_BUSY_TIMEOUT_SECONDS + 5.0

# then how long the fallback thread keeps trying. generous, not closing
# leaves a write transaction open
_CLOSE_BACKGROUND_TIMEOUT_SECONDS = 300.0


def _close_when_free(db: Session, lock: threading.Lock) -> None:
    if not lock.acquire(timeout=_CLOSE_BACKGROUND_TIMEOUT_SECONDS):
        logger.error(
            "session still busy after %.0fs — abandoning the close; its transaction "
            "may hold a write lock until the process exits",
            _CLOSE_BACKGROUND_TIMEOUT_SECONDS,
        )
        return
    try:
        db.close()
        logger.info("session closed by the deferred teardown path")
    except Exception:
        logger.exception("deferred session close failed")
    finally:
        lock.release()


def close_session(db: Session) -> None:
    """Close a Session a background thread may still be using. Use this, not
    db.close(), when tearing down a worker (see IllegalStateChangeError above).

    On timeout the close is handed to a daemon thread, never skipped. Just
    returning left the Session referenced and its write transaction open, and
    every later test died on "database is locked".
    """
    lock = _lock_for(db)
    if not lock.acquire(timeout=_CLOSE_LOCK_TIMEOUT_SECONDS):
        logger.warning(
            "session still busy after %.0fs — deferring its close to a background "
            "thread rather than blocking teardown or leaking its transaction",
            _CLOSE_LOCK_TIMEOUT_SECONDS,
        )
        threading.Thread(
            target=_close_when_free, args=(db, lock), name="sentinel-session-close", daemon=True,
        ).start()
        return
    try:
        db.close()
    except Exception:
        # never raise here: from a finally on a cancel path it replaces the
        # cancellation and gets a healthy camera marked offline
        logger.exception("closing the session failed")
    finally:
        lock.release()

# sqlite3 has no error code for this, so match the message. Narrow on
# purpose: any other OperationalError is a real bug and must not be retried
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
    """Retry body shared by safe_commit and safe_flush (flush writes too, same
    failure, same fix).

    Calls go through run_db because they block on busy_timeout, which on
    the loop thread would stall every camera.

    on_result, if given, is awaited once before returning with
    (final_attempt, max_attempts, success, was_lock_error, duration_s). Purely
    observational (self_heal.record_event), nothing here depends on it.
    """
    started = time.monotonic()
    attempts = max_attempts if reapply is not None else 1
    try:
        return await _attempt_loop(db, op_name, op, label, reapply, attempts, on_result, started)
    finally:
        # every exit path, failures too, or the metric hides the contention
        metrics.DB_WRITE_SECONDS.labels(operation=op_name).observe(time.monotonic() - started)


async def _attempt_loop(
    db: Session,
    op_name: str,
    op: Callable[[], None],
    label: str,
    reapply: Callable[[], None] | None,
    attempts: int,
    on_result: "Callable[[int, int, bool, bool, float], Awaitable[None]] | None",
    started: float,
) -> bool:
    # split out so _safe_write can time every return in one finally
    ever_lock = False
    for attempt in range(1, attempts + 1):
        try:
            # under the session lock so close_session can wait for it
            await run_db(_locked, db, op)
            if on_result is not None:
                await on_result(attempt, attempts, True, ever_lock, time.monotonic() - started)
            return True
        except OperationalError as exc:
            is_lock = _is_lock_error(exc)
            ever_lock = ever_lock or is_lock
            if is_lock:
                metrics.DB_LOCK_RETRIES.inc()
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
            await run_db(_locked, db, db.rollback)
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


async def locked_flush(db: Session) -> None:
    """db.flush() under the session lock, no retry, exceptions propagate.
    For callers that need to tell IntegrityError (a real unique conflict,
    see correlate.upsert_vehicle_for_plate) from a lock; safe_flush swallows
    both.
    """
    await run_db(_locked, db, db.flush)


async def locked_commit(db: Session) -> None:
    """db.commit() under the session lock, no retry, exceptions propagate.
    Same idea as locked_flush, and for making a write visible to other
    sessions right away (correlate's commit-on-create)."""
    await run_db(_locked, db, db.commit)


async def locked_rollback(db: Session) -> None:
    """db.rollback() under the session lock. After a locked_* call raised,
    roll back through this, not bare db.rollback()."""
    await run_db(_locked, db, db.rollback)


async def safe_commit(
    db: Session,
    label: str,
    reapply: Callable[[], None] | None = None,
    max_attempts: int = 4,
    on_result: "Callable[[int, int, bool, bool, float], Awaitable[None]] | None" = None,
) -> bool:
    """db.commit() that never raises into the calling task.

    True on success, False if it gave up (already rolled back; the caller
    decides if that matters).

    Retries (rollback, reapply, backoff, commit) only on a lock/busy error
    AND with a reapply given. Without reapply it's one attempt: retrying
    after a rollback with nothing re-added would "succeed" having lost the
    write, which is worse than a logged failure.
    """
    return await _safe_write(db, "commit", db.commit, label, reapply, max_attempts, on_result)


async def safe_flush(
    db: Session,
    label: str,
    reapply: Callable[[], None] | None = None,
    max_attempts: int = 4,
    on_result: "Callable[[int, int, bool, bool, float], Awaitable[None]] | None" = None,
) -> bool:
    """safe_commit for db.flush(). The detection flush in worker.py is a real
    write and used to be unguarded, so a lock there dropped the detection.
    reapply is usually just re-adding the new objects."""
    return await _safe_write(db, "flush", db.flush, label, reapply, max_attempts, on_result)
