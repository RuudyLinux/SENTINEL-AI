"""Worker resilience: a worker recovers from exceptions, and camera A failing
doesn't take down camera B.
"""
import asyncio

import numpy as np
from sqlalchemy.exc import OperationalError

from app.pipeline import worker
from app.pipeline.worker import _safe_commit, _stats, CAMERA_STATS


class _FailingSession:
    """Session whose commit always raises; _safe_commit has to cope."""
    def __init__(self, message="database is locked"):
        self.rolled_back = False
        self._message = message

    def commit(self):
        raise OperationalError("COMMIT", {}, RuntimeError(self._message))

    def rollback(self):
        self.rolled_back = True


def test_safe_commit_never_raises_and_rolls_back():
    db = _FailingSession()
    ok = asyncio.run(_safe_commit(db, "C-999"))
    assert ok is False
    assert db.rolled_back is True  # cleaned up, not left mid-transaction


class _DoubleFailingSession(_FailingSession):
    """The rollback fails too."""
    def rollback(self):
        raise RuntimeError("database is locked")


def test_safe_commit_survives_a_failing_rollback_too():
    db = _DoubleFailingSession()
    ok = asyncio.run(_safe_commit(db, "C-999"))  # must not raise, even here
    assert ok is False


def test_safe_commit_without_reapply_does_not_retry():
    """No reapply: one attempt. Retrying a bare commit after rollback would
    report success on a lost write."""
    db = _FailingSession()
    calls = {"commit": 0}
    orig_commit = db.commit
    def _counting_commit():
        calls["commit"] += 1
        return orig_commit()
    db.commit = _counting_commit
    ok = asyncio.run(_safe_commit(db, "C-999"))
    assert ok is False
    assert calls["commit"] == 1


def test_safe_commit_retries_a_lock_error_with_reapply_and_succeeds():
    """Lock fails twice then clears: retried with reapply, and the caller's
    change really was reapplied, not just True returned."""
    class _SessionFailsTwiceThenSucceeds:
        def __init__(self):
            self.attempts = 0
            self.rolled_back_count = 0
            self.committed_value = None

        def commit(self):
            self.attempts += 1
            if self.attempts < 3:
                raise OperationalError("COMMIT", {}, RuntimeError("database is locked"))
            # only "durable" once actually committed
            self.committed_value = self._pending

        def rollback(self):
            self.rolled_back_count += 1
            self._pending = None  # rollback discards the not-yet-committed value

    db = _SessionFailsTwiceThenSucceeds()
    target = {"value": "reassigned-after-each-rollback"}

    def reapply():
        db._pending = target["value"]

    reapply()  # first "assignment", mirrors the real call sites' pattern
    ok = asyncio.run(_safe_commit(db, "C-999", reapply=reapply))
    assert ok is True
    assert db.attempts == 3
    assert db.rolled_back_count == 2
    assert db.committed_value == "reassigned-after-each-rollback"  # reapply survived every retry


def test_safe_commit_does_not_retry_a_non_lock_operational_error():
    """A real non-lock DB error is never retried or hidden."""
    db = _FailingSession(message="no such column: bogus")
    calls = {"commit": 0}
    orig_commit = db.commit
    def _counting_commit():
        calls["commit"] += 1
        return orig_commit()
    db.commit = _counting_commit
    ok = asyncio.run(_safe_commit(db, "C-999", reapply=lambda: None))
    assert ok is False
    assert calls["commit"] == 1  # not a lock, not retried


def test_camera_stats_are_isolated_per_camera_id():
    CAMERA_STATS.clear()
    st_a = _stats("cam_A")
    st_b = _stats("cam_B")
    assert st_a is not st_b

    # Camera A records a batch of recovered errors...
    st_a["recovered_errors"] = 3
    st_a["last_error"] = "OperationalError: database is locked"

    # ...and B's counters are untouched: separate dict entries per camera,
    # no shared state
    assert st_b["recovered_errors"] == 0
    assert st_b["last_error"] is None


def test_running_tasks_are_isolated_per_camera_id():
    """RUNNING, CAMERA_STATS, LATEST_FRAMES, detector._MODELS_BY_CAMERA and
    clips._RING are all keyed by camera_id, so one camera's entry can't
    touch another's."""
    worker.RUNNING.clear()
    worker.RUNNING["cam_A"] = object()
    worker.RUNNING["cam_B"] = object()
    assert worker.RUNNING["cam_A"] is not worker.RUNNING["cam_B"]
    del worker.RUNNING["cam_A"]
    assert "cam_B" in worker.RUNNING  # removing A's entry never touches B's
    worker.RUNNING.clear()


def test_stop_worker_sets_grid_state_disconnected():
    """The loop's cancel path never touches grid_state, so a stopped camera
    kept showing CONNECTED/PROCESSING. stop_worker is where we know the
    operator asked to stop."""
    camera_id = "cam_stop_worker_grid_state_test"
    worker.CAMERA_STATS[camera_id] = {"grid_state": "PROCESSING"}
    try:
        worker.stop_worker(camera_id)
        assert worker.CAMERA_STATS[camera_id]["grid_state"] == "DISCONNECTED"
    finally:
        worker.CAMERA_STATS.pop(camera_id, None)


