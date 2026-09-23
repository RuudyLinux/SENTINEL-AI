"""Temporal consensus and the persistence gate.

The behavior this locks down: a plate becomes TRUSTED intelligence only after
enough independent frames agreed on it. Before this gate existed, the first
gate-passing OCR read created a Vehicle and a Plate row outright — one lucky
frame, one plate-shaped-but-wrong read clearing the confidence floor, became a
durable vehicle identity.

What it must NOT do is discard real observations. A vehicle crossing the frame
in a single inference cycle gets exactly one read and will never get another;
that sighting is kept, flagged for review rather than presented as settled.
"""
import pytest

from app.config import settings
from app.pipeline import plate_tracker
from app.pipeline.anpr import review_status_for

PLATE = "GJ05AB1234"
OTHER = "GJ05AB1284"


@pytest.fixture(autouse=True)
def _isolate_tracks():
    """Module state is process-global by design — same convention as the other
    per-track state in this codebase."""
    plate_tracker.reset()
    yield
    plate_tracker.reset()


class TestConsensus:
    def test_no_reads_means_no_consensus_never_a_guess(self):
        assert plate_tracker.consensus("cam1", "284") is None
        assert plate_tracker.has_consensus("cam1", "284") is False

    def test_one_read_is_not_corroborated_by_default(self, monkeypatch):
        monkeypatch.setattr(settings, "plate_min_observations", 2)
        plate_tracker.record_read("cam1", "284", PLATE, 0.91)
        assert plate_tracker.has_consensus("cam1", "284") is False, (
            "a single frame, however confident, is not temporal corroboration"
        )

    def test_enough_agreeing_reads_reach_consensus(self, monkeypatch):
        monkeypatch.setattr(settings, "plate_min_observations", 2)
        plate_tracker.record_read("cam1", "284", PLATE, 0.72)
        plate_tracker.record_read("cam1", "284", PLATE, 0.88)
        assert plate_tracker.has_consensus("cam1", "284") is True

    def test_the_threshold_is_configurable(self, monkeypatch):
        monkeypatch.setattr(settings, "plate_min_observations", 4)
        for confidence in (0.7, 0.8, 0.9):
            plate_tracker.record_read("cam1", "284", PLATE, confidence)
        assert plate_tracker.has_consensus("cam1", "284") is False
        plate_tracker.record_read("cam1", "284", PLATE, 0.85)
        assert plate_tracker.has_consensus("cam1", "284") is True

    def test_one_observation_restores_the_previous_behavior(self, monkeypatch):
        """The escape hatch must be real: setting the threshold to 1 persists on
        the first passing read, exactly as before."""
        monkeypatch.setattr(settings, "plate_min_observations", 1)
        plate_tracker.record_read("cam1", "284", PLATE, 0.50)
        assert plate_tracker.has_consensus("cam1", "284") is True

    def test_disagreeing_reads_do_not_accumulate_toward_consensus(self, monkeypatch):
        """Two frames reading two DIFFERENT plates is not two observations of
        one plate — it is a track the system is confused about."""
        monkeypatch.setattr(settings, "plate_min_observations", 2)
        plate_tracker.record_read("cam1", "284", PLATE, 0.80)
        plate_tracker.record_read("cam1", "284", OTHER, 0.80)
        assert plate_tracker.has_consensus("cam1", "284") is False


class TestConsensusSignals:
    def test_every_signal_is_reported_separately(self, monkeypatch):
        monkeypatch.setattr(settings, "plate_min_observations", 2)
        for confidence in (0.72, 0.91, 0.94):
            plate_tracker.record_read("cam1", "284", PLATE, confidence)
        plate_tracker.record_read("cam1", "284", OTHER, 0.55)

        result = plate_tracker.consensus("cam1", "284")
        assert result.text == PLATE
        assert result.peak_confidence == pytest.approx(0.94), "the best the engine ever reported"
        assert result.observations == 3
        assert result.total_observations == 4
        assert result.agreement == pytest.approx(0.75)
        assert result.competing_text == OTHER
        assert result.competing_observations == 1

    def test_peak_confidence_is_not_inflated_by_observation_count(self):
        """Four agreeing reads at 0.60 stay 0.60. Corroboration is reported as a
        count, never mixed into the confidence."""
        for _ in range(4):
            plate_tracker.record_read("cam1", "284", PLATE, 0.60)
        result = plate_tracker.consensus("cam1", "284")
        assert result.peak_confidence == pytest.approx(0.60)
        assert result.observations == 4

    def test_unanimous_and_split_tracks_are_distinguishable(self):
        for _ in range(4):
            plate_tracker.record_read("cam1", "unanimous", PLATE, 0.7)
        for _ in range(2):
            plate_tracker.record_read("cam1", "split", PLATE, 0.7)
            plate_tracker.record_read("cam1", "split", OTHER, 0.7)
        assert plate_tracker.consensus("cam1", "unanimous").agreement == pytest.approx(1.0)
        assert plate_tracker.consensus("cam1", "split").agreement == pytest.approx(0.5)


