"""Commit/flush with retry for the camera pipeline.

A "database is locked" error means SQLite's busy_timeout already ran out. After
a failed commit the session must be rolled back, and rollback detaches new
objects and reverts changed attributes on persistent ones. So each retry calls
`reapply` to redo the pending write: re-add new objects and reassign persistent
attributes from values captured before the first attempt, never re-read.
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

# Session operations run on their own executor, not asyncio's default one.
# A flush takes SQLite's write lock until the commit, and the commit is another
# thread hop; on the shared pool that hop could queue behind inference while the
# lock stayed held, stalling every other writer. One thread per pooled
# connection means a lock holder's next step always starts immediately.
_DB_EXECUTOR = ThreadPoolExecutor(
    max_workers=settings.db_pool_size + settings.db_max_overflow,
    thread_name_prefix="sentinel-db",
)


async def run_db(func: Callable[..., Any], *args: Any) -> Any:
    """Like asyncio.to_thread, but on the DB executor."""
    loop = asyncio.get_running_loop()
    ctx = contextvars.copy_context()
    return await loop.run_in_executor(_DB_EXECUTOR, functools.partial(ctx.run, func, *args))

# Sessions aren't thread-safe, and a threaded call keeps running if the awaiting
# task is cancelled. One lock per session serialises threaded calls, and
# close_session takes the same lock so it never closes a session mid-commit.
# Weak keys let the lock disappear with the session.
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


# How long teardown waits for an in-flight call. A contended commit can
# legitimately take the whole busy_timeout, so the bound is derived from it.
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
    """Close a session a background thread may still be using. Use this instead
    of db.close() when tearing down a worker.

    If the in-flight call doesn't finish in time, the close is handed to a
    daemon thread rather than skipped, so no write transaction is left open.
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

# sqlite3 exposes no error code for this, so match the message. Kept narrow: any
# other OperationalError is a real error and must not be retried.
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
    """Retry loop shared by safe_commit and safe_flush.

    on_result, if given, is awaited once before returning with
    (final_attempt, max_attempts, success, was_lock_error, duration_s). It is
    observational only (self-heal logging).
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
    """db.flush() under the session lock, without retry; exceptions propagate.
    For callers that must tell an IntegrityError from a lock error."""
    await run_db(_locked, db, db.flush)


async def locked_commit(db: Session) -> None:
    """db.commit() under the session lock, without retry; exceptions propagate.
    Used where a write must be visible to other sessions immediately."""
    await run_db(_locked, db, db.commit)


async def locked_rollback(db: Session) -> None:
    """db.rollback() under the session lock; use after a locked_* call raised."""
    await run_db(_locked, db, db.rollback)


async def safe_commit(
    db: Session,
    label: str,
    reapply: Callable[[], None] | None = None,
    max_attempts: int = 4,
    on_result: "Callable[[int, int, bool, bool, float], Awaitable[None]] | None" = None,
) -> bool:
    """db.commit() that never raises into the calling task.

    Returns True on success, False if it gave up (already rolled back). Retries
    only on lock errors and only with a `reapply`: retrying after a rollback
    with nothing re-added would silently lose the write.
    """
    return await _safe_write(db, "commit", db.commit, label, reapply, max_attempts, on_result)


async def safe_flush(
    db: Session,
    label: str,
    reapply: Callable[[], None] | None = None,
    max_attempts: int = 4,
    on_result: "Callable[[int, int, bool, bool, float], Awaitable[None]] | None" = None,
) -> bool:
    """safe_commit for db.flush(); `reapply` usually just re-adds new objects."""
    return await _safe_write(db, "flush", db.flush, label, reapply, max_attempts, on_result)
