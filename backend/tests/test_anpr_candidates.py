"""OCR candidates: structured reads, agreement-based selection, and
multi-variant reading only ever tightening the gate.

Confidence and cross-variant agreement stay separate: agreement doesn't make
a read more confident, and confidence doesn't imply agreement.
"""
import pytest

from app.config import settings
from app.pipeline.anpr import (
    OcrCandidate, OcrRead, passes_anpr_gate, passes_read_gate, select_candidate,
)

PLATE = "GJ05AB1234"
OTHER = "GJ05AB1284"


def _candidate(variant: str, text: str, confidence: float) -> OcrCandidate:
    return OcrCandidate(variant=variant, raw=text, normalized=text, confidence=confidence)


class TestAgreementSelection:
    def test_the_most_agreed_text_wins_over_a_single_confident_outlier(self):
        """On the labelled corpus, picking the most confident variant scored
        worse than the most agreed one and raised false positives."""
        read = select_candidate([
            _candidate("original", PLATE, 0.55),
            _candidate("sharpen", PLATE, 0.58),
            _candidate("adaptive", PLATE, 0.52),
            _candidate("otsu", OTHER, 0.95),  # a lone, very confident outlier
        ])
        assert read.normalized == PLATE
        assert read.variants_agreeing == 3
        assert read.variant_count == 4

    def test_reported_confidence_is_the_mean_of_agreeing_reads_not_the_max(self):
        """Max of N is biased high; reporting it would inflate every read and
        loosen the gates downstream."""
        read = select_candidate([
            _candidate("original", PLATE, 0.50),
            _candidate("sharpen", PLATE, 0.90),
        ])
        assert read.confidence == pytest.approx(0.70)
        assert read.confidence < 0.90, "the maximum must never be reported as the confidence"

    def test_agreement_does_not_raise_the_reported_confidence(self):
        """Five variants agreeing at 0.57 is stronger evidence, but the
        engine still said 0.57. Agreement is reported separately."""
        read = select_candidate([_candidate(f"v{i}", PLATE, 0.57) for i in range(5)])
        assert read.confidence == pytest.approx(0.57)
        assert read.variants_agreeing == 5

    def test_ties_break_toward_a_gate_passing_read(self):
        read = select_candidate([
            _candidate("original", PLATE, 0.80),
            _candidate("otsu", OTHER, 0.10),
        ])
        assert read.normalized == PLATE

    def test_an_empty_read_never_beats_a_real_one(self):
        read = select_candidate([
            _candidate("original", "", 0.99),
            _candidate("sharpen", PLATE, 0.40),
        ])
        assert read.normalized == PLATE

    def test_all_empty_reads_report_an_honest_empty_result(self):
        """Nothing read means nothing, not the least-bad garbage."""
        read = select_candidate([
            _candidate("original", "", 0.0),
            _candidate("sharpen", "", 0.0),
        ])
        assert read.normalized == ""
        assert read.variants_agreeing == 0
        assert read.variant_count == 2

    def test_no_candidates_at_all(self):
        read = select_candidate([])
        assert read.normalized == "" and read.variant_count == 0 and read.confidence == 0.0

    def test_a_single_candidate_is_returned_verbatim(self):
        """Default config: one variant in, that read out unchanged."""
        read = select_candidate([_candidate("clahe", PLATE, 0.61)])
        assert read.normalized == PLATE
        assert read.confidence == pytest.approx(0.61)
        assert read.variants_agreeing == 1 and read.variant_count == 1

    def test_the_candidates_are_preserved_for_audit(self):
        candidates = [_candidate("original", PLATE, 0.5), _candidate("otsu", OTHER, 0.6)]
        read = select_candidate(candidates)
        assert len(read.candidates) == 2
        assert {c.normalized for c in read.candidates} == {PLATE, OTHER}

    def test_the_winning_variant_is_recorded(self):
        read = select_candidate([
            _candidate("original", PLATE, 0.40),
            _candidate("sharpen", PLATE, 0.80),
        ])
        assert read.variant == "sharpen", "provenance names the variant that read best"


class TestReadGate:
    def test_a_single_variant_read_is_gated_exactly_as_before(self, monkeypatch):
        """One variant: agreement rule can't fire, gate is the old one."""
        monkeypatch.setattr(settings, "plate_min_confidence", 0.35)
        read = OcrRead(raw=PLATE, normalized=PLATE, confidence=0.50, variant_count=1, variants_agreeing=1)
        assert passes_read_gate(read) is True
        assert passes_read_gate(read) == passes_anpr_gate(read.normalized, read.confidence)

    def test_multi_variant_reading_can_only_tighten_the_gate(self, monkeypatch):
        """High confidence from only ONE of several variants isn't
        corroborated and must not be auto-accepted."""
        monkeypatch.setattr(settings, "plate_min_confidence", 0.35)
        monkeypatch.setattr(settings, "plate_min_variants_agreeing", 2)
        lonely = OcrRead(
            raw=PLATE, normalized=PLATE, confidence=0.91, variant_count=7, variants_agreeing=1,
        )
        assert passes_read_gate(lonely) is False, (
            "0.91 confidence with 1 of 7 variants agreeing is not corroboration"
        )

    def test_a_lower_confidence_but_corroborated_read_passes(self, monkeypatch):
        monkeypatch.setattr(settings, "plate_min_confidence", 0.35)
        monkeypatch.setattr(settings, "plate_min_variants_agreeing", 2)
        corroborated = OcrRead(
            raw=PLATE, normalized=PLATE, confidence=0.57, variant_count=7, variants_agreeing=5,
        )
        assert passes_read_gate(corroborated) is True

    def test_agreement_never_rescues_a_below_floor_confidence(self, monkeypatch):
        """Agreement doesn't replace the confidence floor. Seven variants
        agreeing on a 0.05 read is seven variants failing the same way."""
        monkeypatch.setattr(settings, "plate_min_confidence", 0.35)
        monkeypatch.setattr(settings, "plate_min_variants_agreeing", 2)
        read = OcrRead(
            raw=PLATE, normalized=PLATE, confidence=0.05, variant_count=7, variants_agreeing=7,
        )
        assert passes_read_gate(read) is False

    def test_agreement_never_rescues_an_implausible_format(self, monkeypatch):
        monkeypatch.setattr(settings, "plate_min_variants_agreeing", 2)
        read = OcrRead(
            raw="HELLO", normalized="HELLO", confidence=0.99, variant_count=7, variants_agreeing=7,
        )
        assert passes_read_gate(read) is False

    def test_an_empty_read_never_passes(self):
        assert passes_read_gate(OcrRead(raw="", normalized="", confidence=0.99)) is False


class TestLegacyTupleContract:
    def test_as_tuple_matches_the_long_standing_three_tuple(self):
        read = OcrRead(raw="GJ 05 AB 1234", normalized=PLATE, confidence=0.61)
        assert read.as_tuple() == ("GJ 05 AB 1234", PLATE, 0.61)
