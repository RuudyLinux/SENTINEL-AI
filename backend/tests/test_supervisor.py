"""Grid auto-connect supervisor. No network: worker.start_worker/stop_worker
are faked throughout; this is about the supervisor's own scheduling
(eligibility, cap, cooldown, dedup, shutdown), not the RTSP pipeline."""
import asyncio
import time
import uuid

import pytest

from app import config, models
from app.pipeline import supervisor
from app.pipeline import worker


class _FakeTask:
    def __init__(self, done: bool = False):
        self._done = done

    def done(self) -> bool:
        return self._done

    def cancel(self) -> None:
        self._done = True

    def __await__(self):
        # stop_worker returns the cancelled task so callers can gather it, so
        # the fake has to be awaitable too (gather raises TypeError otherwise).
        # resolves immediately, like an already-finished task
        return iter(())


@pytest.fixture(autouse=True)
def _clean_supervisor_state(monkeypatch):
    """Clean slate per test; these are module-level dicts otherwise shared."""
    supervisor.AUTO_MANAGED.clear()
    supervisor.OPERATOR_DISCONNECTED.clear()
    supervisor._last_restart_attempt.clear()
    supervisor._grid_wide_cooldown_until = 0.0
    supervisor._grid_trips = 0
    supervisor._probe_camera = None
    worker.CAMERA_STATS.clear()
    monkeypatch.setattr(config.settings, "sentinel_grid_email", "someone@example.com")
    monkeypatch.setattr(config.settings, "sentinel_grid_password", "correct-password")
    monkeypatch.setattr(config.settings, "sentinel_grid_autoconnect", True)
    monkeypatch.setattr(config.settings, "sentinel_grid_max_autoconnect", 5)
    monkeypatch.setattr(config.settings, "sentinel_grid_auth_cooldown_seconds", 300.0)
    # 0 by default so the scheduling tests are instant; the stagger tests
    # set a small real delay
    monkeypatch.setattr(config.settings, "sentinel_grid_stagger_seconds", 0.0)
    yield
    supervisor.AUTO_MANAGED.clear()
    supervisor.OPERATOR_DISCONNECTED.clear()
    supervisor._last_restart_attempt.clear()
    supervisor._grid_wide_cooldown_until = 0.0
    supervisor._grid_trips = 0
    supervisor._probe_camera = None
    worker.CAMERA_STATS.clear()


def _grid_camera(db_session, code=None, **kwargs):
    code = code or f"SUP-{uuid.uuid4().hex[:8]}"
    cam = models.Camera(
        camera_code=code, name="Supervisor Test Cam", source_type="sentinel_grid",
        source_uri=code.lower(), status="offline", **kwargs,
    )
    db_session.add(cam)
    db_session.commit()
    db_session.refresh(cam)
    return cam


def test_eligible_camera_ids_real_grid_only_not_simulated_or_other_sources(db_session):
    """Only source_type == 'sentinel_grid' is eligible; mock_vms or webcam isn't."""
    grid_cam = _grid_camera(db_session)
    other_cam = models.Camera(camera_code=f"SUP-OTHER-{uuid.uuid4().hex[:6]}", name="x", source_type="mock_vms", source_uri="")
    db_session.add(other_cam)
    db_session.commit()

    ids = supervisor._eligible_camera_ids(db_session)
    assert grid_cam.id in ids
    assert other_cam.id not in ids


def test_eligible_camera_ids_excludes_stale(db_session):
    cam = _grid_camera(db_session, catalog_stale=True)
    assert cam.id not in supervisor._eligible_camera_ids(db_session)


def test_connect_eligible_respects_concurrency_cap(monkeypatch, db_session):
    """Never more than sentinel_grid_max_autoconnect at once, even with more
    eligible cameras."""
    monkeypatch.setattr(config.settings, "sentinel_grid_max_autoconnect", 2)
    cams = [_grid_camera(db_session) for _ in range(4)]

    started_ids: list[str] = []

    def fake_start_worker(camera_id: str) -> None:
        started_ids.append(camera_id)
        worker.RUNNING[camera_id] = _FakeTask(done=False)

    monkeypatch.setattr(supervisor.worker, "start_worker", fake_start_worker)
    # only this test's 4 cameras; the shared DB has grid rows from other files
    # (_eligible_camera_ids itself is tested above)
    monkeypatch.setattr(supervisor, "_eligible_camera_ids", lambda db: [c.id for c in cams])

    started = asyncio.run(supervisor._connect_eligible(db_session))
    assert started == 2
    assert len(started_ids) == 2
    assert all(cid in {c.id for c in cams} for cid in started_ids)