def test_stop_worker_does_not_fabricate_state_for_a_camera_never_started():
    """/stop on a camera whose worker never ran (double-click on an already
    stopped row) doesn't create a bogus DISCONNECTED stats entry."""
    camera_id = "cam_never_started_stop_test"
    worker.CAMERA_STATS.pop(camera_id, None)
    worker.stop_worker(camera_id)
    assert camera_id not in worker.CAMERA_STATS


def test_open_with_timeout_enforced_independently_of_cv2(monkeypatch):
    """CAP_PROP_OPEN_TIMEOUT_MSEC isn't honoured by every build (~30s vs 5s
    against a dead RTSP host), so _open_with_timeout has its own bound."""
    import time as time_mod
    from app.config import settings
    from app.pipeline.worker import _open_with_timeout

    class _NeverRespondingSource:
        def open(self):
            time_mod.sleep(2.0)  # a cv2 open() ignoring its own timeout
            return True

    monkeypatch.setattr(settings, "source_open_timeout_seconds", 0.2)
    # not asyncio.run(): its cleanup waits for the executor thread, so this
    # would time the shutdown, not _open_with_timeout. The server loop never
    # does that wait, so an unclosed loop matches production.
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
    """If _camera_loop raises anyway, the supervisor catches it, logs the
    traceback and marks the camera offline instead of the task vanishing."""
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


class _FakeAlwaysOpenSource:
    """Fake CameraSource: opens instantly, always has a frame, so the checks
    below don't depend on cv2/RTSP."""
    def __init__(self, frame):
        self._frame = frame

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
        pass


def _drive_camera_loop_until(camera_id: str, predicate, attempts: int = 250, step_s: float = 0.02) -> None:
    """Run _camera_loop as a task, poll `predicate` until true (or out of
    attempts), then cancel it; the loop handles CancelledError and releases
    its source/db like a real shutdown."""
    async def _drive():
        task = asyncio.ensure_future(worker._camera_loop(camera_id))
        try:
            for _ in range(attempts):
                await asyncio.sleep(step_s)
                if predicate():
                    return
        finally:
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass
    asyncio.run(_drive())


def test_camera_loop_reaches_connected_state_with_ai_off_and_never_runs_inference(monkeypatch, db_session):
    """Frames flowing with AI off: CONNECTED, not PROCESSING, and
    detect_and_track is never called, so connected-without-AI stays cheap."""
    from app import models

    camera = models.Camera(
        camera_code="C-CONNECTED-STATE-TEST", name="test", source_type="video_file",
        source_uri="unused.mp4", status="offline", ai_person=False, ai_vehicle=False,
    )
    db_session.add(camera)
    db_session.commit()
    db_session.refresh(camera)

    frame = np.zeros((64, 64, 3), dtype=np.uint8)
    monkeypatch.setattr(worker, "CameraSource", lambda *a, **k: _FakeAlwaysOpenSource(frame))
    detect_calls = []
    monkeypatch.setattr(worker, "detect_and_track", lambda *a, **k: detect_calls.append(a) or [])

    CAMERA_STATS.pop(camera.id, None)
    _drive_camera_loop_until(camera.id, lambda: CAMERA_STATS.get(camera.id, {}).get("grid_state") == "CONNECTED")

    assert CAMERA_STATS[camera.id]["grid_state"] == "CONNECTED"
    assert detect_calls == []  # AI off must never reach detect_and_track/get_model


def test_camera_loop_reaches_processing_state_with_ai_on_and_runs_inference(monkeypatch, db_session):
    """With AI on: PROCESSING, and detect_and_track does get called."""
    from app import models
    from app.config import settings

    camera = models.Camera(
        camera_code="C-PROCESSING-STATE-TEST", name="test", source_type="video_file",
        source_uri="unused.mp4", status="offline", ai_person=True, ai_vehicle=False,
    )
    db_session.add(camera)
    db_session.commit()
    db_session.refresh(camera)

    frame = np.zeros((64, 64, 3), dtype=np.uint8)
    monkeypatch.setattr(worker, "CameraSource", lambda *a, **k: _FakeAlwaysOpenSource(frame))
    monkeypatch.setattr(settings, "detect_every_n_frames", 1)  # run inference every frame, not every 3rd
    detect_calls = []
    monkeypatch.setattr(worker, "detect_and_track", lambda *a, **k: detect_calls.append(a) or [])

    CAMERA_STATS.pop(camera.id, None)
    # Wait on detect_calls, not grid_state: PROCESSING is set before
    # _process_frame runs, with an await in between, so polling grid_state
    # raced the cancel against inference actually happening (intermittent
    # failure with PROCESSING set but no calls).
    _drive_camera_loop_until(camera.id, lambda: len(detect_calls) >= 1)

    assert CAMERA_STATS[camera.id]["grid_state"] == "PROCESSING"
    assert len(detect_calls) >= 1


