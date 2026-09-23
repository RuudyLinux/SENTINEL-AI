"""V2 Phase 1 — temporal aggregation / confidence voting for plate reads.

The behavior these lock down is the fix for "one bad OCR frame overwrites a
reliable plate result": reads accumulate per (camera, ByteTrack track), vote,
and the winner is decided by summed confidence while the REPORTED confidence is
the peak actually observed.
"""
import pytest

from app.config import settings
from app.pipeline import plate_tracker


@pytest.fixture(autouse=True)
def _isolate():
    # Module state is process-global by design (same convention as
    # rules_engine's cooldown dicts) — reset around every test.
    plate_tracker.reset()
    yield
    plate_tracker.reset()


def test_repeated_reads_converge_on_the_peak_confidence():
    """The worked example from the V2 brief: four agreeing reads report the
    plate at its best observed confidence, not the latest or the mean."""
    for confidence in (0.72, 0.91, 0.94, 0.89):
        plate_tracker.record_read("cam1", "284", "GJ05AB1234", confidence)

    text, peak, reads = plate_tracker.get("cam1", "284").best()
    assert text == "GJ05AB1234"
    assert peak == pytest.approx(0.94)
    assert reads == 4


def test_one_bad_frame_does_not_overwrite_an_established_plate():
    """A single high-confidence misread must not beat three corroborating
    reads — this is exactly what voting buys over last-write-wins."""
    for confidence in (0.72, 0.91, 0.94):
        plate_tracker.record_read("cam1", "284", "GJ05AB1234", confidence)
    plate_tracker.record_read("cam1", "284", "GJ05AB1284", 0.95)

    text, _, _ = plate_tracker.get("cam1", "284").best()
    assert text == "GJ05AB1234"


def test_a_persistent_alternative_can_still_win():
    """Voting must not be a ratchet. If the early reads were wrong and the
    vehicle is subsequently read consistently as something else, the winner
    changes — otherwise a bad start would permanently mislabel the vehicle."""
    plate_tracker.record_read("cam1", "284", "GJ05AB1284", 0.55)
    for confidence in (0.88, 0.91, 0.93):
        plate_tracker.record_read("cam1", "284", "GJ05AB1234", confidence)

    text, _, _ = plate_tracker.get("cam1", "284").best()
    assert text == "GJ05AB1234"


def test_tracks_are_scoped_per_camera():
    """ByteTrack ids are only unique per predictor instance and detector.py
    keeps one per camera, so track 284 on two cameras is two vehicles."""
    plate_tracker.record_read("cam1", "284", "GJ05AB1234", 0.9)
    plate_tracker.record_read("cam2", "284", "GJ01XY7788", 0.9)

    assert plate_tracker.get("cam1", "284").best()[0] == "GJ05AB1234"
    assert plate_tracker.get("cam2", "284").best()[0] == "GJ01XY7788"


def test_no_reads_means_no_result_never_a_guess():
    plate_tracker.touch("cam1", "284")
    assert plate_tracker.get("cam1", "284").best() is None


class TestOcrGating:
    """`should_ocr` is the pipeline's main CPU saving — previously every vehicle
    detection ran a full OCR pass on every inference cycle."""

    def test_unread_track_is_always_ocred(self):
        plate_tracker.touch("cam1", "284")
        assert plate_tracker.should_ocr("cam1", "284") is True

    def test_unknown_track_is_ocred(self):
        assert plate_tracker.should_ocr("cam1", "999") is True

    def test_stable_track_is_not_reocred_immediately(self):
        for _ in range(settings.plate_min_reads_for_stability):
            plate_tracker.record_read("cam1", "284", "GJ05AB1234", 0.9)
        assert plate_tracker.get("cam1", "284").is_stable() is True
        assert plate_tracker.should_ocr("cam1", "284") is False

    def test_low_confidence_reads_never_become_stable(self):
        """Consistency alone is not trust: a plate read five times at 0.36 is
        still a bad read and must keep being re-checked."""
        for _ in range(5):
            plate_tracker.record_read("cam1", "284", "GJ05AB1234", 0.36)
        assert plate_tracker.get("cam1", "284").is_stable() is False
        assert plate_tracker.should_ocr("cam1", "284") is True

    def test_stable_track_is_reocred_after_the_reverify_interval(self, monkeypatch):
        for _ in range(settings.plate_min_reads_for_stability):
            plate_tracker.record_read("cam1", "284", "GJ05AB1234", 0.9)
        monkeypatch.setattr(settings, "plate_reverify_seconds", 0.0)
        assert plate_tracker.should_ocr("cam1", "284") is True

    def test_unreadable_track_is_throttled_not_hammered(self, monkeypatch):
        """A vehicle whose plate genuinely cannot be read never becomes stable,
        so without mark_ocr_attempt it would be re-OCR'd every single cycle
        forever. It is retried, but on the reverify interval."""
        monkeypatch.setattr(settings, "plate_reverify_seconds", 60.0)
        plate_tracker.mark_ocr_attempt("cam1", "284")
        # Not stable — no read ever passed the gate — but recently attempted.
        assert plate_tracker.get("cam1", "284").is_stable() is False
        assert plate_tracker.should_ocr("cam1", "284") is True, (
            "an unread track must stay eligible; the interval throttles the "
            "worker's own retry pacing, it must not silently give up"
        )


