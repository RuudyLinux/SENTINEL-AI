"""24/7 real Sentinel Camera Grid auto-connect supervisor (real camera
connectivity task). Network-free: `worker.start_worker`/`stop_worker` are
monkeypatched to fakes throughout — these tests are about the supervisor's
OWN scheduling logic (eligibility, cap, cooldown, dedup, shutdown), not the
real RTSP pipeline (that's worker.py's own test files)."""
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


@pytest.fixture(autouse=True)
def _clean_supervisor_state(monkeypatch):
    """Every test starts from a clean slate — these are module-level dicts
    shared across the whole test session otherwise."""
    supervisor.AUTO_MANAGED.clear()
    supervisor.OPERATOR_DISCONNECTED.clear()
    supervisor._last_restart_attempt.clear()
    monkeypatch.setattr(config.settings, "sentinel_grid_email", "someone@example.com")
    monkeypatch.setattr(config.settings, "sentinel_grid_password", "correct-password")
    monkeypatch.setattr(config.settings, "sentinel_grid_autoconnect", True)
    monkeypatch.setattr(config.settings, "sentinel_grid_max_autoconnect", 5)
    monkeypatch.setattr(config.settings, "sentinel_grid_auth_cooldown_seconds", 300.0)
    # 0 by default so the scheduling-logic tests below (cap, dedup, cooldown,
    # etc. — not about stagger timing itself) stay instant; the dedicated
    # staggered-startup tests override this to a small real delay.
    monkeypatch.setattr(config.settings, "sentinel_grid_stagger_seconds", 0.0)
    yield
    supervisor.AUTO_MANAGED.clear()
    supervisor.OPERATOR_DISCONNECTED.clear()
    supervisor._last_restart_attempt.clear()


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
    """Real/simulated separation (Part 13 of the task): only source_type ==
    'sentinel_grid' is ever eligible for auto-connect — a mock_vms or webcam
    camera (or, in the future, any logical-scale simulated source) is not."""
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
    """Resource safety: the supervisor must never auto-connect more than
    settings.sentinel_grid_max_autoconnect at once, even with more eligible
    cameras available."""
    monkeypatch.setattr(config.settings, "sentinel_grid_max_autoconnect", 2)
    cams = [_grid_camera(db_session) for _ in range(4)]

    started_ids: list[str] = []

    def fake_start_worker(camera_id: str) -> None:
        started_ids.append(camera_id)
        worker.RUNNING[camera_id] = _FakeTask(done=False)

    monkeypatch.setattr(supervisor.worker, "start_worker", fake_start_worker)
    # Isolates this test to exactly its own 4 cameras — _eligible_camera_ids
    # itself (unscoped by design, real grid cameras across the whole DB) is
    # covered separately above; the shared test DB also carries sentinel_grid
    # rows from other test files, which would otherwise leak into this
    # scheduling-logic test.
    monkeypatch.setattr(supervisor, "_eligible_camera_ids", lambda db: [c.id for c in cams])

    started = asyncio.run(supervisor._connect_eligible(db_session))
    assert started == 2
    assert len(started_ids) == 2
    assert all(cid in {c.id for c in cams} for cid in started_ids)


def test_connect_eligible_staggers_successive_starts(monkeypatch, db_session):
    """Concurrency optimization task: real finding was that N simultaneous
    RTSP connection bursts are meaningfully less reliable than the same N
    cameras brought up one at a time — this is the actual fix. A real
    (small) delay must elapse between successive worker starts within one
    sweep, not just between sweeps."""
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
    """The stagger delay is BETWEEN starts, not a trailing pause after the
    sweep's last camera — a single-camera (or last-slot) start must not
    incur a pointless wait with nothing left to space out."""
    monkeypatch.setattr(config.settings, "sentinel_grid_max_autoconnect", 1)
    monkeypatch.setattr(config.settings, "sentinel_grid_stagger_seconds", 5.0)  # would time out this test if hit
    cam = _grid_camera(db_session)
    monkeypatch.setattr(supervisor, "_eligible_camera_ids", lambda db: [cam.id])
    monkeypatch.setattr(supervisor.worker, "start_worker", lambda cid: worker.RUNNING.__setitem__(cid, _FakeTask(done=False)))

    t0 = time.monotonic()
    started = asyncio.run(supervisor._connect_eligible(db_session))
    elapsed = time.monotonic() - t0

    assert started == 1
    assert elapsed < 1.0  # nowhere near the 5s stagger — it was never awaited


