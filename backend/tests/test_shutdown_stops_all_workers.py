"""Two shutdown leaks:

1. Startup starts local cameras with start_worker() directly, outside the
   supervisor, and shutdown only stopped supervisor-managed workers, so
   those tasks and their VideoCaptures were abandoned. Shutdown now also
   stops whatever is left in RUNNING.
2. stop_worker() only requests cancellation; the release in the task's
   finally runs when it's next scheduled, which may be never. Shutdown now
   gathers the tasks, so the asserts below run right after
   `await main._on_shutdown()` with no polling.
"""
import asyncio

import numpy as np

from app import main, models
from app.pipeline import supervisor, worker


class _FakeAlwaysOpenSource:
    """Fake CameraSource: opens instantly, always has a frame, records
    whether release() ran."""
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
    """Like startup does for a local camera: start_worker directly, not via
    the supervisor. Shutdown still has to stop it."""
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
        worker.start_worker(camera.id)  # what _on_startup does for local cameras
        for _ in range(150):
            await asyncio.sleep(0.02)
            if worker.CAMERA_STATS.get(camera.id, {}).get("grid_state") == "CONNECTED":
                break
        assert camera.id in worker.RUNNING  # actually running before we shut down

        await main._on_shutdown()
        # no polling, _on_shutdown gathers the stopped tasks itself

    try:
        asyncio.run(_drive())
        # The actual bug: this camera was never in AUTO_MANAGED, so the old
        # _on_shutdown() (supervisor.stop_supervisor() only) left it running.
        assert camera.id not in worker.RUNNING
        assert source.released is True  # the loop's finally really ran
    finally:
        worker.stop_worker(camera.id)
        worker.CAMERA_STATS.pop(camera.id, None)