def test_connect_eligible_staggers_successive_starts(monkeypatch, db_session):
    """Opening N RTSP connections at once was measurably less reliable than
    one at a time, so a real delay has to pass between starts inside one
    sweep, not only between sweeps."""
    monkeypatch.setattr(config.settings, "sentinel_grid_max_autoconnect", 3)
    monkeypatch.setattr(config.settings, "sentinel_grid_stagger_seconds", 0.05)
    cams = [_grid_camera(db_session) for _ in range(3)]
    monkeypatch.setattr(supervisor, "_eligible_camera_ids", lambda db: [c.id for c in cams])

    start_times: list[float] = []

    def fake_start_worker(camera_id: str) -> None:
        start_times.append(time.monotonic())
        worker.RUNNING[camera_id] = _FakeTask(done=False)

    monkeypatch.setattr(supervisor.worker, "start_worker", fake_start_worker)

    t0 = time.monotonic()
    started = asyncio.run(supervisor._connect_eligible(db_session))
    elapsed = time.monotonic() - t0

    assert started == 3
    assert len(start_times) == 3
    # 2 gaps between 3 starts, each >= the configured stagger (a little
    # slack for real asyncio scheduling jitter, never for correctness).
    gap1 = start_times[1] - start_times[0]
    gap2 = start_times[2] - start_times[1]
    assert gap1 >= 0.04, f"gap1={gap1}"
    assert gap2 >= 0.04, f"gap2={gap2}"
    assert elapsed >= 0.09, f"elapsed={elapsed}"  # ~2 * stagger, not a burst


def test_connect_eligible_no_stagger_after_the_last_camera_started(monkeypatch, db_session):
    """The delay is between starts, not after the last one; a single start
    doesn't wait for nothing."""
    monkeypatch.setattr(config.settings, "sentinel_grid_max_autoconnect", 1)
    monkeypatch.setattr(config.settings, "sentinel_grid_stagger_seconds", 5.0)  # would time out this test if hit
    cam = _grid_camera(db_session)
    monkeypatch.setattr(supervisor, "_eligible_camera_ids", lambda db: [cam.id])
    monkeypatch.setattr(supervisor.worker, "start_worker", lambda cid: worker.RUNNING.__setitem__(cid, _FakeTask(done=False)))

    t0 = time.monotonic()
    started = asyncio.run(supervisor._connect_eligible(db_session))
    elapsed = time.monotonic() - t0

    assert started == 1
    assert elapsed < 1.0  # nowhere near the 5s stagger, never awaited


def test_stop_supervisor_cleans_up_while_a_sweep_is_mid_stagger(monkeypatch):
    """Cancelling the supervisor mid-stagger doesn't hang, crash, or leave a
    worker running that stop_supervisor doesn't know about."""
    monkeypatch.setattr(config.settings, "sentinel_grid_stagger_seconds", 1.0)
    monkeypatch.setattr(config.settings, "sentinel_grid_max_autoconnect", 3)
    cam_ids = ["cam_stagger_a", "cam_stagger_b", "cam_stagger_c"]
    monkeypatch.setattr(supervisor, "_eligible_camera_ids", lambda db: cam_ids)

    started, stopped = [], []
    monkeypatch.setattr(supervisor.worker, "start_worker", lambda cid: (started.append(cid), worker.RUNNING.__setitem__(cid, _FakeTask(done=False))))

    def _fake_stop_worker(cid):
        # real stop_worker returns the cancelled task (or None) for callers to
        # await, and stop_supervisor gathers them, so mirror that
        stopped.append(cid)
        return worker.RUNNING.pop(cid, None)

    monkeypatch.setattr(supervisor.worker, "stop_worker", _fake_stop_worker)

    async def _run():
        supervisor.start_supervisor()
        # let the first sweep start A, then land inside the 1s sleep before B
        await asyncio.sleep(0.2)
        assert started == ["cam_stagger_a"]
        await supervisor.stop_supervisor()

    try:
        asyncio.run(_run())
        assert supervisor._supervisor_task is None
        assert supervisor.AUTO_MANAGED == set()
        # B and C never got start_worker (cancelled mid-stagger), so nothing
        # orphaned. stop_supervisor still calls stop_worker for every
        # AUTO_MANAGED id, which is a no-op for cameras never started
        # (test_stop_supervisor_cancels_sweep_and_stops_every_managed_worker)
        assert started == ["cam_stagger_a"]
        assert "cam_stagger_a" in stopped
    finally:
        for cid in cam_ids:
            worker.RUNNING.pop(cid, None)
        supervisor._supervisor_task = None


