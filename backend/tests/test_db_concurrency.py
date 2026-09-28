"""SQLite "database is locked" handling (pipeline/db_retry.py), against a
real on-disk file with a real second connection holding the write lock, not
a mock.

Own throwaway DB file, not conftest's test.db, so busy_timeout can be far
below the app's 30s and a real OperationalError shows up fast. With 30s a
lock error reaching Python already means seconds of contention.
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
    """db.py's PRAGMAs with a much shorter busy_timeout so a real lock error
    shows up in ms. The retry logic is the same for any timeout."""
    engine = create_engine(f"sqlite:///{db_path}", connect_args={"check_same_thread": False, "timeout": 0.2})

    @event.listens_for(engine, "connect")
    def _pragmas(dbapi_connection, _record):
        cursor = dbapi_connection.cursor()
        cursor.execute("PRAGMA journal_mode=WAL")
        cursor.execute("PRAGMA busy_timeout=200")
        cursor.close()

    return engine


def test_safe_commit_retries_through_a_real_sqlite_lock_and_succeeds():
    """A second connection holds the write lock longer than one commit
    attempt waits. safe_commit has to roll back, reapply, back off and retry
    until it's free, and the reapplied value is what ends up committed
    (checked from a separate connection, not the return value)."""
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

    lock_hold_seconds = 0.6  # longer than several retries' total backoff

    def _hold_lock():
        conn = sqlite3.connect(db_path, timeout=30)  # just holds and releases, not under test
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
            # from the captured local, rollback expired camera.status back
            # to "offline"
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
    """No reapply: one attempt, never a blind retry loop."""
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
    assert elapsed < 1.0  # one attempt, not waiting out the holder's 1s


def test_safe_flush_retries_through_a_real_sqlite_lock_and_succeeds():
    """safe_flush on the worker's detection flush, which used to be unguarded:
    a real lock there dropped the detection (seen in a production log:
    INSERT INTO detections ... database is locked). Same harness as above;
    the flushed row has to be durably visible."""
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
            # rollback only detaches the new `det`, its id and attributes
            # survive, so re-add() is enough
            db.add(det)

        ok = asyncio.run(safe_flush(db, "test-camera", reapply=reapply, max_attempts=20))
        assert ok is True
        det_id = det.id
        db.commit()  # so a fresh connection can see it
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
    """No reapply: one attempt, like safe_commit."""
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
    """upsert_vehicle_for_plate's flush used to be unguarded and 500'd when
    the demo scenario ran while workers were writing. Both the live pipeline
    and demo_scenario use it. Same real-lock harness, new-vehicle path."""
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
