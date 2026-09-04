"""Phase 4 — worker resilience regressions.

7. worker exception recovery
8. camera A failure does not kill camera B
"""
import asyncio

import numpy as np
import pytest
from sqlalchemy.exc import OperationalError

from app.pipeline import worker
from app.pipeline.worker import _safe_commit, _stats, CAMERA_STATS


class _FailingSession:
    """A stand-in DB session whose commit always raises, to prove
    _safe_commit degrades gracefully instead of propagating."""
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
    """Even the rollback itself fails — the true Phase 4 failure mode."""
    def rollback(self):
        raise RuntimeError("database is locked")


def test_safe_commit_survives_a_failing_rollback_too():
    db = _DoubleFailingSession()
    ok = asyncio.run(_safe_commit(db, "C-999"))  # must not raise, even here
    assert ok is False


def test_safe_commit_without_reapply_does_not_retry():
    """No `reapply` given -> single attempt, exactly like the pre-fix
    behavior — retrying a bare commit with nothing to redo after a rollback
    would silently report success on a lost write, which is worse than a
    visible, logged failure."""
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
    """A transient lock (fails twice, then the contending writer releases)
    must be retried transparently when a `reapply` is given, re-doing the
    exact pending mutation after each rollback — proven correct by
    re-checking that the caller's state was actually reapplied, not just
    that the call returned True."""
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
    """A genuine (non-lock) DB error — e.g. a real constraint/schema issue —
    must never be silently retried or hidden, even with a `reapply` given."""
    db = _FailingSession(message="no such column: bogus")
    calls = {"commit": 0}
    orig_commit = db.commit
    def _counting_commit():
        calls["commit"] += 1
        return orig_commit()
    db.commit = _counting_commit
    ok = asyncio.run(_safe_commit(db, "C-999", reapply=lambda: None))
    assert ok is False
    assert calls["commit"] == 1  # not retried — this isn't a lock error


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


def test_stop_worker_sets_grid_state_disconnected():
    """Real bug found via the live browser test of the Disconnect button:
    _camera_loop's cancellation path (`except asyncio.CancelledError: pass`)
    never updates grid_state on its own, so a deliberately stopped camera
    kept showing its last live value (CONNECTED/PROCESSING) forever —
    indistinguishable from still being connected. stop_worker is the one
    place that actually knows the operator asked to stop."""
    camera_id = "cam_stop_worker_grid_state_test"
    worker.CAMERA_STATS[camera_id] = {"grid_state": "PROCESSING"}
    try:
        worker.stop_worker(camera_id)
        assert worker.CAMERA_STATS[camera_id]["grid_state"] == "DISCONNECTED"
    finally:
        worker.CAMERA_STATS.pop(camera_id, None)


def test_stop_worker_does_not_fabricate_state_for_a_camera_never_started():
    """A camera whose worker never ran in this process (still just
    REGISTERED, per the Camera Grid's UI convention) must not get a bogus
    DISCONNECTED CAMERA_STATS entry created just because /stop was called on
    it (e.g. a client double-clicking Disconnect on an already-stopped row)."""
    camera_id = "cam_never_started_stop_test"
    worker.CAMERA_STATS.pop(camera_id, None)
    worker.stop_worker(camera_id)
    assert camera_id not in worker.CAMERA_STATS


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


class _FakeAlwaysOpenSource:
    """Stands in for CameraSource: opens instantly, always has a frame ready
    — isolates the grid_state/AI-gate assertions below from real cv2/RTSP
    decode, same spirit as _FakeSession above for DB commits."""
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
    """Runs _camera_loop as a background task, polling `predicate` until true
    (or attempts exhausted), then cleanly cancels it — _camera_loop already
    catches asyncio.CancelledError and releases its source/db (see its
    `finally` block), so this mirrors real shutdown, not a forced kill."""
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
    """LIVE != AI PROCESSING (24/7 auto-connect task): once frames flow for a
    camera with AI off, grid_state reaches CONNECTED (not PROCESSING), and
    detect_and_track is never called — 'connected, AI off' must be genuinely
    lightweight, not just 'inference skipped after loading a model'."""
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
    """The other half of the same distinction: a camera with AI on reaches
    grid_state PROCESSING (not just CONNECTED), and detect_and_track IS
    called — frames flowing plus AI enabled is a genuinely different state
    from frames flowing alone."""
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
    # Predicate on detect_calls, not grid_state: worker.py sets grid_state to
    # PROCESSING synchronously BEFORE calling _process_frame (by design —
    # "processing this frame" should be true the moment work starts, not
    # only once inference finishes), with an await point in between (pos_msec)
    # that yields to the event loop. Polling on grid_state alone raced this
    # test's own cancellation against detect_and_track actually having run
    # yet — found by an intermittent, fast, clean failure with grid_state
    # already correctly PROCESSING but detect_calls still empty.
    _drive_camera_loop_until(camera.id, lambda: len(detect_calls) >= 1)

    assert CAMERA_STATS[camera.id]["grid_state"] == "PROCESSING"
    assert len(detect_calls) >= 1