class TestReviewGating:
    def test_an_uncorroborated_read_is_always_flagged_for_review(self, monkeypatch):
        """Even at a confidence that would otherwise auto-accept. A
        high-confidence read observed once is not corroborated evidence."""
        monkeypatch.setattr(settings, "plate_review_confidence_floor", 0.60)
        assert review_status_for(0.95, None, corroborated=False) == "pending_review"

    def test_a_corroborated_confident_read_is_auto_accepted(self, monkeypatch):
        monkeypatch.setattr(settings, "plate_review_confidence_floor", 0.60)
        assert review_status_for(0.95, None, corroborated=True) == "auto_accepted"

    def test_a_corroborated_but_low_confidence_read_still_needs_review(self, monkeypatch):
        """Corroboration does not substitute for confidence — the two gates are
        independent and both must pass."""
        monkeypatch.setattr(settings, "plate_review_confidence_floor", 0.60)
        assert review_status_for(0.40, None, corroborated=True) == "pending_review"

    @pytest.mark.parametrize("terminal", ["corrected", "rejected"])
    def test_a_human_decision_is_never_overwritten(self, terminal):
        """Terminal states survive any subsequent OCR frame, corroborated or
        not — a machine read must not silently undo an operator."""
        assert review_status_for(0.99, terminal, corroborated=True) == terminal
        assert review_status_for(0.10, terminal, corroborated=False) == terminal

    def test_the_default_keeps_the_previous_signature_working(self):
        """`corroborated` defaults to True so existing callers are unchanged."""
        assert review_status_for(0.95) == "auto_accepted"


class TestTrackCleanup:
    def test_consensus_state_is_dropped_when_the_camera_stops(self):
        plate_tracker.record_read("cam1", "284", PLATE, 0.9)
        plate_tracker.record_read("cam2", "284", PLATE, 0.9)
        plate_tracker.release_camera("cam1")
        assert plate_tracker.consensus("cam1", "284") is None
        assert plate_tracker.consensus("cam2", "284") is not None

    def test_a_stale_track_is_pruned_and_loses_its_consensus(self):
        """ByteTrack never announces that a track id retired, so the TTL is the
        only thing bounding this state on a long-running camera."""
        plate_tracker.record_read("cam1", "284", PLATE, 0.9)
        state = plate_tracker.get("cam1", "284")
        state.last_seen_mono -= settings.plate_track_ttl_seconds + 1.0
        plate_tracker.touch("cam1", "999")  # any touch triggers the prune sweep
        assert plate_tracker.consensus("cam1", "284") is None

    def test_the_held_plate_crop_is_released_with_the_track(self):
        """The crop is held only until the sighting's evidence is written; it
        must not outlive the track and accumulate."""
        plate_tracker.record_read("cam1", "284", PLATE, 0.9, plate_crop=object())
        assert plate_tracker.get("cam1", "284").last_plate_crop is not None
        plate_tracker.release_camera("cam1")
        assert plate_tracker.get("cam1", "284") is None


class TestProvenanceRecording:
    def test_variant_provenance_is_stored_without_affecting_the_vote(self):
        """Cross-variant agreement is recorded for the audit trail but must not
        also weight the temporal vote — that would count one corroboration
        twice."""
        plate_tracker.record_read("cam1", "284", PLATE, 0.60, variant="sharpen", variants_agreeing=5)
        plate_tracker.record_read("cam1", "284", OTHER, 0.60, variant="clahe", variants_agreeing=1)
        state = plate_tracker.get("cam1", "284")
        assert state.votes[PLATE].variant == "sharpen"
        assert state.votes[PLATE].variants_agreeing == 5
        # Equal confidence, one read each — the 5-variant agreement must not
        # have tipped the temporal vote on its own.
        assert state.votes[PLATE].summed_confidence == pytest.approx(
            state.votes[OTHER].summed_confidence
        )

    def test_defaults_keep_single_variant_callers_unchanged(self):
        plate_tracker.record_read("cam1", "284", PLATE, 0.60)
        assert plate_tracker.get("cam1", "284").votes[PLATE].variants_agreeing == 1