def test_connect_eligible_never_starts_a_second_worker_for_an_already_running_camera(monkeypatch, db_session):
    """Duplicate-worker prevention at the supervisor level."""
    cam = _grid_camera(db_session)
    worker.RUNNING[cam.id] = _FakeTask(done=False)  # already running
    monkeypatch.setattr(supervisor, "_eligible_camera_ids", lambda db: [cam.id])

    called = []
    monkeypatch.setattr(supervisor.worker, "start_worker", lambda cid: called.append(cid))

    started = asyncio.run(supervisor._connect_eligible(db_session))
    assert started == 0
    assert called == []


def test_connect_eligible_restarts_a_dead_worker_after_the_backoff_floor(monkeypatch, db_session):
    """A camera whose worker ended (out of retries) gets picked up again on
    a later sweep once the restart floor has passed."""
    cam = _grid_camera(db_session)
    worker.RUNNING[cam.id] = _FakeTask(done=True)  # task ended
    supervisor._last_restart_attempt[cam.id] = time.monotonic() - 999  # long ago
    monkeypatch.setattr(supervisor, "_eligible_camera_ids", lambda db: [cam.id])

    called = []
    monkeypatch.setattr(supervisor.worker, "start_worker", lambda cid: called.append(cid))

    started = asyncio.run(supervisor._connect_eligible(db_session))
    assert started == 1
    assert called == [cam.id]


def test_connect_eligible_does_not_hammer_a_just_dropped_worker(monkeypatch, db_session):
    """A worker that just ended isn't retried on the very next sweep."""
    cam = _grid_camera(db_session)
    worker.RUNNING[cam.id] = _FakeTask(done=True)
    supervisor._last_restart_attempt[cam.id] = time.monotonic()  # just attempted
    monkeypatch.setattr(supervisor, "_eligible_camera_ids", lambda db: [cam.id])

    called = []
    monkeypatch.setattr(supervisor.worker, "start_worker", lambda cid: called.append(cid))

    started = asyncio.run(supervisor._connect_eligible(db_session))
    assert started == 0
    assert called == []


def test_connect_eligible_respects_auth_error_cooldown(monkeypatch, db_session):
    """AUTH_ERROR isn't retried every sweep; it's one shared login, so
    retrying per camera per sweep just hammers it."""
    cam = _grid_camera(db_session)
    worker.CAMERA_STATS[cam.id] = {"grid_state": "AUTH_ERROR"}
    supervisor._last_restart_attempt[cam.id] = time.monotonic() - 60  # 60s ago, past the normal floor...
    monkeypatch.setattr(supervisor, "_eligible_camera_ids", lambda db: [cam.id])

    called = []
    monkeypatch.setattr(supervisor.worker, "start_worker", lambda cid: called.append(cid))

    started = asyncio.run(supervisor._connect_eligible(db_session))
    # ...but well inside the much longer AUTH_ERROR cooldown (300s default).
    assert started == 0
    assert called == []
    worker.CAMERA_STATS.pop(cam.id, None)


def test_connect_eligible_noop_when_credentials_not_configured(monkeypatch, db_session):
    monkeypatch.setattr(config.settings, "sentinel_grid_email", "")
    monkeypatch.setattr(config.settings, "sentinel_grid_password", "")
    _grid_camera(db_session)

    called = []
    monkeypatch.setattr(supervisor.worker, "start_worker", lambda cid: called.append(cid))

    started = asyncio.run(supervisor._connect_eligible(db_session))
    assert started == 0
    assert called == []