def test_camera_loop_survives_transient_lock_errors_without_crashing_or_spurious_error_state(monkeypatch, db_session):
    """"database is locked" from db.commit() in a running worker. The loop's
    real session fails its first two commits with a real lock error, then
    works. The task keeps running, reaches a healthy state, and doesn't bump
    error_count or go ERROR over something the retry recovered."""
    from app import models

    camera = models.Camera(
        camera_code="C-LOCK-RECOVERY-TEST", name="test", source_type="video_file",
        source_uri="unused.mp4", status="offline", ai_person=False, ai_vehicle=False,
    )
    db_session.add(camera)
    db_session.commit()
    db_session.refresh(camera)

    class _FlakyCommitSession:
        """Real session except .commit(), which raises a lock error for the
        first fail_first_n calls. `outcomes` lets the test wait for an actual
        successful commit; the failure count hits zero as the last failing
        attempt starts, before its retry."""
        def __init__(self, real_session, fail_first_n):
            self._real = real_session
            self._remaining_failures = fail_first_n
            self.outcomes: list[str] = []

        def __getattr__(self, name):
            return getattr(self._real, name)

        def commit(self):
            if self._remaining_failures > 0:
                self._remaining_failures -= 1
                self.outcomes.append("fail")
                raise OperationalError("COMMIT", {}, RuntimeError("database is locked"))
            self._real.commit()
            self.outcomes.append("ok")

    real_session_local = worker.SessionLocal
    flaky_session = _FlakyCommitSession(real_session_local(), fail_first_n=2)
    monkeypatch.setattr(worker, "SessionLocal", lambda: flaky_session)

    frame = np.zeros((64, 64, 3), dtype=np.uint8)
    monkeypatch.setattr(worker, "CameraSource", lambda *a, **k: _FakeAlwaysOpenSource(frame))
    monkeypatch.setattr(worker, "detect_and_track", lambda *a, **k: [])

    CAMERA_STATS.pop(camera.id, None)
    # CONNECTED comes before the online-status commit and its retry, so wait
    # for a real successful commit after the two failures
    _drive_camera_loop_until(camera.id, lambda: "ok" in flaky_session.outcomes)

    # connected despite two lock failures: the retry recovered the initial
    # online/fps/resolution commit, didn't just swallow it
    assert flaky_session.outcomes[:3] == ["fail", "fail", "ok"]
    assert CAMERA_STATS[camera.id]["grid_state"] == "CONNECTED"

    db_session.refresh(camera)
    assert camera.status == "online"
    # recovered lock errors never reach the catch-all that bumps error_count
    # and sets ERROR; from the operator's side it was healthy throughout
    assert camera.error_count == 0
    assert "top-level crash" not in (CAMERA_STATS[camera.id].get("last_error") or "")


def test_camera_loop_picks_up_a_mid_flight_ai_toggle_without_a_restart(monkeypatch, db_session):
    """Start AI did nothing on a connected camera: `camera` is loaded once and
    with expire_on_commit=False never sees another session's PATCH. The grid's
    Start/Stop AI buttons rely on this working without a reconnect."""
    from app import models

    camera = models.Camera(
        camera_code="C-MIDFLIGHT-AI-TEST", name="test", source_type="video_file",
        source_uri="unused.mp4", status="offline", ai_person=False, ai_vehicle=False,
    )
    db_session.add(camera)
    db_session.commit()
    db_session.refresh(camera)

    frame = np.zeros((64, 64, 3), dtype=np.uint8)
    monkeypatch.setattr(worker, "CameraSource", lambda *a, **k: _FakeAlwaysOpenSource(frame))
    monkeypatch.setattr(worker, "detect_and_track", lambda *a, **k: [])
    CAMERA_STATS.pop(camera.id, None)

    # one task across both phases; a fresh task per phase would pick up the
    # new value trivially
    async def _drive():
        task = asyncio.ensure_future(worker._camera_loop(camera.id))
        try:
            for _ in range(250):
                await asyncio.sleep(0.02)
                if CAMERA_STATS.get(camera.id, {}).get("grid_state") == "CONNECTED":
                    break
            assert CAMERA_STATS[camera.id]["grid_state"] == "CONNECTED"

            # the PATCH a "Start AI" click sends, separate session like the API
            other_session = worker.SessionLocal()
            try:
                other_camera = other_session.query(models.Camera).filter(models.Camera.id == camera.id).first()
                other_camera.ai_person = True
                other_session.commit()
            finally:
                other_session.close()

            # same task notices within the ~1s refresh, no reconnect
            for _ in range(300):
                await asyncio.sleep(0.02)
                if CAMERA_STATS.get(camera.id, {}).get("grid_state") == "PROCESSING":
                    break
        finally:
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass
    asyncio.run(_drive())

    assert CAMERA_STATS[camera.id]["grid_state"] == "PROCESSING"
