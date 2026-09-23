"""`Camera.status` (the legacy 3-value DB column) and `grid_state` (the real
9-state runtime lifecycle) used to be two independently-written literals at
8 call sites in worker.py — two of them ("degraded" next to `_set_grid_state
(..., "DEGRADED")`) already paired by hand, six of them not paired at all.
Nothing stopped an edit to one from silently leaving the other stale, which
is exactly the audit finding: 33 workers running while 31 cameras reported
`status: degraded` with `last_error: none`.

_DB_STATUS_FOR_GRID_STATE (worker.py) is now the one place that decision is
made; every one of those 8 sites derives `camera.status` from the SAME local
variable it passes to `_set_grid_state`. This file locks down the table
itself — that it only ever produces a value the DB column actually accepts,
and that its coverage matches which grid_state transitions historically
wrote to the DB and which deliberately did not (see the ERROR case below).
"""
from app.pipeline import worker

#: The only three values `Camera.status` has ever been documented to hold —
#: see the comment worker.py itself quotes at `_stats()`'s definition of
#: `grid_state`. A table entry producing anything else would be silently
#: writing a status no other code in this application expects.
_VALID_DB_STATUSES = {"online", "offline", "degraded"}


class TestTableProducesOnlyRealStatuses:
    def test_every_mapped_value_is_a_real_db_status(self):
        for grid_state, db_status in worker._DB_STATUS_FOR_GRID_STATE.items():
            assert db_status in _VALID_DB_STATUSES, (grid_state, db_status)

    def test_every_key_is_a_real_grid_state(self):
        assert set(worker._DB_STATUS_FOR_GRID_STATE) <= worker.GRID_STATES


class TestCoverageMatchesHistoricalBehaviour:
    """Which grid_state transitions wrote to `Camera.status` at all, verified
    by reading every one of the 8 sites before this table existed. A state
    silently gaining or losing an entry here is a real behaviour change to
    catch, not a detail to let drift."""

    def test_every_settled_running_state_has_a_mapping(self):
        for state in ("CONNECTED", "PROCESSING", "DEGRADED", "RECONNECTING",
                      "DISCONNECTED", "AUTH_ERROR"):
            assert state in worker._DB_STATUS_FOR_GRID_STATE, state

    def test_in_flight_and_transient_states_have_no_mapping(self):
        """CONNECTING/DISCOVERING: no site ever wrote camera.status while a
        connection attempt was merely in progress — only once it resolved.
        ERROR: the generic per-iteration exception handler, a genuine hot
        path — forcing a DB write on every transient blip would be a real,
        unmeasured cost this refactor must not introduce."""
        for state in ("CONNECTING", "DISCOVERING", "ERROR"):
            assert state not in worker._DB_STATUS_FOR_GRID_STATE, (
                f"{state} gained a DB-status mapping — if that is deliberate, "
                f"this test's list needs updating; if not, it is a real "
                f"behaviour change (a new hot-path DB write) that needs review"
            )

    def test_the_table_covers_every_state_that_is_neither_in_flight_nor_error(self):
        """Inverse of the two tests above, so a NINTH grid_state added later
        can't silently fall into neither bucket."""
        transient = {"CONNECTING", "DISCOVERING", "ERROR"}
        settled = worker.GRID_STATES - transient
        assert set(worker._DB_STATUS_FOR_GRID_STATE) == settled


class TestTheMappingMatchesWhatTheDocumentedLifecycleMeans:
    """The specific values, not just their presence — this is where a typo
    (mapping DEGRADED to "online", say) would actually be caught."""

    def test_a_live_stream_reads_online_whether_or_not_ai_is_running(self):
        assert worker._DB_STATUS_FOR_GRID_STATE["CONNECTED"] == "online"
        assert worker._DB_STATUS_FOR_GRID_STATE["PROCESSING"] == "online"

    def test_a_degraded_or_reconnecting_stream_reads_degraded_not_offline(self):
        """A camera mid-backoff is not yet given up on — offline would tell
        an operator to restart something that is already trying to recover
        on its own."""
        assert worker._DB_STATUS_FOR_GRID_STATE["DEGRADED"] == "degraded"
        assert worker._DB_STATUS_FOR_GRID_STATE["RECONNECTING"] == "degraded"

    def test_a_stopped_or_auth_failed_camera_reads_offline(self):
        assert worker._DB_STATUS_FOR_GRID_STATE["DISCONNECTED"] == "offline"
        assert worker._DB_STATUS_FOR_GRID_STATE["AUTH_ERROR"] == "offline"