def test_connect_eligible_noop_when_autoconnect_disabled(monkeypatch, db_session):
    monkeypatch.setattr(config.settings, "sentinel_grid_autoconnect", False)
    _grid_camera(db_session)

    called = []
    monkeypatch.setattr(supervisor.worker, "start_worker", lambda cid: called.append(cid))

    started = asyncio.run(supervisor._connect_eligible(db_session))
    assert started == 0
    assert called == []


def test_connect_and_disconnect_manage_auto_managed_set(monkeypatch):
    started, stopped = [], []
    monkeypatch.setattr(supervisor.worker, "start_worker", lambda cid: started.append(cid))
    monkeypatch.setattr(supervisor.worker, "stop_worker", lambda cid: stopped.append(cid))

    supervisor.connect("cam_x")
    assert "cam_x" in supervisor.AUTO_MANAGED
    assert started == ["cam_x"]

    supervisor.disconnect("cam_x")
    assert "cam_x" not in supervisor.AUTO_MANAGED
    assert stopped == ["cam_x"]


def test_sweep_does_not_undo_a_manual_disconnect(monkeypatch, db_session):
    """The sweep re-added every eligible camera to AUTO_MANAGED, undoing an
    operator's Disconnect ~30s later. A disconnected camera stays out until
    the operator connects it again."""
    cam = _grid_camera(db_session)
    # Scoped to this camera. Unscoped, it only passed by luck: SUP- sorts
    # after the GRID-camNN rows other files leave behind, so the capped
    # reconnect loop never reached it and the bug was hidden.
    monkeypatch.setattr(supervisor, "_eligible_camera_ids", lambda db: [cam.id])

    started: list[str] = []
    stopped: list[str] = []
    monkeypatch.setattr(supervisor.worker, "start_worker", lambda cid: (started.append(cid), worker.RUNNING.__setitem__(cid, _FakeTask(done=False))))
    monkeypatch.setattr(supervisor.worker, "stop_worker", lambda cid: (stopped.append(cid), worker.RUNNING.pop(cid, None)))

    # Sweep #1: picks the camera up like any newly-eligible camera.
    asyncio.run(supervisor._connect_eligible(db_session))
    assert cam.id in supervisor.AUTO_MANAGED
    assert started == [cam.id]

    # Operator explicitly disconnects it.
    supervisor.disconnect(cam.id)
    assert cam.id not in supervisor.AUTO_MANAGED
    assert cam.id not in worker.RUNNING

    # sweep 2 must not bring it back; the first fix only filtered the
    # bookkeeping and the connect loop still used the unfiltered list
    asyncio.run(supervisor._connect_eligible(db_session))
    assert cam.id not in supervisor.AUTO_MANAGED
    assert cam.id not in worker.RUNNING
    assert started == [cam.id]  # no second start_worker call for cam.id

    # An explicit Connect afterwards still works normally.
    supervisor.connect(cam.id)
    assert cam.id in supervisor.AUTO_MANAGED
    assert cam.id in worker.RUNNING


def test_stop_supervisor_cancels_sweep_and_stops_every_managed_worker(monkeypatch):
    """Shutdown leaves no sweep task and no camera worker behind."""
    supervisor.AUTO_MANAGED.update({"cam_a", "cam_b"})
    stopped = []
    monkeypatch.setattr(supervisor.worker, "stop_worker", lambda cid: stopped.append(cid))

    async def _run():
        async def _never_ending():
            await asyncio.sleep(3600)
        supervisor._supervisor_task = asyncio.create_task(_never_ending())
        await supervisor.stop_supervisor()

    asyncio.run(_run())
    assert set(stopped) == {"cam_a", "cam_b"}
    assert supervisor.AUTO_MANAGED == set()
    assert supervisor._supervisor_task is None