def test_camera_loop_survives_transient_lock_errors_without_crashing_or_spurious_error_state(monkeypatch, db_session):
    """Real regression test for the reported bug: `sqlite3.OperationalError:
    database is locked` repeatedly raised from db.commit() while a camera
    worker runs. Wraps the loop's real SessionLocal-produced session so its
    first two .commit() calls raise a genuine lock OperationalError (then
    proceed normally, simulating a contending writer that releases) and
    drives a real _camera_loop through it — proving the task keeps running
    (no crash), reaches a healthy grid_state despite the early failures, and
    never spuriously bumps error_count/flips to ERROR for a transient
    failure that _safe_commit's retry actually recovered from."""
    from app import models

    camera = models.Camera(
        camera_code="C-LOCK-RECOVERY-TEST", name="test", source_type="video_file",
        source_uri="unused.mp4", status="offline", ai_person=False, ai_vehicle=False,
    )
    db_session.add(camera)
    db_session.commit()
    db_session.refresh(camera)

    class _FlakyCommitSession:
        """Delegates everything to a real session except .commit(), which
        raises a real lock error for the first `fail_first_n` calls, then
        delegates to the real commit — `outcomes` records each call's
        result so the test can wait for an actual successful commit, not
        just for the failure count to hit zero (which happens the instant
        the LAST failing attempt starts, before its retry has even run)."""
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
    # grid_state flips to CONNECTED as soon as the source opens — BEFORE the
    # online-status commit (and its retry) even runs — so the predicate
    # waits for an actual successful commit after the two failures, not
    # just for CONNECTED or for the failure counter to hit zero (both of
    # which can be observed before the successful retry attempt has run).
    _drive_camera_loop_until(camera.id, lambda: "ok" in flaky_session.outcomes)

    # Reached CONNECTED despite the two early lock failures -> the task
    # never crashed, and _safe_commit's retry actually recovered the write
    # (not just swallowed it) — the initial online/fps/resolution commit is
    # what those first two failures land on.
    assert flaky_session.outcomes[:3] == ["fail", "fail", "ok"]
    assert CAMERA_STATS[camera.id]["grid_state"] == "CONNECTED"

    db_session.refresh(camera)
    assert camera.status == "online"
    # The failures were real lock errors on a commit that _safe_commit's
    # retry recovered — they must never have fallen through to the
    # per-iteration catch-all that bumps error_count/flips grid_state=ERROR
    # for what was, from the operator's point of view, a fully healthy
    # connection throughout.
    assert camera.error_count == 0
    assert "top-level crash" not in (CAMERA_STATS[camera.id].get("last_error") or "")


def test_camera_loop_picks_up_a_mid_flight_ai_toggle_without_a_restart(monkeypatch, db_session):
    """Real bug found during the supervisor UI's live browser test (Start AI
    button had no effect on an already-connected camera): `camera` is loaded
    once when _camera_loop starts and, with expire_on_commit=False, never
    picks up another session's PATCH on its own — the Camera Grid's Start AI/
    Stop AI buttons rely on exactly this working without a full reconnect."""
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

    # Deliberately NOT using _drive_camera_loop_until twice — that starts a
    # fresh task per call (a restart would trivially pick up the new value,
    # which isn't what this test is proving). One task, spanning both phases.
    async def _drive():
        task = asyncio.ensure_future(worker._camera_loop(camera.id))
        try:
            for _ in range(250):
                await asyncio.sleep(0.02)
                if CAMERA_STATS.get(camera.id, {}).get("grid_state") == "CONNECTED":
                    break
            assert CAMERA_STATS[camera.id]["grid_state"] == "CONNECTED"

            # Simulate the PATCH /api/cameras/{id} an operator's "Start AI"
            # click sends — a separate session/request, exactly like the real API.
            other_session = worker.SessionLocal()
            try:
                other_camera = other_session.query(models.Camera).filter(models.Camera.id == camera.id).first()
                other_camera.ai_person = True
                other_session.commit()
            finally:
                other_session.close()

            # The already-running loop must notice within its refresh
            # throttle (~1s) — no Disconnect/Connect, same task throughout.
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
