"""Camera worker lifecycle: cancellation must be a clean stop, not a fault.

The bug that motivated this file: cancelling a worker mid-commit let
`_camera_loop`'s `finally: db.close()` race the commit still running on a
worker thread, raising IllegalStateChangeError. Coming from a `finally`, that
escaped to `_camera_loop_supervised`, which marked a perfectly HEALTHY camera
offline and logged a critical self-heal event — on an ordinary stop.

These tests assert the lifecycle contract directly:

    RUNNING -> STOPPING -> STOPPED      (never -> ERROR/OFFLINE)
"""
import asyncio

import numpy as np

from app import models
from app.pipeline import worker
from app.pipeline.worker import CAMERA_STATS, RUNNING


class _FakeAlwaysOpenSource:
    """Real frames, no cv2 — the DB/lifecycle path is what is under test."""

    def __init__(self, frame):
        self._frame = frame

    def open(self):
        return True

    def read(self):
        return True, self._frame

    def pos_msec(self):
        return None

    def fps(self):
        return 30.0

    def resolution(self):
        return "64x64"

    def release(self):
        pass


def _camera(db_session, code: str, **kwargs) -> models.Camera:
    camera = models.Camera(
        camera_code=code, name="lifecycle test", source_type="video_file",
        source_uri="unused.mp4", status="offline", **kwargs,
    )
    db_session.add(camera)
    db_session.commit()
    db_session.refresh(camera)
    CAMERA_STATS.pop(camera.id, None)
    return camera


def _run_then_cancel(camera_id: str, run_for: float = 0.4):
    """Bring a worker to a healthy running state, then cancel it the way a real
    stop_worker/shutdown does, and report what the camera looks like after."""

    async def scenario():
        task = asyncio.ensure_future(worker._camera_loop_supervised(camera_id))
        await asyncio.sleep(run_for)
        running_state = CAMERA_STATS.get(camera_id, {}).get("grid_state")
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass
        return running_state, task

    return asyncio.run(scenario())


class TestNormalStop:
    def test_cancelling_a_healthy_worker_does_not_mark_the_camera_offline(self, monkeypatch, db_session):
        """The exact regression. A stop is not a failure."""
        camera = _camera(db_session, "C-LIFE-STOP", ai_person=True, ai_vehicle=False, ai_anpr=False)
        frame = np.zeros((64, 64, 3), dtype=np.uint8)
        monkeypatch.setattr(worker, "CameraSource", lambda *a, **k: _FakeAlwaysOpenSource(frame))
        monkeypatch.setattr(
            worker, "detect_and_track",
            lambda *a, **k: [{"cls": "person", "confidence": 0.9, "bbox": [1.0, 2.0, 10.0, 10.0], "track_id": 1}],
        )

        running_state, task = _run_then_cancel(camera.id)

        assert running_state in ("PROCESSING", "CONNECTED"), "precondition: it was healthy before the stop"
        db_session.refresh(camera)
        assert camera.status != "offline", (
            "an ordinary cancellation must not mark a healthy camera offline - "
            "that was the IllegalStateChangeError escaping the camera loop finally"
        )
        assert task.cancelled() or task.exception() is None

    def test_cancellation_does_not_raise_out_of_the_worker(self, monkeypatch, db_session):
        camera = _camera(db_session, "C-LIFE-NORAISE", ai_person=True, ai_vehicle=False, ai_anpr=False)
        frame = np.zeros((64, 64, 3), dtype=np.uint8)
        monkeypatch.setattr(worker, "CameraSource", lambda *a, **k: _FakeAlwaysOpenSource(frame))
        monkeypatch.setattr(worker, "detect_and_track", lambda *a, **k: [])

        _, task = _run_then_cancel(camera.id, run_for=0.25)

        assert task.cancelled() or task.exception() is None

    def test_stop_worker_reports_stopped_state_not_a_stale_running_one(self, monkeypatch, db_session):
        """A deliberately stopped camera must not keep showing PROCESSING -
        indistinguishable from still being connected."""
        camera = _camera(db_session, "C-LIFE-STATE", ai_person=True, ai_vehicle=False, ai_anpr=False)
        frame = np.zeros((64, 64, 3), dtype=np.uint8)
        monkeypatch.setattr(worker, "CameraSource", lambda *a, **k: _FakeAlwaysOpenSource(frame))
        monkeypatch.setattr(worker, "detect_and_track", lambda *a, **k: [])

        async def scenario():
            worker.start_worker(camera.id)
            await asyncio.sleep(0.3)
            task = worker.stop_worker(camera.id)
            if task is not None:
                try:
                    await task
                except asyncio.CancelledError:
                    pass

        asyncio.run(scenario())

        assert CAMERA_STATS[camera.id]["grid_state"] == "DISCONNECTED"
        assert camera.id not in RUNNING, "a stopped camera must not linger in RUNNING"


class TestResourceRelease:
    def test_stopping_releases_every_per_camera_resource(self, monkeypatch, db_session):
        """Model, clip buffer and plate-vote accumulator are all per camera and
        must all be dropped together - a leak here is unbounded memory on a
        long-running control room that starts and stops cameras."""
        from app.pipeline import clips, plate_tracker

        camera = _camera(db_session, "C-LIFE-RELEASE")
        released = {"model": False, "clips": False, "plates": False}
        monkeypatch.setattr(worker, "release_model", lambda cid: released.__setitem__("model", True))
        monkeypatch.setattr(clips, "release_camera", lambda cid: released.__setitem__("clips", True))
        monkeypatch.setattr(plate_tracker, "release_camera", lambda cid: released.__setitem__("plates", True))

        worker.stop_worker(camera.id)

        assert all(released.values()), f"not every per-camera resource was released: {released}"

    def test_the_frame_buffer_is_dropped_on_stop(self, db_session):
        """LATEST_FRAMES feeds the MJPEG endpoint. A stale frame left behind
        would let a stopped camera keep serving a live-looking image."""
        camera = _camera(db_session, "C-LIFE-FRAME")
        worker.LATEST_FRAMES[camera.id] = b"stale-jpeg"

        worker.stop_worker(camera.id)

        assert camera.id not in worker.LATEST_FRAMES


class TestFailurePathStillWorks:
    def test_a_genuinely_unopenable_source_does_mark_the_camera_offline(self, monkeypatch, db_session):
        """The fix must not have made the worker unable to report real failure.
        A source that never opens is a real fault and must still go offline."""

        class _NeverOpens(_FakeAlwaysOpenSource):
            def open(self):
                return False

        camera = _camera(db_session, "C-LIFE-FAIL")
        monkeypatch.setattr(worker, "CameraSource", lambda *a, **k: _NeverOpens(None))
        # Collapse the reconnect budget so the test does not sit through the
        # real exponential backoff.
        monkeypatch.setattr(worker.settings, "reconnect_max_attempts", 1)
        monkeypatch.setattr(worker.settings, "reconnect_backoff_base", 0.01)
        monkeypatch.setattr(worker.settings, "reconnect_backoff_max", 0.01)

        asyncio.run(worker._camera_loop_supervised(camera.id))

        db_session.refresh(camera)
        assert camera.status == "offline"
        assert CAMERA_STATS[camera.id]["grid_state"] in ("DISCONNECTED", "AUTH_ERROR", "ERROR")
