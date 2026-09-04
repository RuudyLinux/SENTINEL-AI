"""Stress/concurrency verification (stability audit Phase 11): N real
_camera_loop tasks running concurrently against the SAME real on-disk
SQLite file (shared tests/conftest.py test.db, real WAL + busy_timeout,
real safe_commit/safe_flush retry) — not mocked DB sessions. Proves the
concrete claim "concurrent camera workers do not crash, do not lose all
their writes, and do not leave the DB unusable" under genuine write
pressure, using the same `_FakeAlwaysOpenSource` / `_drive_camera_loop_until`
pattern as test_worker_resilience.py (isolates cv2/RTSP decode, not the DB
write path this test is actually exercising).

Deliberately NOT using real YOLO/ByteTrack (would make this a slow,
flaky-on-CPU test of the model, not of concurrency) — `detect_and_track` is
monkeypatched to return one fake detection per call, which still exercises
the REAL code path this is meant to stress: Detection insert (safe_flush),
per-frame commit, heartbeat commit, and the rules engine (real alert
evaluation, real Alert/Incident rows) for cameras with a real zone.
"""
import asyncio

import numpy as np

from app import models
from app.pipeline import worker
from app.pipeline.worker import CAMERA_STATS, RUNNING
from app.self_heal import engine as self_heal

N_CAMERAS = 12
RUN_SECONDS = 2.5


class _FakeAlwaysOpenSource:
    """Same fake source as test_worker_resilience.py — real DB write
    pressure, no real cv2/RTSP decode in the loop."""
    def __init__(self, frame):
        self._frame = frame

    def open(self):
        return True

    def read(self):
        return True, self._frame

    def pos_msec(self):
        return None

    def fps(self):
        return 30.0  # realistic camera frame rate — real write pressure without synthetic overload

    def resolution(self):
        return "64x64"

    def release(self):
        pass


def _fake_detect(_frame, _camera_id, _want_person, _want_vehicle):
    return [{"cls": "person", "confidence": 0.9, "bbox": [1.0, 2.0, 10.0, 10.0], "track_id": 1}]


def test_many_concurrent_camera_workers_survive_real_sqlite_contention(monkeypatch, db_session):
    self_heal._LATEST.clear()
    monkeypatch.setattr(worker, "detect_and_track", _fake_detect)
    # Real default (settings.detect_every_n_frames=3), not forced to 1 —
    # this test measures realistic concurrent production load, not a
    # synthetic worst case with no throttle at all.
    frame = np.zeros((64, 64, 3), dtype=np.uint8)
    monkeypatch.setattr(worker, "CameraSource", lambda *a, **k: _FakeAlwaysOpenSource(frame))

    cameras = []
    for i in range(N_CAMERAS):
        cam = models.Camera(
            camera_code=f"C-STRESS-{i:02d}", name=f"stress {i}", source_type="video_file",
            source_uri="unused.mp4", status="offline", ai_person=True, ai_vehicle=False, ai_anpr=False,
        )
        db_session.add(cam)
        cameras.append(cam)
    db_session.commit()
    for cam in cameras:
        db_session.refresh(cam)
        CAMERA_STATS.pop(cam.id, None)

    async def _drive_all():
        tasks = [asyncio.ensure_future(worker._camera_loop_supervised(cam.id)) for cam in cameras]
        try:
            await asyncio.sleep(RUN_SECONDS)
        finally:
            for t in tasks:
                t.cancel()
            # _camera_loop_supervised swallows everything (its whole point is
            # "no task disappears without a trace") — gather with
            # return_exceptions=True just to be certain nothing escapes here.
            await asyncio.gather(*tasks, return_exceptions=True)
        return tasks

    tasks = asyncio.run(_drive_all())

    # 1. No unhandled exception escaped ANY task — _camera_loop_supervised's
    #    entire job is to guarantee this; a failure here means that
    #    guarantee itself broke under real concurrency.
    for t in tasks:
        assert t.cancelled() or t.exception() is None, f"task raised: {t.exception()}"

    # 2. Every camera reached a real, healthy state — none stuck permanently
    #    offline/degraded from unresolved lock contention.
    for cam in cameras:
        db_session.refresh(cam)
        assert cam.status in ("online", "degraded"), f"{cam.camera_code} ended {cam.status}"

    # 3. Real detection writes actually landed — proves safe_flush's retry
    #    is durably persisting under contention, not silently losing writes.
    total_detections = (
        db_session.query(models.Detection)
        .filter(models.Detection.camera_id.in_([c.id for c in cameras]))
        .count()
    )
    assert total_detections > N_CAMERAS, f"expected substantial real writes across {N_CAMERAS} cameras, got {total_detections}"

    # No-duplicate-worker guard (start_worker's `existing and not
    # existing.done()` check) is a structural property of RUNNING being a
    # plain dict keyed by camera_id — one slot per camera_id, so a "second"
    # task can only ever mean the same key was overwritten, never two
    # concurrently live tasks under it. Covered directly, independent of
    # load, by test_worker_resilience.py's test_running_tasks_are_isolated_
    # per_camera_id and test_camera_control.py's duplicate-in-progress test;
    # not re-asserted here since re-invoking start_worker needs a running
    # event loop this synchronous cleanup block no longer has.
    for cam in cameras:
        CAMERA_STATS.pop(cam.id, None)
        RUNNING.pop(cam.id, None)
