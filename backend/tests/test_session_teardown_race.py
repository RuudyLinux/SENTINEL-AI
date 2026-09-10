"""Regression: tearing down a camera worker must not race its own DB thread.

The defect this pins down was reproducible under the 12-worker concurrency
stress test roughly two runs in five, and it was a real production fault, not a
test artefact:

`db_retry` hands every commit/flush to a worker thread via `asyncio.to_thread`.
Cancelling a camera worker unwinds the *await* immediately, but cannot stop the
thread — so `_camera_loop`'s `finally: db.close()` ran on the event-loop thread
while that thread was still inside `commit()`:

    sqlalchemy.exc.IllegalStateChangeError: Method 'close()' can't be called
    here; method '_prepare_impl()' is already in progress

A SQLAlchemy Session is explicitly not thread-safe. Because the raise came from
a `finally`, it escaped to `_camera_loop_supervised`, which marked a perfectly
healthy camera OFFLINE and recorded a critical self-heal event. Every
stop_worker and every process shutdown could trigger it.
"""
import asyncio
import threading
import time

import pytest

from app.pipeline import db_retry


class _SlowSession:
    """Stands in for a Session whose commit is still running on a worker thread
    when teardown begins. Records the real ordering so the test asserts what
    actually happened rather than merely that nothing raised."""

    def __init__(self, commit_seconds: float = 0.4):
        self.commit_seconds = commit_seconds
        self.events: list[str] = []
        self.closed = False
        self._inside_commit = False

    def commit(self) -> None:
        self.events.append("commit:start")
        self._inside_commit = True
        time.sleep(self.commit_seconds)
        # This is precisely the check SQLAlchemy performs internally: a close()
        # landing here is the bug.
        if self.closed:
            raise AssertionError("close() ran while a commit was still in progress")
        self._inside_commit = False
        self.events.append("commit:end")

    def rollback(self) -> None:
        self.events.append("rollback")

    def close(self) -> None:
        if self._inside_commit:
            raise AssertionError("close() ran while a commit was still in progress")
        self.events.append("close")
        self.closed = True


def test_close_session_waits_for_an_in_flight_threaded_commit():
    """The core invariant: teardown blocks until the DB thread is done."""
    session = _SlowSession(commit_seconds=0.3)

    thread = threading.Thread(target=lambda: db_retry._locked(session, session.commit))
    thread.start()
    time.sleep(0.05)  # let the commit get inside the lock

    db_retry.close_session(session)  # must wait, not race
    thread.join(timeout=5)

    assert session.events == ["commit:start", "commit:end", "close"]
    assert session.closed is True


def test_cancelling_a_worker_mid_commit_still_closes_cleanly():
    """The real-world shape: a task cancelled while its commit is in flight.

    `asyncio.to_thread` cannot interrupt the thread, so the commit continues
    after the await unwinds. Teardown must tolerate that.
    """
    session = _SlowSession(commit_seconds=0.3)

    async def scenario():
        async def worker():
            try:
                await asyncio.to_thread(db_retry._locked, session, session.commit)
            finally:
                # Exactly what _camera_loop's `finally` does.
                db_retry.close_session(session)

        task = asyncio.create_task(worker())
        await asyncio.sleep(0.05)
        task.cancel()
        # Must not raise anything other than the cancellation itself.
        with pytest.raises(asyncio.CancelledError):
            await task

    asyncio.run(scenario())
    # Give the abandoned worker thread a moment to finish its commit.
    for _ in range(50):
        if session.closed:
            break
        time.sleep(0.05)
    assert session.closed is True, "the session must still get closed, just not early"
    assert "commit:end" in session.events


def test_close_session_never_raises_even_if_close_itself_fails():
    """This runs from a `finally` on a cancellation path. An exception there
    replaces the cancellation and is what marked healthy cameras offline."""

    class _Exploding:
        def close(self):
            raise RuntimeError("connection already returned to the pool")

    db_retry.close_session(_Exploding())  # must not propagate


def test_a_stuck_operation_defers_the_close_instead_of_blocking_teardown(monkeypatch):
    """A pathological stall must not hold shutdown open — but it must not skip
    the close either.

    An earlier version simply returned on timeout, reasoning that the connection
    would go back to the pool once the Session was collected. That was wrong,
    and this suite caught it: the Session is still referenced so it is never
    collected, and on SQLite its open write transaction keeps the database
    locked for every other writer in the process — every subsequent test died
    on "database is locked". The close is handed to a daemon thread instead.
    """
    monkeypatch.setattr(db_retry, "_CLOSE_LOCK_TIMEOUT_SECONDS", 0.15)
    session = _SlowSession(commit_seconds=0.8)

    thread = threading.Thread(target=lambda: db_retry._locked(session, session.commit), daemon=True)
    thread.start()
    time.sleep(0.05)

    started = time.monotonic()
    db_retry.close_session(session)
    elapsed = time.monotonic() - started

    assert elapsed < 0.6, "teardown must not block on a stuck operation"
    assert session.closed is False, "it must not close underneath the running commit"

    # The transaction is still released, just later — this is the property whose
    # absence broke the whole suite.
    thread.join(timeout=10)
    for _ in range(100):
        if session.closed:
            break
        time.sleep(0.05)
    assert session.closed is True, "the deferred close must still happen"
    assert session.events == ["commit:start", "commit:end", "close"]


def test_the_close_budget_exceeds_the_databases_own_busy_wait():
    """A contended commit legitimately blocks for SQLite's entire busy-wait
    budget. A close timeout shorter than that is guaranteed to fire under
    exactly the load this code exists to survive — which is what happened at
    10s against a 30s busy_timeout."""
    from app.db import SQLITE_BUSY_TIMEOUT_SECONDS

    assert db_retry._CLOSE_LOCK_TIMEOUT_SECONDS > SQLITE_BUSY_TIMEOUT_SECONDS


def test_one_lock_per_session_not_a_global_one():
    """Two cameras' sessions must not serialize against each other — that would
    turn a correctness fix into a throughput regression."""
    a, b = _SlowSession(), _SlowSession()
    assert db_retry._lock_for(a) is db_retry._lock_for(a)
    assert db_retry._lock_for(a) is not db_retry._lock_for(b)
