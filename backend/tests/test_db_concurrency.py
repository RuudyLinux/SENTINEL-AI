"""Regression tests for the SQLite "database is locked" concurrency fix
(pipeline/db_retry.py). These use a real, dedicated on-disk SQLite file and
a genuine second connection holding a real write lock — not a mock — so
they prove the fix against SQLite's actual locking behavior, the same way
this was verified before the fix was written (scratchpad experiment
scripts, not reproduced here).

A dedicated throwaway DB file (not the shared tests/conftest.py test.db) is
used so this test can safely shrink busy_timeout far below the app's real
30s (db.py) — forcing a genuine, fast OperationalError instead of waiting
out the full 30s budget SQLite itself already absorbs before ever surfacing
"database is locked" to Python (confirmed during this fix's investigation:
with the app's real 30s busy_timeout, a lock error reaching Python at all
already means multi-second contention, not a sub-second blip).
"""
import asyncio
import os
import sqlite3
import tempfile
import threading
import time

from sqlalchemy import create_engine, event
from sqlalchemy.orm import sessionmaker

from app.db import Base
from app import models
from app.pipeline.db_retry import safe_commit, safe_flush
from app.pipeline.correlate import upsert_vehicle_for_plate


def _make_short_timeout_engine(db_path: str):
    """Same PRAGMAs as db.py's real engine, but with a much shorter
    busy_timeout — purely so this test can force a real lock error in
    milliseconds instead of seconds; the retry/backoff mechanism under test
    is identical for any timeout value."""
    engine = create_engine(f"sqlite:///{db_path}", connect_args={"check_same_thread": False, "timeout": 0.2})

    @event.listens_for(engine, "connect")
    def _pragmas(dbapi_connection, _record):
        cursor = dbapi_connection.cursor()
        cursor.execute("PRAGMA journal_mode=WAL")
        cursor.execute("PRAGMA busy_timeout=200")
        cursor.close()

    return engine


def test_safe_commit_retries_through_a_real_sqlite_lock_and_succeeds():
    """A genuine second connection holds a real write lock on the actual
    on-disk file for longer than a single commit attempt's busy_timeout
    budget. safe_commit must rollback, reapply, back off, and retry until
    the lock releases — and the reapplied value must actually be the one
    that ends up durably committed (verified by reloading through a
    completely separate connection, not just trusting the return value)."""
    tmp_dir = tempfile.mkdtemp(prefix="sentinel_lock_test_")
    db_path = os.path.join(tmp_dir, "lock_test.db").replace("\\", "/")

    engine = _make_short_timeout_engine(db_path)
    Base.metadata.create_all(bind=engine)
    Session = sessionmaker(bind=engine, autocommit=False, autoflush=False, expire_on_commit=False)

    db = Session()
    camera = models.Camera(
        camera_code="C-REAL-LOCK", name="lock test", source_type="video_file",
        source_uri="unused.mp4", status="offline", error_count=0,
    )
    db.add(camera)
    db.commit()
    camera_id = camera.id

    lock_hold_seconds = 0.6  # comfortably longer than several retry attempts' total backoff

    def _hold_lock():
        conn = sqlite3.connect(db_path, timeout=30)  # this side isn't under test — it just needs to hold and release
        conn.execute("BEGIN IMMEDIATE")
        conn.execute("UPDATE cameras SET name = name WHERE id = ?", (camera_id,))
        time.sleep(lock_hold_seconds)
        conn.commit()
        conn.close()

    holder = threading.Thread(target=_hold_lock)
    holder.start()
    time.sleep(0.15)  # let the holder actually acquire the write lock first

    try:
        target_status = "online"
        camera.status = target_status  # type: ignore[assignment]

        def reapply():
            # Reassigns from the captured local, never by re-reading
            # camera.status — a rollback would have expired it back to
            # "offline" (the last-committed value), per this fix's
            # verified rollback semantics.
            camera.status = target_status  # type: ignore[assignment]

        ok = asyncio.run(safe_commit(db, "test-camera", reapply=reapply, max_attempts=20))
        assert ok is True
    finally:
        holder.join()
        db.close()

    verify_db = Session()
    try:
        reloaded = verify_db.query(models.Camera).filter(models.Camera.id == camera_id).first()
        assert reloaded is not None
        assert reloaded.status == "online"  # durably committed, verified via a fresh connection
    finally:
        verify_db.close()
    engine.dispose()


