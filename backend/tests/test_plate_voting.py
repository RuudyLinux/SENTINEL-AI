"""Plate voting across frames.

One bad OCR frame used to overwrite a good result. Reads now accumulate per
(camera, track) and vote by summed confidence; the reported confidence is
the peak seen.
"""
import pytest

from app.config import settings
from app.pipeline import plate_tracker


@pytest.fixture(autouse=True)
def _isolate():
    # module state is process-global, reset around every test
    plate_tracker.reset()
    yield
    plate_tracker.reset()


def test_repeated_reads_converge_on_the_peak_confidence():
    """Four agreeing reads report the best confidence seen, not the latest
    or the mean."""
    for confidence in (0.72, 0.91, 0.94, 0.89):
        plate_tracker.record_read("cam1", "284", "GJ05AB1234", confidence)

    text, peak, reads = plate_tracker.get("cam1", "284").best()
    assert text == "GJ05AB1234"
    assert peak == pytest.approx(0.94)
    assert reads == 4


def test_one_bad_frame_does_not_overwrite_an_established_plate():
    """One confident misread doesn't beat three agreeing reads."""
    for confidence in (0.72, 0.91, 0.94):
        plate_tracker.record_read("cam1", "284", "GJ05AB1234", confidence)
    plate_tracker.record_read("cam1", "284", "GJ05AB1284", 0.95)

    text, _, _ = plate_tracker.get("cam1", "284").best()
    assert text == "GJ05AB1234"


def test_a_persistent_alternative_can_still_win():
    """Not a ratchet: if early reads were wrong and later ones agree on
    something else, the winner changes."""
    plate_tracker.record_read("cam1", "284", "GJ05AB1284", 0.55)
    for confidence in (0.88, 0.91, 0.93):
        plate_tracker.record_read("cam1", "284", "GJ05AB1234", confidence)

    text, _, _ = plate_tracker.get("cam1", "284").best()
    assert text == "GJ05AB1234"


def test_tracks_are_scoped_per_camera():
    """Track 284 on two cameras is two vehicles."""
    plate_tracker.record_read("cam1", "284", "GJ05AB1234", 0.9)
    plate_tracker.record_read("cam2", "284", "GJ01XY7788", 0.9)

    assert plate_tracker.get("cam1", "284").best()[0] == "GJ05AB1234"
    assert plate_tracker.get("cam2", "284").best()[0] == "GJ01XY7788"


def test_no_reads_means_no_result_never_a_guess():
    plate_tracker.touch("cam1", "284")
    assert plate_tracker.get("cam1", "284").best() is None


class TestOcrGating:
    """should_ocr is the main CPU saving; every detection used to get OCR
    every cycle."""

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
        """Five reads at 0.36 are consistent but still bad; keep checking."""
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
        """An unreadable plate never gets stable; without mark_ocr_attempt it'd
        be OCR'd every cycle forever. Retried on the reverify interval."""
        monkeypatch.setattr(settings, "plate_reverify_seconds", 60.0)
        plate_tracker.mark_ocr_attempt("cam1", "284")
        # not stable (nothing passed), but attempted recently
        assert plate_tracker.get("cam1", "284").is_stable() is False
        assert plate_tracker.should_ocr("cam1", "284") is True, (
            "an unread track must stay eligible; the interval throttles the "
            "worker's own retry pacing, it must not silently give up"
        )


class TestPersistGating:
    """should_persist keeps the sighting row from being rewritten every frame."""

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
        """Winner flipped after the write: rewrite now, refresh interval or not."""
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
        """ByteTrack never says a track ended; the TTL bounds this dict."""
        plate_tracker.record_read("cam1", "284", "GJ05AB1234", 0.9)
        # aged directly instead of sleeping past a short TTL, monotonic is
        # ~15.6ms granular on Windows and a short sleep can read as zero
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
