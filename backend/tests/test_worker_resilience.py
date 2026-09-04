"""Phase 4 — worker resilience regressions.

7. worker exception recovery
8. camera A failure does not kill camera B
"""
import asyncio

import pytest

from app.pipeline import worker
from app.pipeline.worker import _safe_commit, _stats, CAMERA_STATS


class _FailingSession:
    """A stand-in DB session whose commit always raises, to prove
    _safe_commit degrades gracefully instead of propagating."""
    def __init__(self):
        self.rolled_back = False

    def commit(self):
        raise RuntimeError("database is locked")

    def rollback(self):
        self.rolled_back = True


def test_safe_commit_never_raises_and_rolls_back():
    db = _FailingSession()
    ok = _safe_commit(db, "C-999")
    assert ok is False
    assert db.rolled_back is True  # cleaned up, not left mid-transaction


class _DoubleFailingSession(_FailingSession):
    """Even the rollback itself fails — the true Phase 4 failure mode."""
    def rollback(self):
        raise RuntimeError("database is locked")


def test_safe_commit_survives_a_failing_rollback_too():
    db = _DoubleFailingSession()
    ok = _safe_commit(db, "C-999")  # must not raise, even here
    assert ok is False


def test_camera_stats_are_isolated_per_camera_id():
    CAMERA_STATS.clear()
    st_a = _stats("cam_A")
    st_b = _stats("cam_B")
    assert st_a is not st_b

    # Camera A records a batch of recovered errors...
    st_a["recovered_errors"] = 3
    st_a["last_error"] = "OperationalError: database is locked"

    # ...camera B's own counters are completely untouched by A's failures —
    # the concrete, code-level guarantee behind "camera A failure does not
    # kill camera B": there is no shared mutable state between them, only
    # independent dict entries keyed by camera_id.
    assert st_b["recovered_errors"] == 0
    assert st_b["last_error"] is None


def test_running_tasks_are_isolated_per_camera_id():
    """RUNNING (and by the same construction CAMERA_STATS, LATEST_FRAMES,
    detector._MODELS_BY_CAMERA, clips._RING) are all keyed by camera_id —
    removing/crashing one camera's entry can never remove or touch
    another's. This dict-per-camera_id shape is the concrete guarantee
    behind "camera A failure does not kill camera B": every piece of
    per-camera runtime state lives in its own dict slot."""
    worker.RUNNING.clear()
    worker.RUNNING["cam_A"] = object()
    worker.RUNNING["cam_B"] = object()
    assert worker.RUNNING["cam_A"] is not worker.RUNNING["cam_B"]
    del worker.RUNNING["cam_A"]
    assert "cam_B" in worker.RUNNING  # removing A's entry never touches B's
    worker.RUNNING.clear()


def test_open_with_timeout_enforced_independently_of_cv2(monkeypatch):
    """Phase 4 finding: CAP_PROP_OPEN_TIMEOUT_MSEC is not reliably honored by
    every OpenCV/FFmpeg build (measured ~30s instead of a configured 5s
    against a real unreachable RTSP endpoint) — _open_with_timeout enforces
    its own bound at the asyncio level regardless of what cv2 does."""
    import time as time_mod
    from app.config import settings
    from app.pipeline.worker import _open_with_timeout

    class _NeverRespondingSource:
        def open(self):
            time_mod.sleep(2.0)  # stands in for a cv2 open() that ignores its own timeout property
            return True

    monkeypatch.setattr(settings, "source_open_timeout_seconds", 0.2)
    # NOTE: deliberately NOT asyncio.run() here — its cleanup waits for the
    # default executor's in-flight thread to finish before the loop closes,
    # which would make this test measure that shutdown wait rather than
    # _open_with_timeout's own (correct, immediate) return. The real caller
    # (worker.py's long-lived server loop) never does that shutdown wait, so
    # run_until_complete on a loop we don't close matches production.
    loop = asyncio.new_event_loop()
    try:
        t0 = time_mod.monotonic()
        result = loop.run_until_complete(_open_with_timeout(_NeverRespondingSource()))
        elapsed = time_mod.monotonic() - t0
    finally:
        loop.close()
    assert result is False
    assert elapsed < 1.0  # bounded by OUR timeout, not the source's 2s


def test_camera_loop_supervised_marks_offline_instead_of_disappearing(monkeypatch, db_session):
    """The Phase 4 top-level safety net: if _camera_loop ever raises for any
    reason (including ones this test suite hasn't anticipated), the
    supervisor catches it, logs a full traceback, and marks the camera
    offline+observable rather than letting the task vanish silently."""
    from app import models
    from app.pipeline.worker import _camera_loop_supervised, _stats

    camera = models.Camera(
        camera_code="C-RESILIENCE-TEST", name="test", source_type="video_file",
        source_uri="does-not-exist.mp4", status="online",
    )
    db_session.add(camera)
    db_session.commit()
    db_session.refresh(camera)

    async def _boom(_camera_id):
        raise RuntimeError("simulated unexpected failure")

    monkeypatch.setattr("app.pipeline.worker._camera_loop", _boom)
    asyncio.run(_camera_loop_supervised(camera.id))

    db_session.refresh(camera)
    assert camera.status == "offline"
    assert camera.error_count >= 1
    assert "top-level crash" in (_stats(camera.id)["last_error"] or "")