def test_safe_commit_without_reapply_fails_fast_under_a_real_lock_rather_than_retrying():
    """No `reapply` given -> a single attempt, matching the pre-fix
    behavior — proves this never silently turns into an unbounded/blind
    retry loop against a real lock when the caller hasn't opted in with a
    way to redo the pending write."""
    tmp_dir = tempfile.mkdtemp(prefix="sentinel_lock_test_")
    db_path = os.path.join(tmp_dir, "lock_test2.db").replace("\\", "/")

    engine = _make_short_timeout_engine(db_path)
    Base.metadata.create_all(bind=engine)
    Session = sessionmaker(bind=engine, autocommit=False, autoflush=False, expire_on_commit=False)

    db = Session()
    camera = models.Camera(
        camera_code="C-REAL-LOCK-2", name="lock test 2", source_type="video_file",
        source_uri="unused.mp4", status="offline",
    )
    db.add(camera)
    db.commit()
    camera_id = camera.id

    def _hold_lock():
        conn = sqlite3.connect(db_path, timeout=30)
        conn.execute("BEGIN IMMEDIATE")
        conn.execute("UPDATE cameras SET name = name WHERE id = ?", (camera_id,))
        time.sleep(1.0)
        conn.commit()
        conn.close()

    holder = threading.Thread(target=_hold_lock)
    holder.start()
    time.sleep(0.15)

    try:
        camera.status = "online"  # type: ignore[assignment]
        t0 = time.monotonic()
        ok = asyncio.run(safe_commit(db, "test-camera"))
        elapsed = time.monotonic() - t0
    finally:
        holder.join()
        db.close()
        engine.dispose()

    assert ok is False
    assert elapsed < 1.0  # single attempt, not stuck waiting through the holder's full 1s hold


def test_safe_flush_retries_through_a_real_sqlite_lock_and_succeeds():
    """Root-cause regression test: worker.py's `db.add(det_row); db.flush()`
    (assigns the detection's identity before the rest of the frame's
    processing) was completely unguarded — a real lock there escaped to
    _camera_loop's outer except and silently dropped the detection instead
    of retrying, exactly as seen in a real production log (`INSERT INTO
    detections ... sqlite3.OperationalError: database is locked`). Same real-
    lock harness as the safe_commit test above, proving safe_flush recovers
    and the flushed row is durably visible."""
    tmp_dir = tempfile.mkdtemp(prefix="sentinel_lock_test_")
    db_path = os.path.join(tmp_dir, "flush_lock_test.db").replace("\\", "/")

    engine = _make_short_timeout_engine(db_path)
    Base.metadata.create_all(bind=engine)
    Session = sessionmaker(bind=engine, autocommit=False, autoflush=False, expire_on_commit=False)

    db = Session()
    camera = models.Camera(
        camera_code="C-FLUSH-LOCK", name="flush lock test", source_type="video_file",
        source_uri="unused.mp4", status="offline",
    )
    db.add(camera)
    db.commit()
    camera_id = camera.id

    lock_hold_seconds = 0.6

    def _hold_lock():
        conn = sqlite3.connect(db_path, timeout=30)
        conn.execute("BEGIN IMMEDIATE")
        conn.execute("UPDATE cameras SET name = name WHERE id = ?", (camera_id,))
        time.sleep(lock_hold_seconds)
        conn.commit()
        conn.close()

    holder = threading.Thread(target=_hold_lock)
    holder.start()
    time.sleep(0.15)

    try:
        det = models.Detection(
            camera_id=camera_id, cls="car", confidence=0.9, bbox=[1, 2, 3, 4],
        )
        db.add(det)

        def reapply():
            # Same reasoning as db_retry.safe_flush's docstring: rollback
            # only detaches the still-transient `det` — its client-generated
            # id and other already-set attributes survive untouched, so
            # re-add() alone correctly restores it for the retried flush.
            db.add(det)

        ok = asyncio.run(safe_flush(db, "test-camera", reapply=reapply, max_attempts=20))
        assert ok is True
        det_id = det.id
        db.commit()  # persist what the flush staged, so a fresh connection can see it
    finally:
        holder.join()
        db.close()

    verify_db = Session()
    try:
        reloaded = verify_db.query(models.Detection).filter(models.Detection.id == det_id).first()
        assert reloaded is not None  # durably persisted, verified via a fresh connection
        assert reloaded.camera_id == camera_id
    finally:
        verify_db.close()
    engine.dispose()