class TestPersistGating:
    """`should_persist` is what stops the sighting row being rewritten every
    frame for as long as a vehicle stays in view."""

    def test_first_read_persists(self):
        plate_tracker.record_read("cam1", "284", "GJ05AB1234", 0.9)
        assert plate_tracker.should_persist("cam1", "284", new_read=True) is True

    def test_bound_row_without_a_new_read_does_not_persist(self, monkeypatch):
        monkeypatch.setattr(settings, "plate_sighting_refresh_seconds", 60.0)
        plate_tracker.record_read("cam1", "284", "GJ05AB1234", 0.9)
        plate_tracker.bind_plate_row("cam1", "284", "plt_1", "veh_1", "GJ05AB1234")
        assert plate_tracker.should_persist("cam1", "284", new_read=False) is False

    def test_a_new_read_always_persists(self, monkeypatch):
        monkeypatch.setattr(settings, "plate_sighting_refresh_seconds", 60.0)
        plate_tracker.record_read("cam1", "284", "GJ05AB1234", 0.9)
        plate_tracker.bind_plate_row("cam1", "284", "plt_1", "veh_1", "GJ05AB1234")
        assert plate_tracker.should_persist("cam1", "284", new_read=True) is True

    def test_a_changed_vote_winner_persists(self, monkeypatch):
        """If the winner flips after the row was written, the row is wrong and
        must be rewritten even though no refresh interval has elapsed."""
        monkeypatch.setattr(settings, "plate_sighting_refresh_seconds", 60.0)
        plate_tracker.record_read("cam1", "284", "GJ05AB1284", 0.55)
        plate_tracker.bind_plate_row("cam1", "284", "plt_1", "veh_1", "GJ05AB1284")
        for confidence in (0.88, 0.91, 0.93):
            plate_tracker.record_read("cam1", "284", "GJ05AB1234", confidence)
        assert plate_tracker.should_persist("cam1", "284", new_read=False) is True

    def test_refresh_interval_advances_last_seen(self, monkeypatch):
        monkeypatch.setattr(settings, "plate_sighting_refresh_seconds", 0.0)
        plate_tracker.record_read("cam1", "284", "GJ05AB1234", 0.9)
        plate_tracker.bind_plate_row("cam1", "284", "plt_1", "veh_1", "GJ05AB1234")
        assert plate_tracker.should_persist("cam1", "284", new_read=False) is True

    def test_unknown_track_never_persists(self):
        assert plate_tracker.should_persist("cam1", "no-such-track", new_read=True) is False


class TestLifecycle:
    def test_stale_tracks_are_pruned(self):
        """ByteTrack never announces that a track id retired, so the TTL is the
        only thing bounding this dict on a camera running for days."""
        plate_tracker.record_read("cam1", "284", "GJ05AB1234", 0.9)
        # The track is aged directly rather than by sleeping past a shortened
        # TTL: time.monotonic() has ~15.6ms granularity on Windows, so a short
        # real sleep can measure as exactly zero elapsed and fail this test for
        # a reason that has nothing to do with pruning.
        state = plate_tracker.get("cam1", "284")
        state.last_seen_mono -= settings.plate_track_ttl_seconds + 1.0

        plate_tracker.touch("cam1", "999")  # any touch triggers the prune sweep

        assert plate_tracker.get("cam1", "284") is None
        assert plate_tracker.get("cam1", "999") is not None, "the live track must survive the sweep"

    def test_release_camera_drops_only_that_cameras_tracks(self):
        plate_tracker.record_read("cam1", "284", "GJ05AB1234", 0.9)
        plate_tracker.record_read("cam2", "284", "GJ01XY7788", 0.9)
        plate_tracker.release_camera("cam1")
        assert plate_tracker.get("cam1", "284") is None
        assert plate_tracker.get("cam2", "284") is not None
