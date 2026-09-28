"""Login stalling behind camera writes.

With 30 Sentinel Grid cameras a login took 121-149 s. Measured cause: a
camera's detection flush takes SQLite's single write lock, and the commit
that releases it was another hop on asyncio's default thread pool. That pool
was full of threads waiting on the shared YOLO/OCR model locks, so the commit
queued for tens of seconds with the lock held, and the login's audit INSERT
waited out the 30 s busy_timeout five times over.

These tests rebuild that: a camera session holding the lock, the default
pool saturated, and a login (or another writer) that has to get through.
"""
import asyncio
import sqlite3
import statistics
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor

import numpy as np

from app import models
from app.config import settings
from app.db import SessionLocal
from app.pipeline import rules_engine, worker
from app.pipeline.db_retry import safe_commit, safe_flush

LOGIN = {"username": "test_admin", "password": "testpass123"}

# bcrypt alone is ~0.2 s here. Generous on purpose: the failure being guarded
# against is a wait of tens of seconds, not a slow CI box.
FAST_LOGIN_S = 3.0


def _login_seconds(client) -> float:
    t = time.perf_counter()
    resp = client.post("/api/auth/login", json=LOGIN)
    elapsed = time.perf_counter() - t
    assert resp.status_code == 200, resp.text
    return elapsed


def _camera(db_session) -> models.Camera:
    camera = models.Camera(
        camera_code=f"C-CONTENTION-{uuid.uuid4().hex[:8]}", name="Contention", location="bench",
        source_type="mock_vms", source_uri="", ai_person=True, ai_vehicle=True, ai_anpr=False,
    )
    db_session.add(camera)
    db_session.commit()
    return camera


def test_login_with_no_camera_workers_is_fast(client, admin_user):
    assert _login_seconds(client) < FAST_LOGIN_S


def test_login_is_not_held_up_by_a_camera_commit_waiting_for_a_thread(client, admin_user, db_session):
    """The measured failure. The camera flushes (write lock taken), then every
    thread of the default pool is busy (inference waiting on the model lock),
    then it commits. The commit must not need that pool, or the lock stays
    held and the login's audit write waits behind it."""
    camera = _camera(db_session)
    flushed, committed = threading.Event(), threading.Event()
    inference_done = threading.Event()
    # the blockers give up on their own, so a regression fails in ~10 s
    # instead of hanging the suite
    threading.Timer(10.0, inference_done.set).start()
    result = {}

    def camera_worker():
        async def main():
            loop = asyncio.get_running_loop()
            loop.set_default_executor(ThreadPoolExecutor(max_workers=2, thread_name_prefix="test-default"))
            db = SessionLocal()
            try:
                det = models.Detection(camera_id=camera.id, cls="car", confidence=0.9, bbox=[0, 0, 10, 10])
                db.add(det)
                assert await safe_flush(db, "test camera", reapply=lambda: db.add(det))
                # both default-pool threads now stuck, like YOLO/OCR callers
                # queued on the shared model lock
                blockers = [loop.run_in_executor(None, inference_done.wait, 30) for _ in range(2)]
                await asyncio.sleep(0.2)
                flushed.set()
                t = time.perf_counter()
                result["ok"] = await safe_commit(db, "test camera", reapply=lambda: db.add(det))
                result["commit_s"] = time.perf_counter() - t
                committed.set()
                inference_done.set()
                await asyncio.gather(*blockers)
            finally:
                db.close()
        asyncio.run(main())

    t = threading.Thread(target=camera_worker)
    t.start()
    try:
        assert flushed.wait(15), "camera never flushed"
        login_s = _login_seconds(client)
    finally:
        inference_done.set()
        t.join(30)
    assert result.get("ok") is True
    assert result["commit_s"] < 2.0, f"commit waited {result['commit_s']:.1f}s for a thread"
    assert login_s < FAST_LOGIN_S, f"login took {login_s:.1f}s behind the camera's write lock"


def test_login_stays_fast_while_cameras_write_continuously(client, admin_user, db_session):
    """Several cameras committing detections as fast as they can, logins in
    between. Steady short writers must not starve the audit write."""
    cameras = [_camera(db_session) for _ in range(6)]
    stop = threading.Event()
    writes = []

    def camera_writer(camera_id):
        async def main():
            db = SessionLocal()
            n = 0
            try:
                while not stop.is_set():
                    det = models.Detection(camera_id=camera_id, cls="car", confidence=0.9, bbox=[0, 0, 10, 10])
                    db.add(det)
                    if await safe_commit(db, "writer", reapply=lambda det=det: db.add(det)):
                        n += 1
                    await asyncio.sleep(0.005)
            finally:
                writes.append(n)
                db.close()
        asyncio.run(main())

    threads = [threading.Thread(target=camera_writer, args=(c.id,)) for c in cameras]
    for th in threads:
        th.start()
    try:
        time.sleep(0.5)
        latencies = [_login_seconds(client) for _ in range(8)]
    finally:
        stop.set()
        for th in threads:
            th.join(30)
    assert sum(writes) > 50, f"the writers barely wrote ({writes}), the test proves nothing"
    assert max(latencies) < FAST_LOGIN_S, latencies
    assert statistics.median(latencies) < 1.5, latencies