def test_stop_supervisor_cleans_up_while_a_sweep_is_mid_stagger(monkeypatch):
    """Shutdown during staggered startup: cancelling the supervisor while
    _connect_eligible is asleep between two staggered starts must not hang,
    crash, or leave a worker running that stop_supervisor doesn't know about."""
    monkeypatch.setattr(config.settings, "sentinel_grid_stagger_seconds", 1.0)
    monkeypatch.setattr(config.settings, "sentinel_grid_max_autoconnect", 3)
    cam_ids = ["cam_stagger_a", "cam_stagger_b", "cam_stagger_c"]
    monkeypatch.setattr(supervisor, "_eligible_camera_ids", lambda db: cam_ids)

    started, stopped = [], []
    monkeypatch.setattr(supervisor.worker, "start_worker", lambda cid: (started.append(cid), worker.RUNNING.__setitem__(cid, _FakeTask(done=False))))
    monkeypatch.setattr(supervisor.worker, "stop_worker", lambda cid: (stopped.append(cid), worker.RUNNING.pop(cid, None)))

    async def _run():
        supervisor.start_supervisor()
        # Let the first sweep start camera A, then land inside the 1s
        # stagger sleep before camera B — a real mid-stagger moment.
        await asyncio.sleep(0.2)
        assert started == ["cam_stagger_a"]
        await supervisor.stop_supervisor()

    try:
        asyncio.run(_run())
        assert supervisor._supervisor_task is None
        assert supervisor.AUTO_MANAGED == set()
        # The real invariant: B and C never actually got a start_worker call
        # (still mid-stagger, cancelled before their turn) — no orphaned
        # worker was created for them. stop_supervisor's own cleanup loop
        # still calls stop_worker for every AUTO_MANAGED id unconditionally
        # (existing, already-tested behavior — see
        # test_stop_supervisor_cancels_sweep_and_stops_every_managed_worker),
        # which is harmless/idempotent for a camera never started (real
        # worker.stop_worker no-ops on a RUNNING dict miss).
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
    """24/7 reconnect: a camera whose worker task has ended (gave up after
    its own internal retry budget) gets picked back up on a later sweep,
    once the supervisor-level restart floor has elapsed."""
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
    """The supervisor's own restart floor — a worker that JUST ended is not
    immediately retried on the very next sweep."""
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
    """A rejected credential (AUTH_ERROR) must not be retried on every
    ordinary sweep interval — it fails identically for every camera (one
    shared grid login), so hammering it per-camera per-sweep is exactly the
    'endlessly retry invalid credentials' the task explicitly forbids."""
    cam = _grid_camera(db_session)
    worker.CAMERA_STATS[cam.id] = {"grid_state": "AUTH_ERROR"}
    supervisor._last_restart_attempt[cam.id] = time.monotonic() - 60  # 60s ago — past the ordinary floor...
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
    """Real bug found via the live browser test of the Disconnect button:
    _connect_eligible used to unconditionally re-add every eligible camera to
    AUTO_MANAGED on every sweep, silently undoing an operator's Disconnect on
    the very next sweep (~30s later) — contradicting disconnect()'s own
    documented intent. A camera the operator disconnected must stay out of
    AUTO_MANAGED (and therefore not get auto-reconnected) until the operator
    explicitly connects it again."""
    cam = _grid_camera(db_session)
    # Isolates this test to exactly its own camera — an earlier version of
    # this test left _eligible_camera_ids unscoped (real grid cameras across
    # the whole shared test DB, covered separately by the
    # test_eligible_camera_ids_* tests above) and the assertions still
    # happened to pass by alphabetical-ordering luck (`cam`'s SUP- code sorts
    # after the dozens of GRID-camNN rows other test files leave in the
    # shared DB, so the cap-limited reconnect loop never reached it) — which
    # masked the very bug this test exists to catch. Scoped explicitly here.
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

    # Sweep #2 (the next periodic sweep) must NOT bring it back — this is
    # the exact assertion the incomplete first fix still failed: it filtered
    # AUTO_MANAGED's bookkeeping but the (re)connect loop below it still
    # iterated the raw, unfiltered eligible list.
    asyncio.run(supervisor._connect_eligible(db_session))
    assert cam.id not in supervisor.AUTO_MANAGED
    assert cam.id not in worker.RUNNING
    assert started == [cam.id]  # no second start_worker call for cam.id

    # An explicit Connect afterwards still works normally.
    supervisor.connect(cam.id)
    assert cam.id in supervisor.AUTO_MANAGED
    assert cam.id in worker.RUNNING


def test_stop_supervisor_cancels_sweep_and_stops_every_managed_worker(monkeypatch):
    """Shutdown cleanup — no orphaned sweep task, no orphaned camera worker."""
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
    """No credentials configured -> no attempt to reach the real grid at
    all, not even a fetch that's expected to fail cleanly."""
    monkeypatch.setattr(config.settings, "sentinel_grid_email", "")
    monkeypatch.setattr(config.settings, "sentinel_grid_password", "")

    called = []
    monkeypatch.setattr(supervisor, "fetch_grid_cameras", lambda: called.append(True))

    asyncio.run(supervisor.discover_and_register())
    assert called == []
