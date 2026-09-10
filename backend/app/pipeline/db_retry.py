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
import threading
import time
from typing import Awaitable, Callable
from weakref import WeakKeyDictionary

from sqlalchemy.exc import OperationalError
from sqlalchemy.orm import Session

from .. import metrics
from ..db import SQLITE_BUSY_TIMEOUT_SECONDS

logger = logging.getLogger("sentinel.worker")

# --- Session serialization -------------------------------------------------
# Every DB call below is offloaded with `asyncio.to_thread`, which means the
# work continues on a worker thread even if the awaiting task is CANCELLED —
# `to_thread` cannot interrupt a running thread, it only abandons the await.
#
# Real bug this fixes (reproduced under the 12-worker concurrency stress test,
# roughly 2 runs in 5): cancelling a camera worker mid-commit unwinds the await
# immediately, so `_camera_loop`'s `finally: db.close()` ran on the event-loop
# thread while the worker thread was still inside `commit()`:
#
#   sqlalchemy.exc.IllegalStateChangeError: Method 'close()' can't be called
#   here; method '_prepare_impl()' is already in progress
#
# A Session is explicitly NOT thread-safe, and that raise escaped the `finally`
# to `_camera_loop_supervised`, which marked a perfectly healthy camera
# OFFLINE. Every stop_worker/shutdown could hit it.
#
# One lock per Session, held for the duration of each threaded DB call, gives
# the Session the single-writer discipline it requires. `close_session` below
# takes the same lock, so teardown waits for an in-flight commit instead of
# racing it. Keyed weakly so a closed session's lock is collected with it.
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
    """Run one Session operation with exclusive access to that Session."""
    with _lock_for(db):
        op()


# How long teardown waits, on the CALLING thread, for an in-flight DB call.
#
# This must not be a small round number. A contended commit legitimately blocks
# for SQLite's whole busy-wait budget, so anything shorter is guaranteed to fire
# under exactly the load this code exists to survive — measured: a 10s bound hit
# every full test run, because a 12-worker commit was still inside its 30s
# busy_timeout. Derived from that budget so the two can never drift apart again.
_CLOSE_LOCK_TIMEOUT_SECONDS = SQLITE_BUSY_TIMEOUT_SECONDS + 5.0

# ...and how long the fallback thread keeps trying afterwards. Generous, because
# the alternative to closing is leaving a write transaction open (see below).
_CLOSE_BACKGROUND_TIMEOUT_SECONDS = 300.0


def _close_when_free(db: Session, lock: threading.Lock) -> None:
    """Wait out a genuinely stuck operation, then close, off the caller's thread."""
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
    """Close a Session that a background thread may still be using.

    Callers tearing a camera worker down must use this instead of `db.close()`:
    a plain close racing an in-flight threaded commit raises
    IllegalStateChangeError (see above), and doing so from a `finally` turns a
    routine cancellation into a worker crash.

    On timeout the close is HANDED OFF, never skipped. An earlier version simply
    returned, on the reasoning that "the connection goes back to the pool when
    the Session is collected" — that reasoning was wrong and the test suite
    caught it: the Session is still referenced, so it is not collected, and on
    SQLite its open write transaction keeps the database locked for every other
    writer in the process. The observed symptom was every subsequent test dying
    on `database is locked`. Waiting on a daemon thread keeps the caller (often
    an event loop mid-shutdown) responsive while still releasing the lock.
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
        # Teardown must never raise: this runs from a `finally` on a
        # cancellation path, where an exception replaces the cancellation and
        # is what made a healthy camera get marked offline.
        logger.exception("closing the session failed")
    finally:
        lock.release()

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
    try:
        return await _attempt_loop(db, op_name, op, label, reapply, attempts, on_result, started)
    finally:
        # Observed once per logical write, on every exit path, including the
        # failure ones — a metric that only records successes would hide
        # exactly the contention it exists to reveal.
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
    """The retry loop itself, split out only so `_safe_write` can time every
    exit path in one `finally` rather than repeating it at six return sites."""
    ever_lock = False
    for attempt in range(1, attempts + 1):
        try:
            # Under the Session's lock: `to_thread` keeps running after a
            # cancellation, so teardown must be able to wait for it (see
            # close_session).
            await asyncio.to_thread(_locked, db, op)
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
            await asyncio.to_thread(_locked, db, db.rollback)
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