def test_safe_flush_without_reapply_fails_fast_rather_than_retrying():
    """No `reapply` given -> a single attempt, same bounded-retry contract as
    safe_commit — proves this never turns into an unbounded/blind retry
    loop against a real lock."""
    tmp_dir = tempfile.mkdtemp(prefix="sentinel_lock_test_")
    db_path = os.path.join(tmp_dir, "flush_lock_test2.db").replace("\\", "/")

    engine = _make_short_timeout_engine(db_path)
    Base.metadata.create_all(bind=engine)
    Session = sessionmaker(bind=engine, autocommit=False, autoflush=False, expire_on_commit=False)

    db = Session()
    camera = models.Camera(
        camera_code="C-FLUSH-LOCK-2", name="flush lock test 2", source_type="video_file",
        source_uri="unused.mp4", status="offline",
    )
    db.add(camera)
    db.commit()
    camera_id = camera.id

    def _hold_lock():
        conn = sqlite3.connect(db_path, timeout=30)
        conn.execute("BEGIN IMMEDIATE")
        conn.execute("UPDATE cameras SET name = name WHERE id = ?", (camera_id,))
        time.sleep(1.0)
        conn.commit()
        conn.close()

    holder = threading.Thread(target=_hold_lock)
    holder.start()
    time.sleep(0.15)

    try:
        det = models.Detection(camera_id=camera_id, cls="car", confidence=0.9, bbox=[1, 2, 3, 4])
        db.add(det)
        t0 = time.monotonic()
        ok = asyncio.run(safe_flush(db, "test-camera"))
        elapsed = time.monotonic() - t0
    finally:
        holder.join()
        db.close()
        engine.dispose()

    assert ok is False
    assert elapsed < 1.0


def test_upsert_vehicle_for_plate_retries_through_a_real_sqlite_lock():
    """Final-demo-readiness-phase regression test: found live — triggering
    the demo scenario while the two demo cameras' real workers were writing
    concurrently produced a genuine unhandled 500 from this function's own
    unguarded `db.flush()`. Shared by BOTH the real live pipeline
    (worker.py) and demo_scenario.py — fixing it here covers both callers.
    Same real-second-connection-holds-a-real-lock harness as the tests
    above, for the NEW-vehicle path (inserts a fresh, never-before-committed
    Vehicle row)."""
    tmp_dir = tempfile.mkdtemp(prefix="sentinel_lock_test_")
    db_path = os.path.join(tmp_dir, "vehicle_lock_test.db").replace("\\", "/")

    engine = _make_short_timeout_engine(db_path)
    Base.metadata.create_all(bind=engine)
    Session = sessionmaker(bind=engine, autocommit=False, autoflush=False, expire_on_commit=False)

    db = Session()
    lock_hold_seconds = 0.6

    def _hold_lock():
        conn = sqlite3.connect(db_path, timeout=30)
        conn.execute("BEGIN IMMEDIATE")
        conn.execute("CREATE TABLE IF NOT EXISTS _lock_probe (id INTEGER)")
        conn.execute("INSERT INTO _lock_probe (id) VALUES (1)")
        time.sleep(lock_hold_seconds)
        conn.commit()
        conn.close()

    holder = threading.Thread(target=_hold_lock)
    holder.start()
    time.sleep(0.15)

    try:
        vehicle = asyncio.run(upsert_vehicle_for_plate(db, "GJ01LOCKTEST", 0.9))
        assert vehicle is not None
        vehicle_id = vehicle.id
        db.commit()
    finally:
        holder.join()
        db.close()

    verify_db = Session()
    try:
        reloaded = verify_db.query(models.Vehicle).filter(models.Vehicle.id == vehicle_id).first()
        assert reloaded is not None  # durably persisted despite the real lock
        assert reloaded.plate_text == "GJ01LOCKTEST"
    finally:
        verify_db.close()
    engine.dispose()
