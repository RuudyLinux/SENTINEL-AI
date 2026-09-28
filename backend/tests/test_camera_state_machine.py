"""Camera lifecycle transitions are a real table now.

The old guard only checked the new state was a known name, so DISCONNECTED ->
PROCESSING passed, and it was an assert, gone under -O.

Illegal transitions are still applied (refusing would leave CAMERA_STATS
wrong, raising would kill a worker) and counted in ILLEGAL_TRANSITIONS, so a
test can fail on them.
"""
import pytest

from app.pipeline import worker


@pytest.fixture(autouse=True)
def _isolate():
    worker.CAMERA_STATS.clear()
    worker.ILLEGAL_TRANSITIONS.clear()
    yield
    worker.CAMERA_STATS.clear()
    worker.ILLEGAL_TRANSITIONS.clear()


class TestFirstTransition:
    def test_a_camera_s_first_ever_transition_is_never_illegal(self):
        """CAMERA_STATS is created by setdefault on first lookup, not when a
        worker starts, so grid_state starts at None and can't be mistaken for
        a real prior state."""
        worker._set_grid_state("cam1", "DISCOVERING")
        assert worker.ILLEGAL_TRANSITIONS == {}
        worker.CAMERA_STATS.clear()
        worker._set_grid_state("cam2", "PROCESSING")
        assert worker.ILLEGAL_TRANSITIONS == {}, (
            "a state with no prior history must never be flagged, whatever "
            "the destination is"
        )


class TestARealBootLifecycle:
    def test_the_documented_happy_path_is_entirely_legal(self):
        sequence = [
            "DISCOVERING", "CONNECTING", "CONNECTED", "PROCESSING",
            "DEGRADED", "RECONNECTING", "CONNECTED", "PROCESSING",
        ]
        for state in sequence:
            worker._set_grid_state("cam1", state)
        assert worker.ILLEGAL_TRANSITIONS == {}

    def test_stopping_from_any_running_state_is_legal(self):
        # DEGRADED/RECONNECTING via CONNECTED: they're read-failure states,
        # only reached once a stream was live
        for start in ("CONNECTED", "PROCESSING", "DEGRADED", "RECONNECTING"):
            worker.CAMERA_STATS.clear()
            worker._set_grid_state("cam1", "CONNECTING")
            worker._set_grid_state("cam1", "CONNECTED")
            worker._set_grid_state("cam1", start)
            worker._set_grid_state("cam1", "DISCONNECTED")
        assert worker.ILLEGAL_TRANSITIONS == {}

    def test_a_failure_from_any_running_state_is_legal(self):
        for start in ("CONNECTED", "PROCESSING", "DEGRADED"):
            for failure in ("AUTH_ERROR", "ERROR"):
                worker.CAMERA_STATS.clear()
                worker._set_grid_state("cam1", "CONNECTING")
                worker._set_grid_state("cam1", "CONNECTED")
                worker._set_grid_state("cam1", start)
                worker._set_grid_state("cam1", failure)
        assert worker.ILLEGAL_TRANSITIONS == {}

    def test_a_stopped_camera_restarts_through_connecting_not_directly_live(self):
        worker._set_grid_state("cam1", "CONNECTING")
        worker._set_grid_state("cam1", "DISCONNECTED")
        worker._set_grid_state("cam1", "CONNECTING")
        worker._set_grid_state("cam1", "CONNECTED")
        assert worker.ILLEGAL_TRANSITIONS == {}

    def test_a_fast_failing_initial_connect_falls_straight_into_a_retry(self):
        """Seen live: start_worker does _open_with_timeout and then
        _reopen_with_backoff in the same call. The first marks DISCONNECTED
        on a synchronous failure, the second immediately marks RECONNECTING.
        Two real cameras did exactly this before the table allowed it."""
        worker._set_grid_state("cam1", "CONNECTING")
        worker._set_grid_state("cam1", "DISCONNECTED")
        worker._set_grid_state("cam1", "RECONNECTING")
        assert worker.ILLEGAL_TRANSITIONS == {}


class TestIllegalTransitionsAreCaughtNotSilent:
    def test_jumping_straight_to_processing_from_disconnected_is_flagged(self):
        worker._set_grid_state("cam1", "CONNECTING")
        worker._set_grid_state("cam1", "DISCONNECTED")
        worker._set_grid_state("cam1", "PROCESSING")
        assert worker.ILLEGAL_TRANSITIONS == {("DISCONNECTED", "PROCESSING"): 1}

    def test_the_illegal_transition_is_applied_anyway(self):
        """Refusing would leave state claiming the camera is doing what it
        did before, worse than logging a gap."""
        worker._set_grid_state("cam1", "CONNECTING")
        worker._set_grid_state("cam1", "DISCONNECTED")
        worker._set_grid_state("cam1", "PROCESSING")
        assert worker.CAMERA_STATS["cam1"]["grid_state"] == "PROCESSING"

    def test_repeated_illegal_transitions_accumulate_a_count(self):
        worker._set_grid_state("cam1", "CONNECTING")
        worker._set_grid_state("cam1", "DISCONNECTED")
        for _ in range(3):
            worker._set_grid_state("cam1", "PROCESSING")
            worker._set_grid_state("cam1", "DISCONNECTED")
        assert worker.ILLEGAL_TRANSITIONS[("DISCONNECTED", "PROCESSING")] == 3

    def test_an_unknown_state_name_is_still_rejected_outright(self):
        """Different from an illegal move: not a state at all, applying it
        would corrupt CAMERA_STATS."""
        with pytest.raises(ValueError):
            worker._set_grid_state("cam1", "TOTALLY_MADE_UP")


class TestTheTableItselfIsInternallyConsistent:
    def test_every_state_can_reach_disconnected(self):
        """An operator can stop any camera at any point in its lifecycle."""
        for state in worker.GRID_STATES:
            assert "DISCONNECTED" in worker._TRANSITIONS[state], state

    def test_every_running_state_can_fail(self):
        for state in ("CONNECTED", "PROCESSING", "DEGRADED", "RECONNECTING"):
            assert "AUTH_ERROR" in worker._TRANSITIONS[state], state
            assert "ERROR" in worker._TRANSITIONS[state], state

    def test_every_table_entry_only_names_real_states(self):
        for src, dests in worker._TRANSITIONS.items():
            assert src in worker.GRID_STATES, src
            assert dests <= worker.GRID_STATES, dests - worker.GRID_STATES

    def test_every_declared_state_has_a_row(self):
        assert set(worker._TRANSITIONS) == worker.GRID_STATES
