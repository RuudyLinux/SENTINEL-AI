"""Camera.status (3-value DB column) and grid_state (9-state lifecycle) used
to be written separately at 8 sites in worker.py, 2 paired by hand and 6 not
at all. That's how 33 workers were running while 31 cameras said "degraded"
with no last_error.

Now every site derives status from the same variable it passes to
_set_grid_state via _DB_STATUS_FOR_GRID_STATE. This pins the table: only
values the column accepts, and coverage matching which transitions
historically wrote status and which deliberately didn't (ERROR, below).
"""
from app.pipeline import worker

# the only three values Camera.status has ever held; anything else would be
# a status no other code expects
_VALID_DB_STATUSES = {"online", "offline", "degraded"}


class TestTableProducesOnlyRealStatuses:
    def test_every_mapped_value_is_a_real_db_status(self):
        for grid_state, db_status in worker._DB_STATUS_FOR_GRID_STATE.items():
            assert db_status in _VALID_DB_STATUSES, (grid_state, db_status)

    def test_every_key_is_a_real_grid_state(self):
        assert set(worker._DB_STATUS_FOR_GRID_STATE) <= worker.GRID_STATES


class TestCoverageMatchesHistoricalBehaviour:
    """Which transitions wrote Camera.status, from reading all 8 sites
    before the table existed. Gaining or losing an entry is a behaviour change."""

    def test_every_settled_running_state_has_a_mapping(self):
        for state in ("CONNECTED", "PROCESSING", "DEGRADED", "RECONNECTING",
                      "DISCONNECTED", "AUTH_ERROR"):
            assert state in worker._DB_STATUS_FOR_GRID_STATE, state

    def test_in_flight_and_transient_states_have_no_mapping(self):
        """CONNECTING/DISCOVERING never wrote status mid-attempt, only once it
        resolved. ERROR is the per-iteration catch-all, a hot path; a DB write
        per blip would be a new cost."""
        for state in ("CONNECTING", "DISCOVERING", "ERROR"):
            assert state not in worker._DB_STATUS_FOR_GRID_STATE, (
                f"{state} gained a DB-status mapping — if that is deliberate, "
                f"this test's list needs updating; if not, it is a real "
                f"behaviour change (a new hot-path DB write) that needs review"
            )

    def test_the_table_covers_every_state_that_is_neither_in_flight_nor_error(self):
        """So a ninth state added later can't land in neither bucket."""
        transient = {"CONNECTING", "DISCOVERING", "ERROR"}
        settled = worker.GRID_STATES - transient
        assert set(worker._DB_STATUS_FOR_GRID_STATE) == settled


class TestTheMappingMatchesWhatTheDocumentedLifecycleMeans:
    """The actual values, where a typo (DEGRADED -> "online") would show."""

    def test_a_live_stream_reads_online_whether_or_not_ai_is_running(self):
        assert worker._DB_STATUS_FOR_GRID_STATE["CONNECTED"] == "online"
        assert worker._DB_STATUS_FOR_GRID_STATE["PROCESSING"] == "online"

    def test_a_degraded_or_reconnecting_stream_reads_degraded_not_offline(self):
        """Mid-backoff isn't given up; offline would tell an operator to
        restart something already recovering."""
        assert worker._DB_STATUS_FOR_GRID_STATE["DEGRADED"] == "degraded"
        assert worker._DB_STATUS_FOR_GRID_STATE["RECONNECTING"] == "degraded"

    def test_a_stopped_or_auth_failed_camera_reads_offline(self):
        assert worker._DB_STATUS_FOR_GRID_STATE["DISCONNECTED"] == "offline"
        assert worker._DB_STATUS_FOR_GRID_STATE["AUTH_ERROR"] == "offline"