def test_discover_and_register_skips_network_call_when_not_configured(monkeypatch):
    """No credentials, no attempt to reach the grid at all."""
    monkeypatch.setattr(config.settings, "sentinel_grid_email", "")
    monkeypatch.setattr(config.settings, "sentinel_grid_password", "")

    called = []
    monkeypatch.setattr(supervisor, "fetch_grid_cameras", lambda: called.append(True))

    asyncio.run(supervisor.discover_and_register())
    assert called == []


# restart(): a grid camera restarted with raw stop/start dropped out of the
# supervisor (not in AUTO_MANAGED, or stuck in OPERATOR_DISCONNECTED). These
# check the state it ends up in, not just that it ran.

def test_restart_on_sentinel_grid_camera_rejoins_auto_managed_and_clears_operator_disconnected(monkeypatch, db_session):
    cam = _grid_camera(db_session)
    started, stopped = [], []
    monkeypatch.setattr(supervisor.worker, "start_worker", lambda cid: (started.append(cid), worker.RUNNING.__setitem__(cid, _FakeTask(done=False))))
    monkeypatch.setattr(supervisor.worker, "stop_worker", lambda cid: (stopped.append(cid), worker.RUNNING.pop(cid, None))[1])

    # Simulates the realistic precondition: the operator had explicitly
    # disconnected this camera at some point before the restart.
    supervisor.OPERATOR_DISCONNECTED.add(cam.id)
    supervisor.AUTO_MANAGED.discard(cam.id)

    asyncio.run(supervisor.restart(cam.id, cam.source_type))

    # restart leaves the camera exactly as a fresh Connect would, managed by
    # the sweep from now on
    assert cam.id not in supervisor.OPERATOR_DISCONNECTED
    assert cam.id in supervisor.AUTO_MANAGED
    assert stopped == [cam.id]  # old worker actually stopped first...
    assert started == [cam.id]  # ...before the new one started (not skipped by the dedup guard)


def test_restart_on_sentinel_grid_camera_that_was_never_disconnected_still_ends_auto_managed(monkeypatch, db_session):
    """The common case: restart a running grid camera; it ends up
    AUTO_MANAGED like any Connect."""
    cam = _grid_camera(db_session)
    monkeypatch.setattr(supervisor.worker, "start_worker", lambda cid: worker.RUNNING.__setitem__(cid, _FakeTask(done=False)))
    monkeypatch.setattr(supervisor.worker, "stop_worker", lambda cid: worker.RUNNING.pop(cid, None))
    supervisor.AUTO_MANAGED.add(cam.id)  # already running/managed, the common case

    asyncio.run(supervisor.restart(cam.id, cam.source_type))

    assert cam.id in supervisor.AUTO_MANAGED
    assert cam.id not in supervisor.OPERATOR_DISCONNECTED


def test_restart_on_a_non_grid_camera_never_touches_supervisor_bookkeeping(monkeypatch, db_session):
    """A webcam/video_file restart is unchanged; the supervisor only manages
    grid cameras."""
    started, stopped = [], []
    monkeypatch.setattr(supervisor.worker, "start_worker", lambda cid: started.append(cid))
    monkeypatch.setattr(supervisor.worker, "stop_worker", lambda cid: stopped.append(cid))

    asyncio.run(supervisor.restart("cam_plain_video_file", "video_file"))

    assert started == ["cam_plain_video_file"]
    assert stopped == ["cam_plain_video_file"]
    assert "cam_plain_video_file" not in supervisor.AUTO_MANAGED
    assert "cam_plain_video_file" not in supervisor.OPERATOR_DISCONNECTED


def test_restart_actually_awaits_the_old_tasks_cancellation_before_starting_a_new_one(monkeypatch):
    """restart awaits the old task before starting the new one, instead of
    relying on scheduling order. The old task's __await__ completes before
    start_worker is called."""
    events: list[str] = []

    class _RecordingFakeTask(_FakeTask):
        def __await__(self):
            events.append("old_task_awaited")
            return super().__await__()

    monkeypatch.setattr(supervisor.worker, "stop_worker", lambda cid: _RecordingFakeTask(done=False))
    monkeypatch.setattr(supervisor.worker, "start_worker", lambda cid: events.append("start_worker_called"))

    asyncio.run(supervisor.restart("cam_order_test", "video_file"))

    assert events == ["old_task_awaited", "start_worker_called"], (
        "the old worker's cancellation must be awaited BEFORE the new worker starts, "
        f"got order: {events}"
    )


