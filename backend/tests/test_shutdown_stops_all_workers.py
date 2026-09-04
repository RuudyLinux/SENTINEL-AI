"""Regression tests for two real shutdown-leak bugs:

1. _on_startup starts workers directly for every webcam/video_file/mock_vms
   camera via start_worker(), completely bypassing the supervisor — those
   tasks are never registered in supervisor.AUTO_MANAGED. The old
   _on_shutdown() only called supervisor.stop_supervisor(), which only
   stops AUTO_MANAGED workers, so a directly-started camera's asyncio task
   and cv2.VideoCapture handle were just abandoned at process exit instead
   of going through _camera_loop's `finally: source.release()`. Fixed by
   having _on_shutdown also stop whatever is still left in worker.RUNNING
   after the supervisor is stopped.

2. (Final-review audit finding) stop_worker() only REQUESTS cancellation
   via task.cancel() — the task's own `finally: source.release()` only
   actually runs once the task is next scheduled, which is not guaranteed
   before uvicorn tears down the event loop unless something explicitly
   awaits it. _on_shutdown now collects stop_worker's returned tasks and
   `await`s them via asyncio.gather before returning, so cleanup is
   deterministic — proven below by asserting immediately after
   `await main._on_shutdown()` with NO manual poll loop (an earlier version
   of this test polled for up to 2s afterward specifically because the
   cleanup wasn't actually guaranteed to have finished yet).
"""
import asyncio

import numpy as np

from app import main, models
from app.pipeline import supervisor, worker


class _FakeAlwaysOpenSource:
    """Stands in for CameraSource: opens instantly, always has a frame
    ready, and records whether release() was actually called — the concrete
    proof that _camera_loop's cleanup path ran to completion rather than
    the task being silently abandoned."""
    def __init__(self, frame):
        self._frame = frame
        self.released = False

    def open(self):
        return True

    def read(self):
        return True, self._frame

    def pos_msec(self):
        return None

    def fps(self):
        return 200.0  # fast loop iteration for a quick test

    def resolution(self):
        return "64x64"

    def release(self):
        self.released = True


def test_shutdown_stops_a_directly_started_webcam_worker_not_just_supervisor_managed_ones(monkeypatch, db_session):
    """Simulates exactly what _on_startup does for a webcam/video_file/
    mock_vms camera: start_worker() called directly, never registered with
    the supervisor. _on_shutdown() must still stop it."""
    camera = models.Camera(
        camera_code="C-SHUTDOWN-DIRECT-TEST", name="test", source_type="webcam",
        source_uri="0", status="offline",
    )
    db_session.add(camera)
    db_session.commit()
    db_session.refresh(camera)

    frame = np.zeros((64, 64, 3), dtype=np.uint8)
    source = _FakeAlwaysOpenSource(frame)
    monkeypatch.setattr(worker, "CameraSource", lambda *a, **k: source)
    monkeypatch.setattr(worker, "detect_and_track", lambda *a, **k: [])

    worker.CAMERA_STATS.pop(camera.id, None)
    assert camera.id not in supervisor.AUTO_MANAGED  # never touched by the supervisor, by construction

    async def _drive():
        worker.start_worker(camera.id)  # exactly what _on_startup does for webcam/video_file/mock_vms
        for _ in range(150):
            await asyncio.sleep(0.02)
            if worker.CAMERA_STATS.get(camera.id, {}).get("grid_state") == "CONNECTED":
                break
        assert camera.id in worker.RUNNING  # actually running before we shut down

        await main._on_shutdown()
        # No manual poll loop here (deliberately) — _on_shutdown now awaits
        # every stopped camera's task via asyncio.gather internally, so
        # cleanup is guaranteed complete the instant this await returns.

    try:
        asyncio.run(_drive())
        # The actual bug: this camera was never in AUTO_MANAGED, so the old
        # _on_shutdown() (supervisor.stop_supervisor() only) left it running.
        assert camera.id not in worker.RUNNING
        assert source.released is True  # source.release() in _camera_loop's finally actually ran, deterministically
    finally:
        worker.stop_worker(camera.id)
        worker.CAMERA_STATS.pop(camera.id, None)
