"""Tearing down a worker mustn't race its own DB thread.

Hit about 2 runs in 5 of the 12-worker stress test, and a real production
fault. db_retry runs commits via to_thread; cancelling a worker unwinds the
await but not the thread, so the loop's finally db.close() ran while the
thread was still in commit():

    sqlalchemy.exc.IllegalStateChangeError: Method 'close()' can't be called
    here; method '_prepare_impl()' is already in progress

Sessions aren't thread-safe, and from a finally that reached
_camera_loop_supervised, which marked a healthy camera OFFLINE on any stop
or shutdown.
"""
import asyncio
import threading
import time

import pytest

from app.pipeline import db_retry


class _SlowSession:
    """A Session whose commit is still running on a thread when teardown
    starts. Records the order of events so the test checks what happened."""

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
    """A task cancelled mid-commit. to_thread can't stop the thread, so the
    commit carries on after the await unwinds.
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
    """Runs from a finally on cancel; raising there is what took healthy
    cameras offline."""

    class _Exploding:
        def close(self):
            raise RuntimeError("connection already returned to the pool")

    db_retry.close_session(_Exploding())  # must not propagate


def test_a_stuck_operation_defers_the_close_instead_of_blocking_teardown(monkeypatch):
    """A stuck close mustn't hold up shutdown, and mustn't be skipped either.

    Just returning on timeout left the Session referenced (never collected)
    with its write transaction open, and every later test died on
    "database is locked". It's handed to a daemon thread now.
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

    # still released, just later; without this the whole suite broke
    thread.join(timeout=10)
    for _ in range(100):
        if session.closed:
            break
        time.sleep(0.05)
    assert session.closed is True, "the deferred close must still happen"
    assert session.events == ["commit:start", "commit:end", "close"]


def test_the_close_budget_exceeds_the_databases_own_busy_wait():
    """A contended commit can sit in the whole busy_timeout, so a shorter
    close timeout fires under exactly that load (10s vs 30s did)."""
    from app.db import SQLITE_BUSY_TIMEOUT_SECONDS

    assert db_retry._CLOSE_LOCK_TIMEOUT_SECONDS > SQLITE_BUSY_TIMEOUT_SECONDS


def test_one_lock_per_session_not_a_global_one():
    """Two cameras' sessions don't serialize on each other."""
    a, b = _SlowSession(), _SlowSession()
    assert db_retry._lock_for(a) is db_retry._lock_for(a)
    assert db_retry._lock_for(a) is not db_retry._lock_for(b)