class TestGridWideCircuitBreaker:
    """cv2's FFmpeg backend hides the RTSP response, so a real 401 from the
    grid (credentials set but rejected, e.g. account blocked) looks like a
    network blip. It never becomes AUTH_ERROR (only blank credentials do), so
    every camera retries on the normal ~20s floor forever. With everything
    always connected that was up to 30 cameras hammering a rejected login
    every sweep, which is how accounts get blocked.

    The breaker spots the shape instead: lots of cameras tried, none connected.
    """

    def _mark_attempted(self, camera_id: str, grid_state: str | None) -> None:
        worker.CAMERA_STATS[camera_id] = {"started_at": "2026-01-01T00:00:00", "grid_state": grid_state}
        supervisor.AUTO_MANAGED.add(camera_id)

    def test_below_the_threshold_never_trips(self):
        """A few unlucky cameras isn't a rejected shared login."""
        for i in range(supervisor._GRID_WIDE_FAILURE_THRESHOLD - 1):
            self._mark_attempted(f"cam{i}", "DISCONNECTED")
        assert supervisor._grid_wide_rejection_detected() is None

    def test_at_the_threshold_with_zero_connected_trips(self):
        for i in range(supervisor._GRID_WIDE_FAILURE_THRESHOLD):
            self._mark_attempted(f"cam{i}", "DISCONNECTED")
        assert supervisor._grid_wide_rejection_detected() == supervisor._GRID_WIDE_FAILURE_THRESHOLD

    def test_even_one_real_connection_prevents_the_trip(self):
        """One connected camera among many failing means the grid is fine."""
        for i in range(supervisor._GRID_WIDE_FAILURE_THRESHOLD):
            self._mark_attempted(f"cam{i}", "DISCONNECTED")
        self._mark_attempted("cam_ok", "CONNECTED")
        assert supervisor._grid_wide_rejection_detected() is None

    def test_processing_also_counts_as_genuinely_connected(self):
        for i in range(supervisor._GRID_WIDE_FAILURE_THRESHOLD):
            self._mark_attempted(f"cam{i}", "DISCONNECTED")
        self._mark_attempted("cam_ok", "PROCESSING")
        assert supervisor._grid_wide_rejection_detected() is None

    def test_a_camera_never_yet_attempted_does_not_count_toward_the_total(self):
        """started_at is None until a worker has run once, so the first sweep
        after a restart can't trip it before trying anything."""
        for i in range(supervisor._GRID_WIDE_FAILURE_THRESHOLD):
            worker.CAMERA_STATS[f"cam{i}"] = {"started_at": None, "grid_state": None}
            supervisor.AUTO_MANAGED.add(f"cam{i}")
        assert supervisor._grid_wide_rejection_detected() is None

    def test_a_tripped_breaker_stops_new_connect_attempts_entirely(self, monkeypatch, db_session):
        for i in range(supervisor._GRID_WIDE_FAILURE_THRESHOLD):
            self._mark_attempted(f"cam{i}", "ERROR")
        cams = [_grid_camera(db_session) for _ in range(3)]
        monkeypatch.setattr(supervisor, "_eligible_camera_ids", lambda db: [c.id for c in cams])
        started = []
        monkeypatch.setattr(supervisor.worker, "start_worker", lambda cid: started.append(cid))

        result = asyncio.run(supervisor._connect_eligible(db_session))

        assert result == 0
        assert started == [], "the breaker tripped; no new camera should have been started this sweep"

    def test_the_cooldown_holds_for_subsequent_sweeps_too(self, monkeypatch, db_session):
        """It pauses every grid connect for the cooldown, not just one sweep."""
        for i in range(supervisor._GRID_WIDE_FAILURE_THRESHOLD):
            self._mark_attempted(f"cam{i}", "ERROR")
        cams = [_grid_camera(db_session) for _ in range(3)]
        monkeypatch.setattr(supervisor, "_eligible_camera_ids", lambda db: [c.id for c in cams])
        started = []
        monkeypatch.setattr(supervisor.worker, "start_worker", lambda cid: started.append(cid))

        asyncio.run(supervisor._connect_eligible(db_session))  # trips it
        # clear the failing stats as if cleanup ran; the cooldown timestamp
        # alone has to hold the next sweep back
        worker.CAMERA_STATS.clear()
        result = asyncio.run(supervisor._connect_eligible(db_session))

        assert result == 0
        assert started == []

    def _tripped(self, monkeypatch, db_session, n_cams=3):
        for i in range(supervisor._GRID_WIDE_FAILURE_THRESHOLD):
            self._mark_attempted(f"cam{i}", "ERROR")
        cams = [_grid_camera(db_session) for _ in range(n_cams)]
        monkeypatch.setattr(supervisor, "_eligible_camera_ids", lambda db: [c.id for c in cams])
        started = []
        monkeypatch.setattr(supervisor.worker, "start_worker", lambda cid: started.append(cid))
        asyncio.run(supervisor._connect_eligible(db_session))  # trips it
        worker.CAMERA_STATS.clear()
        return cams, started

    def test_after_the_pause_one_camera_probes_before_the_rest(self, monkeypatch, db_session):
        cams, started = self._tripped(monkeypatch, db_session)
        supervisor._grid_wide_cooldown_until = time.monotonic() - 1.0
        assert asyncio.run(supervisor._connect_eligible(db_session)) == 1
        assert started == [cams[0].id]
        # probe still connecting: nobody else starts
        monkeypatch.setattr(supervisor, "_is_running", lambda cid: cid == cams[0].id)
        assert asyncio.run(supervisor._connect_eligible(db_session)) == 0
        assert started == [cams[0].id]

    def test_a_probe_that_connects_brings_everyone_back(self, monkeypatch, db_session):
        cams, started = self._tripped(monkeypatch, db_session)
        supervisor._grid_wide_cooldown_until = time.monotonic() - 1.0
        asyncio.run(supervisor._connect_eligible(db_session))
        worker.CAMERA_STATS[cams[0].id] = {"started_at": "x", "grid_state": "CONNECTED"}
        monkeypatch.setattr(supervisor, "_is_running", lambda cid: cid == cams[0].id)
        assert asyncio.run(supervisor._connect_eligible(db_session)) == 2
        assert set(started) == {c.id for c in cams}
        assert supervisor._grid_trips == 0

    def test_a_failed_probe_pauses_again_for_twice_as_long(self, monkeypatch, db_session):
        cams, started = self._tripped(monkeypatch, db_session)
        first_pause = supervisor._grid_wide_cooldown_until - time.monotonic()
        supervisor._grid_wide_cooldown_until = time.monotonic() - 1.0
        asyncio.run(supervisor._connect_eligible(db_session))   # probe starts
        monkeypatch.setattr(supervisor, "_is_running", lambda cid: False)  # probe gave up
        assert asyncio.run(supervisor._connect_eligible(db_session)) == 0
        second_pause = supervisor._grid_wide_cooldown_until - time.monotonic()
        assert supervisor._grid_trips == 2
        assert second_pause == pytest.approx(2 * first_pause, abs=5)
        assert started == [cams[0].id]

    def test_the_pause_never_exceeds_an_hour(self, monkeypatch):
        supervisor._grid_trips = 20
        supervisor._trip(time.monotonic(), "test")
        assert supervisor._grid_wide_cooldown_until - time.monotonic() <= supervisor._MAX_GRID_COOLDOWN_S + 1

    def test_a_genuinely_healthy_fleet_is_never_paused(self, monkeypatch, db_session):
        """Lots attempted, most connected: normal operation never trips it."""
        for i in range(10):
            self._mark_attempted(f"cam{i}", "PROCESSING" if i < 8 else "DEGRADED")
        cams = [_grid_camera(db_session) for _ in range(2)]
        monkeypatch.setattr(supervisor, "_eligible_camera_ids", lambda db: [c.id for c in cams])
        started = []
        monkeypatch.setattr(supervisor.worker, "start_worker", lambda cid: started.append(cid))

        result = asyncio.run(supervisor._connect_eligible(db_session))

        assert result == 2
        assert set(started) == {c.id for c in cams}