def test_an_alert_flush_waiting_on_the_lock_does_not_freeze_the_event_loop(db_session, monkeypatch, tmp_path):
    """evaluate() flushed the alert with a bare db.flush() on the event loop.
    While another connection held the write lock, every camera and request
    on that loop stopped for as long as the flush waited."""
    monkeypatch.setattr(settings, "zone_entry_min_frames", 1)
    monkeypatch.setattr(worker.settings, "evidence_dir", tmp_path)
    rules_engine._alert_claims.clear()
    rules_engine._zone_presence.clear()
    camera = _camera(db_session)
    db_session.add(models.Zone(name="all", camera_id=camera.id, x1=0, y1=0, x2=1, y2=1, severity="HIGH", active=True))
    det = models.Detection(camera_id=camera.id, cls="person", confidence=0.9, bbox=[100, 100, 300, 300], track_id="1")
    db_session.add(det)
    db_session.commit()

    holding, release = threading.Event(), threading.Event()

    def other_writer():
        # a raw connection holding the write lock, like a slow writer elsewhere
        conn = sqlite3.connect(str(settings.db_path), timeout=30)
        conn.execute("BEGIN IMMEDIATE")
        holding.set()
        release.wait(10)
        conn.rollback()
        conn.close()

    async def main():
        loop = asyncio.get_running_loop()
        lags, probing = [], threading.Event()

        def probe():
            # from outside the loop, how long a trivial callback waits to run.
            # an in-loop ticker can't see a freeze, it's frozen too
            async def noop():
                return None
            while not probing.is_set():
                t = time.perf_counter()
                asyncio.run_coroutine_threadsafe(noop(), loop).result(30)
                lags.append(time.perf_counter() - t)
                time.sleep(0.05)

        prober = threading.Thread(target=probe)
        prober.start()
        # the lock goes away 1.5 s in, on a plain timer: scheduled on the loop
        # it could never fire while the loop was the thing stuck
        threading.Timer(1.5, release.set).start()
        try:
            alerts = await rules_engine.evaluate(db_session, camera, det, 640, 480, None)
            await asyncio.sleep(0.2)
        finally:
            probing.set()
            await asyncio.to_thread(prober.join, 30)
        return alerts, max(lags)

    th = threading.Thread(target=other_writer)
    th.start()
    assert holding.wait(5)
    try:
        alerts, worst_gap = asyncio.run(main())
    finally:
        release.set()
        th.join(10)
    assert len(alerts) == 1
    assert worst_gap < 0.5, f"event loop stalled {worst_gap:.2f}s while the alert flush waited"


def test_anpr_snapshot_is_saved_before_the_detection_takes_the_write_lock(db_session, monkeypatch, tmp_path):
    """The plate snapshot (a thread hop) used to be taken after the detection
    flush, inside the open write transaction."""
    order = []
    monkeypatch.setattr(worker.settings, "evidence_dir", tmp_path)
    monkeypatch.setattr(worker.settings, "plate_pipeline_v2", False)
    monkeypatch.setattr(worker, "read_plate", lambda crop: ("GJ01AB1234", "GJ01AB1234", 0.95))
    real_save = worker._save_snapshot

    def recording_save(*a, **k):
        order.append("snapshot")
        return real_save(*a, **k)

    async def fake_flush(*a, **k):
        order.append("flush")
        return False  # stop after the flush

    monkeypatch.setattr(worker, "_save_snapshot", recording_save)
    monkeypatch.setattr(worker, "_safe_flush", fake_flush)
    monkeypatch.setattr(worker, "detect_and_track", lambda *a, **k: [
        {"cls": "car", "confidence": 0.9, "bbox": [1, 1, 60, 40], "track_id": None},
    ])
    camera = models.Camera(
        camera_code=f"C-SNAPFIRST-{uuid.uuid4().hex[:6]}", name="p", source_type="mock_vms", source_uri="",
        ai_person=False, ai_vehicle=True, ai_anpr=True,
    )
    frame = np.zeros((48, 64, 3), dtype=np.uint8)
    n = worker.settings.detect_every_n_frames
    asyncio.run(worker._process_frame(db_session, camera, frame, n, 64, 48, None, []))
    assert order == ["snapshot", "flush"], order
    db_session.rollback()
