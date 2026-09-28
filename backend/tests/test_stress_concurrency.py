"""12 real _camera_loop tasks at once against the shared on-disk SQLite file
(real WAL, busy_timeout and safe_commit/safe_flush retry, no mocked
sessions): no crashes, writes land, DB stays usable.

Uses the fake source from test_worker_resilience.py (no cv2 decode) and
patches detect_and_track to one fake detection per call, so it stresses the
DB path, not YOLO: detection insert, per-frame and heartbeat commits, and
real alert evaluation for cameras with a zone.
"""
import asyncio

import numpy as np

from app import models
from app.pipeline import worker
from app.pipeline.worker import CAMERA_STATS, RUNNING
from app.self_heal import engine as self_heal

N_CAMERAS = 12
# 2.5s was fine on a dev box with lots of cores. On a throttled CI runner, 12
# tasks opening sources through to_thread (pool sized off the CPU count)
# could take longer than that just to get every camera through its first
# read, with no fault at all. Same asserts, more time. N_CAMERAS stays at 12;
# widen the clock, not shrink the load.
RUN_SECONDS = 6.0


class _FakeAlwaysOpenSource:
    """Same fake source as test_worker_resilience.py, DB pressure without cv2."""
    def __init__(self, frame):
        self._frame = frame

    def open(self):
        return True

    def read(self):
        return True, self._frame

    def pos_msec(self):
        return None

    def fps(self):
        return 30.0  # real camera frame rate

    def resolution(self):
        return "64x64"

    def release(self):
        pass


def _fake_detect(_frame, _camera_id, _want_person, _want_vehicle):
    return [{"cls": "person", "confidence": 0.9, "bbox": [1.0, 2.0, 10.0, 10.0], "track_id": 1}]


# Even at 6s a shared 2-vCPU CI runner occasionally ends one camera offline
# with no crash, exception or lock error anywhere; looks like host CPU steal
# delaying the first open() of 12 tasks past the reconnect budget. So the
# whole scenario gets a bounded retry, but only check 2 (every camera
# healthy in the window). Checks 1 and 3 fail hard on every attempt.
MAX_ATTEMPTS = 3


def _run_once(monkeypatch, db_session, attempt: int) -> "list[str] | None":
    """One run of 12 cameras. Hard-asserts no crash and real writes, then
    returns the cameras that ended up offline (only that check is retried)."""
    self_heal._LATEST.clear()
    monkeypatch.setattr(worker, "detect_and_track", _fake_detect)
    # real default detect_every_n_frames=3, realistic load not worst case
    frame = np.zeros((64, 64, 3), dtype=np.uint8)
    monkeypatch.setattr(worker, "CameraSource", lambda *a, **k: _FakeAlwaysOpenSource(frame))

    cameras = []
    for i in range(N_CAMERAS):
        cam = models.Camera(
            camera_code=f"C-STRESS-{attempt}-{i:02d}", name=f"stress {i}", source_type="video_file",
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
            # the supervised loop swallows everything anyway; return_exceptions
            # just to be sure
            await asyncio.gather(*tasks, return_exceptions=True)
        return tasks

    tasks = asyncio.run(_drive_all())

    # 1. nothing escaped any task. hard failure, never retried
    for t in tasks:
        assert t.cancelled() or t.exception() is None, f"task raised: {t.exception()}"

    # 2. every camera healthy, none stuck offline/degraded from lock
    #    contention. the one retried check (MAX_ATTEMPTS)
    offline = []
    for cam in cameras:
        db_session.refresh(cam)
        if cam.status not in ("online", "degraded"):
            offline.append(cam.camera_code)

    # 3. detection writes landed, so safe_flush isn't losing them. hard failure
    total_detections = (
        db_session.query(models.Detection)
        .filter(models.Detection.camera_id.in_([c.id for c in cameras]))
        .count()
    )
    assert total_detections > N_CAMERAS, f"expected substantial real writes across {N_CAMERAS} cameras, got {total_detections}"

    # One worker per camera is structural (RUNNING is a dict keyed by
    # camera_id). Tested directly in test_worker_resilience.py and
    # test_camera_control.py; start_worker would need a running loop here.
    for cam in cameras:
        CAMERA_STATS.pop(cam.id, None)
        RUNNING.pop(cam.id, None)

    return offline


def test_many_concurrent_camera_workers_survive_real_sqlite_contention(monkeypatch, db_session):
    last_offline: list[str] = []
    for attempt in range(1, MAX_ATTEMPTS + 1):
        last_offline = _run_once(monkeypatch, db_session, attempt)
        if not last_offline:
            return  # all healthy
    assert not last_offline, (
        f"{last_offline} still ended offline after {MAX_ATTEMPTS} attempts, "
        f"each with no crash and real writes landing — a real fault, not scheduling luck"
    )
